"""Core data types shared by every part of DealHawk."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "at", "in", "on", "of", "to",
    "new", "free", "shipping", "deal", "sale", "off", "w", "ymmv",
}


def normalize_title(title: str) -> str:
    """Lower-case, strip prices/punctuation and filler words.

    Used both for keyword matching and for deriving a stable product id when a
    source doesn't give us a SKU, so two posts about the same product land on
    the same price history.
    """
    t = title.lower()
    t = re.sub(r"\$\s?\d[\d,]*(?:\.\d+)?", " ", t)       # prices
    t = re.sub(r"\d+%\s*off", " ", t)                      # "40% off"
    t = re.sub(r"[\[\](){}|,;:!?\"'®™]", " ", t)
    t = re.sub(r"[^a-z0-9.+\-/ ]", " ", t)
    return " ".join(w for w in t.split() if w not in _STOPWORDS)


def title_fingerprint(title: str) -> str:
    tokens = sorted(set(normalize_title(title).split()))
    return hashlib.sha1(" ".join(tokens).encode()).hexdigest()[:16]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "unknown"


@dataclass
class Offer:
    """One product at one price at one retailer, as seen by a source."""

    source: str                       # which source produced it, e.g. "walmart"
    retailer: str                     # "Amazon", "Walmart", ...
    title: str
    url: str
    price: float | None               # current price; None if we couldn't parse one
    currency: str = "USD"
    reference_price: float | None = None   # "was"/"list"/"regular" price the seller claims
    reference_trust: float = 0.8           # 0..1 — how much to believe reference_price
    product_id: str | None = None          # SKU / ASIN / handle when the source has one
    category: str | None = None
    in_stock: bool | None = None
    image: str | None = None
    community_score: int | None = None     # upvotes / thumbs
    posted_at: float | None = None         # epoch seconds
    typical_price: float | None = None     # source-supplied stats (e.g. Keepa 90-day avg)
    low_price: float | None = None         # source-supplied historic low
    tags: list[str] = field(default_factory=list)
    tracked_id: int | None = None          # set when this is a product you asked DealHawk to track
    # where the price comes from:
    #   first-party   read directly from the retailer (its page, official API, or store JSON)
    #   price-tracker a price-history service that records the retailer's own prices (Keepa)
    #   community     a person's post — only a lead; it must be confirmed on the store page first
    trust: str = "first-party"

    @property
    def key(self) -> str:
        pid = self.product_id or title_fingerprint(self.title)
        return f"{slug(self.retailer)}:{pid}"


@dataclass
class Score:
    value: int                         # 0..100
    tier: str                          # steal | great | good | meh
    basis: str                         # history | source-stats | reference | signals
    reasons: list[str] = field(default_factory=list)
    typical_price: float | None = None
    low_price: float | None = None
    discount: float | None = None      # fraction off vs typical/reference
    record_low: bool = False
    glitch_suspect: bool = False
    rule_value: int | None = None      # the formula's score before learning / Claude adjusted it
    llm: dict | None = None            # Claude's verdict, when it was consulted


@dataclass
class Deal:
    offer: Offer
    score: Score
    watch: str                          # name of the watch rule that matched
    previous_alert_price: float | None = None
    explore: bool = False               # sent to learn your taste, below your usual threshold
    features: dict | None = None        # what the learner saw (stored so feedback can train on it)
    id: int | None = None               # row id in the deals log, once logged
    links: dict | None = None           # feedback URLs: good / bad / go (click-through)
    verification: object | None = None  # verify.VerifyResult when the price was re-checked live

    @property
    def open_url(self) -> str:
        return (self.links or {}).get("go") or self.offer.url

    @property
    def headline(self) -> str:
        o, s = self.offer, self.score
        price = f"${o.price:,.2f}" if o.price is not None else "?"
        prefix = {"steal": "🚨 STEAL", "great": "🔥 Great", "good": "👍 Good"}.get(s.tier, "Deal")
        v = self.verification
        if s.glitch_suspect and v is not None and getattr(v, "live", False):
            prefix = "⚡✅ VERIFIED PRICE GLITCH"
        elif s.glitch_suspect:
            prefix = "⚡ POSSIBLE PRICE GLITCH"
        elif self.explore:
            prefix = "🧪 Learning — good deal?"
        return f"{prefix} ({s.value}/100): {price} at {o.retailer}"

    @property
    def body(self) -> str:
        o, s = self.offer, self.score
        lines = [o.title]
        ref = s.typical_price or o.reference_price
        if ref and o.price is not None:
            lines.append(f"Usually ~${ref:,.2f} → now ${o.price:,.2f} ({(1 - o.price / ref):.0%} off)")
        if s.low_price:
            lines.append(f"Usual sale low: ${s.low_price:,.2f}" + ("  ← NEW RECORD LOW" if s.record_low else ""))
        if s.reasons:
            lines.append("Why: " + "; ".join(s.reasons[:3]))
        lines.append(f"Watch: {self.watch} · via {o.source}")
        return "\n".join(lines)
