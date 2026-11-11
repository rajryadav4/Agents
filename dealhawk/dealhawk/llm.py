"""Claude as a deal judge.

The formula is blind when it has no price history for an item, and it can't
tell a $1,299 MacBook Pro from a $12.99 MacBook Pro *case*. Claude can: it
knows roughly what products sell for, spots accessory / refurb / bundle
mismatches, and recognises the shape of a price mistake.

We only ask about candidates that already pass your watch filters, batch them
(one request per ~10 offers), cache every verdict, and cap calls per hour.

Requires `pip install anthropic` and ANTHROPIC_API_KEY (or `ant auth login`).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

from .models import Offer, Score

log = logging.getLogger("dealhawk.llm")

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "product_match": {"type": "boolean"},
                    "deal_quality": {"type": "integer"},
                    "estimated_typical_price": {"type": "number"},
                    "likely_price_error": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "product_match", "deal_quality", "estimated_typical_price",
                             "likely_price_error", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

SYSTEM = """You are DealHawk's deal judge. You see product listings scraped from US retailers and \
deal communities, and decide which are genuinely exceptional prices worth interrupting a person for.

For each listing return:
- product_match: false if the listing is not really the product its title suggests to a shopper — an \
accessory, case, part, subscription, rental, used/for-parts item presented as new, a bundle where the \
headline price is misleading, or a "from $X" teaser. true otherwise.
- deal_quality: 0-100. 50 = an ordinary sale. 70 = clearly better than typical sale prices. \
85+ = rare: at or below the best prices this product normally sees. 95+ = a likely price mistake or \
once-a-year price. Judge against what the item actually sells for, not its claimed "list" price.
- estimated_typical_price: what this exact item usually sells for at major US retailers today, \
in USD; 0 if you genuinely don't know.
- likely_price_error: true only if the price is so far below normal that it is probably a glitch.
- reason: one short sentence a shopper would find useful.

Your price knowledge has a cutoff and products get cheaper over time (new models launch, old ones \
are cleared out). When unsure, stay near 50 rather than guessing high. Price and history fields \
given to you are measured data — trust them over your memory."""


@dataclass
class LLMConfig:
    enabled: bool = False
    model: str = "claude-opus-5-5"
    effort: str = "low"               # classification works well at low effort
    profile: str = ""                 # free text about you: "student, want MacBooks, don't care about clothes"
    batch_size: int = 10
    max_calls_per_hour: int = 6       # each call judges up to batch_size offers
    min_rule_score: int = 15          # don't spend calls on offers the formula thinks are hopeless
    weight: float = 0.5               # how much Claude moves a score that has real price history


def _describe(i: int, o: Offer, s: Score) -> dict:
    d = {"id": i, "title": o.title, "retailer": o.retailer, "price": o.price}
    if o.reference_price:
        d["claimed_regular_price"] = o.reference_price
    if s.basis in ("history", "source-stats") and s.typical_price:
        d["measured_typical_price"] = round(s.typical_price, 2)
        if s.low_price:
            d["measured_usual_low"] = round(s.low_price, 2)
    if o.category:
        d["category"] = o.category
    if o.community_score:
        d["community_votes"] = o.community_score
    if o.tags:
        d["tags"] = o.tags[:5]
    return d


class ClaudeJudge:
    def __init__(self, cfg: LLMConfig, client=None):
        self.cfg = cfg
        if client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover - depends on install
                raise RuntimeError("llm.enabled needs the SDK: pip install 'dealhawk[claude]'") from e
            client = anthropic.Anthropic()
        self.client = client
        system = SYSTEM
        if cfg.profile.strip():
            system += "\n\nAbout the person you are judging for:\n" + cfg.profile.strip()
        self.system = system

    def judge(self, items: list[tuple[Offer, Score]]) -> list[dict | None]:
        """One request for a batch. Returns a verdict (or None) per item, in order."""
        if not items:
            return []
        payload = [_describe(i, o, s) for i, (o, s) in enumerate(items)]
        user = (f"Today is {time.strftime('%Y-%m-%d')}. Judge these {len(items)} listings:\n"
                + json.dumps(payload, ensure_ascii=False))
        try:
            import anthropic
            errors: tuple = (anthropic.APIStatusError, anthropic.APIConnectionError)
        except ImportError:  # pragma: no cover
            errors = (Exception,)
        try:
            resp = self.client.beta.messages.create(
                model=self.cfg.model,
                max_tokens=16000,
                system=self.system,
                cache_control={"type": "ephemeral"},       # the system prompt is identical every call
                messages=[{"role": "user", "content": user}],
                output_config={"effort": self.cfg.effort,
                               "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except errors as e:
            log.warning("Claude judge unavailable: %s", e)
            return [None] * len(items)
        if resp.stop_reason == "refusal":
            log.warning("Claude declined to judge this batch")
            return [None] * len(items)
        if resp.stop_reason == "max_tokens":
            log.warning("Claude judge output was truncated")
            return [None] * len(items)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            verdicts = json.loads(text)["verdicts"]
        except (json.JSONDecodeError, KeyError, TypeError):
            log.warning("Claude judge returned unparseable output")
            return [None] * len(items)
        by_id = {v.get("id"): v for v in verdicts if isinstance(v, dict)}
        out = []
        for i in range(len(items)):
            v = by_id.get(i)
            if v is not None:
                v = {**v, "deal_quality": max(0, min(100, int(v.get("deal_quality", 50))))}
            out.append(v)
        return out


def combine(score: Score, verdict: dict | None, cfg: LLMConfig) -> Score:
    """Fold Claude's verdict into a Score (mutates and returns it)."""
    if not verdict:
        return score
    score.llm = verdict
    q = verdict["deal_quality"]
    if not verdict.get("product_match", True):
        score.value = round(score.value * 0.3)
        score.reasons.insert(0, f"Claude: probably not the real product — {verdict.get('reason', '')}")
        return score
    if score.basis in ("history", "source-stats"):
        # we have real data; Claude nudges
        score.value = round((1 - cfg.weight) * score.value + cfg.weight * max(q, score.value * 0.8))
    else:
        # cold start: Claude's market knowledge is the best signal we have
        score.value = max(score.value, round(0.9 * q))
        typ = verdict.get("estimated_typical_price") or 0
        if typ and score.typical_price is None:
            score.typical_price = float(typ)
    if verdict.get("likely_price_error"):
        score.glitch_suspect = True
        score.value = max(score.value, 85)
    score.reasons.insert(0, f"Claude ({q}/100): {verdict.get('reason', '')}")
    return score
