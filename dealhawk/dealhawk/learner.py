"""A model that learns what *you* consider a good deal.

Online logistic regression over sparse features (discount depth, record-low,
retailer, category, price band, title words, community heat, Claude's opinion,
...), trained with AdaGrad on every piece of feedback:

  • your 👍 / 👎 taps on alerts (weight 1.0)
  • tapping "Open deal" (weak positive, 0.3)
  • the crowd: posts that rack up votes fast or sell out fast are positives,
    posts nobody cares about are negatives (weak, 0.2–0.3) — so it keeps
    learning even when you don't give feedback

It starts out mimicking the rule-based Steal Score and shifts toward its own
judgement as labels accumulate. It can then promote deals the formula
underrates (e.g. "this person always buys open-box ThinkPads") and demote
ones it overrates (e.g. "never cares about Kohl's clothing").
"""

from __future__ import annotations

import json
import math
import re
import threading

from .models import Offer, Score, normalize_title

_WORD = re.compile(r"[a-z][a-z0-9+]{2,}|\d{3,5}[a-z]*")


def _bucket(x: float, edges: list[float]) -> int:
    return sum(1 for e in edges if x >= e)


def featurize(offer: Offer, score: Score, now: float | None = None) -> dict[str, float]:
    """Sparse features describing a deal. Stored with each deal so later feedback can train on it."""
    rule = (score.rule_value if score.rule_value is not None else score.value) / 100
    f: dict[str, float] = {"bias": 1.0, "rule": rule, "rule_sq": rule * rule}
    if score.discount is not None:
        d = max(-0.5, min(1.0, score.discount))
        f["disc"] = d
        f[f"disc_b={_bucket(d, [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])}"] = 1.0
    if score.record_low:
        f["record_low"] = 1.0
    if score.glitch_suspect:
        f["glitch"] = 1.0
    f[f"basis={score.basis}"] = 1.0
    f[f"retailer={offer.retailer.lower()}"] = 1.0
    f[f"source={offer.source}"] = 1.0
    if offer.category:
        f[f"cat={offer.category.lower()[:30]}"] = 1.0
    if offer.price is not None:
        f[f"price_b={_bucket(offer.price, [10, 25, 50, 100, 200, 400, 800, 1500, 3000])}"] = 1.0
    if offer.community_score:
        f["votes"] = math.log1p(offer.community_score) / 6
    if score.llm:
        f["llm_quality"] = score.llm.get("deal_quality", 50) / 100
        if not score.llm.get("product_match", True):
            f["llm_mismatch"] = 1.0
        if score.llm.get("likely_price_error"):
            f["llm_price_error"] = 1.0
    words = set(_WORD.findall(normalize_title(offer.title)))
    for w in sorted(words)[:40]:
        f[f"w={w}"] = 1.0
    return f


class Learner:
    """Sparse online logistic regression with AdaGrad + L2. Persisted as JSON in the store."""

    KV_KEY = "learner_v1"

    def __init__(self, lr: float = 0.15, l2: float = 1e-4, warmup_labels: float = 40.0):
        self.lr, self.l2, self.warmup = lr, l2, warmup_labels
        # Prior: p ≈ sigmoid(10·(rule − 0.6)) — i.e. start out agreeing with the formula.
        self.w: dict[str, float] = {"rule": 10.0, "bias": -6.0}
        self.g2: dict[str, float] = {}
        self.n_labels = 0.0
        self._lock = threading.Lock()

    # ---- inference ----
    def proba(self, f: dict[str, float]) -> float:
        z = sum(self.w.get(k, 0.0) * v for k, v in f.items())
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))

    @property
    def confidence(self) -> float:
        """0 → trust the formula entirely; 1 → trust the learned model entirely (capped at 0.8)."""
        return min(0.8, self.n_labels / self.warmup)

    def blend(self, rule_value: int, f: dict[str, float]) -> tuple[int, int]:
        """Return (final_score, learned_score)."""
        learned = round(100 * self.proba(f))
        a = self.confidence
        return round((1 - a) * rule_value + a * learned), learned

    def uncertainty(self, f: dict[str, float]) -> float:
        p = self.proba(f)
        return 1 - abs(p - 0.5) * 2      # 1 at p=0.5, 0 at p∈{0,1}

    # ---- training ----
    def update(self, f: dict[str, float], label: int, weight: float = 1.0) -> None:
        with self._lock:
            err = (self.proba(f) - label) * weight
            for k, v in f.items():
                g = err * v + self.l2 * self.w.get(k, 0.0)
                self.g2[k] = self.g2.get(k, 0.0) + g * g
                self.w[k] = self.w.get(k, 0.0) - self.lr * g / (math.sqrt(self.g2[k]) + 1e-8)
            self.n_labels += weight

    def top_weights(self, n: int = 12) -> list[tuple[str, float]]:
        items = [(k, v) for k, v in self.w.items() if k not in ("bias", "rule", "rule_sq")]
        return sorted(items, key=lambda kv: abs(kv[1]), reverse=True)[:n]

    # ---- persistence ----
    def dumps(self) -> str:
        return json.dumps({"w": self.w, "g2": self.g2, "n": self.n_labels})

    @classmethod
    def loads(cls, s: str | None, **kw) -> "Learner":
        m = cls(**kw)
        if s:
            d = json.loads(s)
            m.w, m.g2, m.n_labels = d["w"], d["g2"], d["n"]
        return m
