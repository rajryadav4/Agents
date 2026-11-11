"""Your preferences: which deals you actually want to hear about."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Offer, Score, normalize_title


@dataclass
class Watch:
    name: str
    keywords: list[str] = field(default_factory=list)    # any-of; empty = everything
    exclude: list[str] = field(default_factory=list)     # none-of
    retailers: list[str] = field(default_factory=list)   # empty = any retailer
    categories: list[str] = field(default_factory=list)
    min_price: float | None = None
    max_price: float | None = None
    min_score: int | None = None                         # falls back to alerts.min_score
    min_discount: float | None = None                    # e.g. 0.5 = must be ≥50% under typical

    def __post_init__(self) -> None:
        self._kw = [_compile(k) for k in self.keywords]
        self._ex = [_compile(k) for k in self.exclude]
        self._retailers = {r.lower() for r in self.retailers}
        self._categories = {c.lower() for c in self.categories}


def _compile(term: str):
    """'re:<regex>' is a regex; anything else is a phrase whose words must all appear."""
    if term.startswith("re:"):
        rx = re.compile(term[3:], re.I)
        return lambda title, norm: bool(rx.search(title))
    words = normalize_title(term).split()
    return lambda title, norm: all(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", norm) for w in words)


def matches(watch: Watch, offer: Offer, score: Score | None, default_min_score: int) -> bool:
    """Does the offer satisfy the watch? With score=None, check everything except score thresholds."""
    norm = normalize_title(offer.title + " " + " ".join(offer.tags))
    if watch._kw and not any(k(offer.title, norm) for k in watch._kw):
        return False
    if any(k(offer.title, norm) for k in watch._ex):
        return False
    if watch._retailers and offer.retailer.lower() not in watch._retailers:
        return False
    if watch._categories and (offer.category or "").lower() not in watch._categories:
        return False
    if offer.price is not None:
        if watch.max_price is not None and offer.price > watch.max_price:
            return False
        if watch.min_price is not None and offer.price < watch.min_price:
            return False
    if score is None:
        return True
    if watch.min_discount is not None and (score.discount or 0) < watch.min_discount:
        return False
    threshold = watch.min_score if watch.min_score is not None else default_min_score
    return score.value >= threshold


def first_match(watches: list[Watch], offer: Offer, score: Score | None, default_min_score: int) -> Watch | None:
    for w in watches:
        if matches(w, offer, score, default_min_score):
            return w
    return None
