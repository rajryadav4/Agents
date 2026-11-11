"""The Brain: formula + learned model + Claude + crowd, combined into one decision.

    rule score (scoring.py) ──► Claude verdict (llm.py, cold-start & borderline items)
                           └──► learned model (learner.py, trained on your 👍/👎 + crowd)
                                     │
                                     ▼
                         final score · tier · reasons · "explore?" flag
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .learner import Learner, featurize
from .llm import ClaudeJudge, LLMConfig, combine
from .models import Offer, Score
from .scoring import ScoringConfig, tier_for
from .store import Store

log = logging.getLogger("dealhawk.brain")


@dataclass
class LearningConfig:
    enabled: bool = True
    learning_rate: float = 0.15
    warmup_labels: float = 40       # labels before the learned model gets its full say
    explore_per_day: int = 6        # extra "is this good?" alerts below your threshold, to learn faster
    explore_min_score: int = 35     # never explore junk
    crowd_labels: bool = True       # learn from how deal communities react
    crowd_hot_votes_per_hour: float = 30
    crowd_cold_votes_per_hour: float = 2


LABEL_WEIGHTS = {"user": 1.0, "click": 0.3, "crowd": 0.25}


class Brain:
    def __init__(self, store: Store, scoring: ScoringConfig, learning: LearningConfig | None = None,
                 llm: LLMConfig | None = None, judge: ClaudeJudge | bool | None = None):
        self.store = store
        self.scoring = scoring
        self.learning = learning or LearningConfig()
        self.llm_cfg = llm or LLMConfig()
        if judge is False:                       # explicit opt-out (CLI tools that only train)
            judge = None
        elif judge is None and self.llm_cfg.enabled:
            judge = ClaudeJudge(self.llm_cfg)
        self.judge = judge
        self.learner = Learner.loads(store.kv_get(Learner.KV_KEY), lr=self.learning.learning_rate,
                                     warmup_labels=self.learning.warmup_labels)

    # ---- Claude ------------------------------------------------------------
    def consult_claude(self, items: list[tuple[Offer, Score]], now: float) -> dict[str, dict]:
        """Get verdicts for candidates (cache first, then batched calls within the hourly budget)."""
        verdicts: dict[str, dict] = {}
        if self.judge is None:
            return verdicts
        todo = []
        for o, s in items:
            if s.value < self.llm_cfg.min_rule_score and s.basis in ("history", "source-stats"):
                continue   # we have data and it says no
            cached = self.store.llm_cached(o.key, o.price)
            if cached is not None:
                verdicts[o.key] = cached
            else:
                todo.append((o, s))
        todo.sort(key=lambda it: it[1].value, reverse=True)   # most promising first
        bs = max(1, self.llm_cfg.batch_size)
        for i in range(0, len(todo), bs):
            if self.store.llm_calls_since(now - 3600) >= self.llm_cfg.max_calls_per_hour:
                log.info("Claude hourly budget reached; %d candidates judged by formula only", len(todo) - i)
                break
            batch = todo[i:i + bs]
            self.store.llm_note_call(now)
            for (o, _), v in zip(batch, self.judge.judge(batch)):
                if v is not None:
                    self.store.llm_store(o.key, o.price, v, now)
                    verdicts[o.key] = v
        return verdicts

    # ---- final decision ----------------------------------------------------
    def finalize(self, offer: Offer, score: Score, verdict: dict | None) -> tuple[Score, dict]:
        score.rule_value = score.value
        combine(score, verdict, self.llm_cfg)
        if score.typical_price and offer.price is not None and score.discount is None:
            score.discount = 1 - offer.price / score.typical_price
        features = featurize(offer, score)
        if self.learning.enabled and self.learner.n_labels > 0:
            before = score.value
            score.value, learned = self.learner.blend(score.value, features)
            delta = score.value - before
            if abs(delta) >= 5:
                score.reasons.append(f"learned from your feedback: {'+' if delta > 0 else ''}{delta}")
        if score.glitch_suspect:
            score.value = max(score.value, self.scoring.tiers["steal"] + 5)
        score.value = max(0, min(100, score.value))
        score.tier = tier_for(score.value, self.scoring)
        return score, features

    def should_explore(self, score: Score, features: dict, now: float) -> bool:
        L = self.learning
        if not (L.enabled and L.explore_per_day > 0) or score.value < L.explore_min_score:
            return False
        if self.store.explores_since(now - 86400) >= L.explore_per_day:
            return False
        # ask about the deals the model is least sure about
        return self.learner.uncertainty(features) >= 0.5

    # ---- learning ------------------------------------------------------------
    def learn(self, key: str, label: int, origin: str, features: dict, deal_id: int | None = None,
              now: float | None = None) -> bool:
        if not self.learning.enabled:
            return False
        if not self.store.add_feedback(key, label, LABEL_WEIGHTS[origin], origin, features, deal_id, now):
            return False
        self.learner.update(features, label, LABEL_WEIGHTS[origin])
        self.store.kv_set(Learner.KV_KEY, self.learner.dumps())
        return True

    def observe_crowd(self, offer: Offer, score: Score, features: dict, now: float) -> None:
        """Weak labels from community reaction: fast upvotes / fast sell-outs = a real deal."""
        L = self.learning
        if not (L.enabled and L.crowd_labels) or offer.posted_at is None:
            return
        age_h = (now - offer.posted_at) / 3600
        tags = {t.lower() for t in offer.tags}
        label = None
        if tags & {"expired", "sold out", "oos", "dead"} and age_h <= 6:
            label = 1                                   # gone within hours: it was good
        elif offer.community_score is not None and age_h >= 1.5:
            vel = offer.community_score / age_h
            if vel >= L.crowd_hot_votes_per_hour:
                label = 1
            elif age_h >= 6 and vel <= L.crowd_cold_votes_per_hour:
                label = 0
        if label is not None and not self.store.has_feedback(offer.key, "crowd"):
            self.learn(offer.key, label, "crowd", features, now=now)

    def retrain(self) -> int:
        """Rebuild the model from scratch by replaying every stored label."""
        self.learner = Learner(lr=self.learning.learning_rate, warmup_labels=self.learning.warmup_labels)
        rows = self.store.all_feedback()
        for _ in range(3):           # a few passes over the (small) dataset
            for r in rows:
                self.learner.update(r["features"], r["label"], r["weight"])
        self.learner.n_labels = sum(r["weight"] for r in rows)
        self.store.kv_set(Learner.KV_KEY, self.learner.dumps())
        return len(rows)
