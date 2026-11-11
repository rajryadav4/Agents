"""The Steal Score: how unusual is this price *for this product*?

A "20% off" banner means nothing — list prices are inflated and half the store
is "on sale" all the time. What matters is how the price compares to what the
item *actually* sells for. So the score is built from the product's own price
history, in three parts:

  novelty  — is it below the item's usual *sale* low (10th percentile), and by how much?
  rarity   — robust z-score vs. the item's typical price (median / MAD): is this
             drop out of character for how this product normally moves?
  depth    — plain % below the typical price (a gate, so a 3% blip on a
             never-changing item can't score high, plus a bonus for huge cuts).

Example from the README: an item normally $100, whose sales bottom out at $60,
suddenly listed at $20 → novelty 1.0, rarity 1.0, depth 0.8 → 100 ("steal").
The same item at $80 ("20% off!") scores ~27 and never alerts.

With no history yet, we fall back to source-supplied stats (e.g. Keepa's 90-day
average for Amazon), then to the seller's claimed reference price (discounted,
because "was" prices lie), then to signals alone (community votes, "price
mistake" keywords).
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from .models import Offer, Score


@dataclass
class ScoringConfig:
    min_history_points: int = 4
    tiers: dict[str, int] = field(default_factory=lambda: {"steal": 80, "great": 60, "good": 40})
    glitch_vs_low: float = 0.5        # ≤ 50% of the prior low → suspected glitch
    glitch_vs_typical: float = 0.35   # ≤ 35% of typical price → suspected glitch
    keyword_boosts: dict[str, int] = field(default_factory=lambda: {
        "price mistake": 25, "price error": 25, "pricing error": 25, "glitch": 20,
        "penny deal": 15, "hidden clearance": 12, "clearance": 6,
        "lightning deal": 5, "all-time low": 8, "all time low": 8, "atl": 6,
        "lowest price ever": 8, "historical low": 8,
    })
    max_signal_bonus: int = 30


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def tier_for(value: int, cfg: ScoringConfig) -> str:
    for name in ("steal", "great", "good"):
        if value >= cfg.tiers[name]:
            return name
    return "meh"


def _signals(offer: Offer, cfg: ScoringConfig, now: float) -> tuple[int, list[str]]:
    bonus, reasons = 0, []
    text = (offer.title + " " + " ".join(offer.tags)).lower()
    hits = [(kw, pts) for kw, pts in cfg.keyword_boosts.items() if kw in text]
    if hits:
        kw, pts = max(hits, key=lambda h: h[1])
        bonus += pts
        reasons.append(f'tagged "{kw}"')
    if offer.community_score:
        age_h = max((now - offer.posted_at) / 3600, 0.25) if offer.posted_at else 6.0
        velocity = offer.community_score / age_h          # votes per hour
        pts = round(15 * _clamp(velocity / 60))
        if pts:
            bonus += pts
            reasons.append(f"community heat {offer.community_score} votes (~{velocity:.0f}/h)")
    return min(bonus, cfg.max_signal_bonus), reasons


def _from_stats(price: float, typical: float, low: float, spread: float) -> tuple[float, dict]:
    d = 1 - price / typical
    r = price / low if low > 0 else 1.0
    z = (typical - price) / spread
    novelty = _clamp((1.0 - r) / 0.30)          # 30% under the usual low → maxed
    rarity = _clamp((z - 1.5) / 3.5)
    gate = _clamp((d - 0.05) / 0.20)            # need ≥25% off typical for full credit
    depth = _clamp((d - 0.30) / 0.50)           # 80% off alone is a steal
    raw = max((0.5 * novelty + 0.5 * rarity) * gate, depth)
    return raw, {"d": d, "r": r, "z": z, "novelty": novelty, "rarity": rarity}


def score_offer(offer: Offer, history: list[float], cfg: ScoringConfig | None = None,
                now: float | None = None) -> Score:
    """Score an offer given previously observed prices for the same product (excluding this one)."""
    cfg = cfg or ScoringConfig()
    now = now or time.time()
    price = offer.price
    bonus, signal_reasons = _signals(offer, cfg, now)

    if price is None:
        v = min(bonus, 60)
        return Score(v, tier_for(v, cfg), "signals", signal_reasons or ["no price found"])

    hist = [h for h in history if h and h > 0]
    reasons: list[str] = []
    typical = low = None
    record_low = False

    if len(hist) >= cfg.min_history_points:
        basis = "history"
        typical = statistics.median(hist)
        low = percentile(hist, 0.10)
        mad = statistics.median(abs(h - typical) for h in hist)
        spread = max(mad * 1.4826, 0.05 * typical)
        raw, f = _from_stats(price, typical, low, spread)
        record_low = price < min(hist)
        if f["d"] > 0:
            reasons.append(f"{f['d']:.0%} below its typical ${typical:,.2f} ({len(hist)} past prices)")
        if f["r"] < 1:
            reasons.append(f"{1 - f['r']:.0%} under its usual sale low ${low:,.2f}")
        if f["z"] >= 3:
            reasons.append(f"{f['z']:.1f}σ drop — very unusual for this item")
        prior_min = min(hist)
    elif offer.typical_price:
        basis = "source-stats"
        typical = offer.typical_price
        low = offer.low_price or typical * 0.85
        raw, f = _from_stats(price, typical, low, 0.08 * typical)
        record_low = offer.low_price is not None and price < offer.low_price
        reasons.append(f"{f['d']:.0%} below its average ${typical:,.2f}")
        prior_min = offer.low_price or low
    elif offer.reference_price and offer.reference_price > price:
        basis = "reference"
        d = 1 - price / offer.reference_price
        raw = _clamp((d - 0.20) / 0.55) * _clamp(offer.reference_trust)
        typical = offer.reference_price
        reasons.append(f"{d:.0%} off claimed ${offer.reference_price:,.2f} (no price history yet)")
        prior_min = None
    else:
        basis = "signals"
        raw, prior_min = 0.0, None

    value = round(100 * raw) + bonus
    if basis == "signals":
        value = min(value, 75)   # can't be a confirmed steal on vibes alone
    measured = basis in ("history", "source-stats")   # never trust a claimed "was" price for this
    glitch = bool(
        (measured and prior_min and price <= cfg.glitch_vs_low * prior_min)
        or (measured and typical and price <= cfg.glitch_vs_typical * typical)
        # a "price mistake" title only counts if measured data doesn't say the price is normal
        or (any(k in offer.title.lower() for k in ("price mistake", "price error", "pricing error", "glitch"))
            and not (measured and typical and price > 0.7 * typical))
    )
    if glitch and price > 0:
        value = max(value, cfg.tiers["steal"] + 5)
        reasons.insert(0, "possible price glitch — these get cancelled or fixed fast")
    if record_low and basis == "history":
        reasons.insert(0, "new all-time low")
    value = int(_clamp(value, 0, 100))
    discount = (1 - price / typical) if typical else None
    return Score(value, tier_for(value, cfg), basis, reasons + signal_reasons,
                 typical_price=typical, low_price=low, discount=discount,
                 record_low=record_low, glitch_suspect=glitch and price > 0)
