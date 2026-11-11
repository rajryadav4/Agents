"""Instant re-check of suspected price glitches, straight from the retailer.

When something looks like a pricing error, minutes matter: retailers fix
glitches fast. Before alerting we re-read the live price — from the source's
own API when it has one (Best Buy SKU lookup), otherwise from the product
page's schema.org markup — and:

  ✅ confirmed     live price still ≤ what we saw → STEAL, sent at once (skips quiet hours / caps)
  📉 lower         even cheaper now → same, with the new price
  ❌ corrected     price already fixed → re-score at the real price (usually no alert)
  ⚠️ unverifiable  page blocked / no price markup → still sent, flagged as unverified

Note: in the US a retailer usually isn't obliged to honour a pricing error —
most terms of sale let them cancel. Verification tells you the price is live
*right now*, so you can check out before it's fixed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .models import Offer
from .sources.base import FetchError, Http
from .sources.pagewatch import extract_product

log = logging.getLogger("dealhawk.verify")


@dataclass
class VerifyConfig:
    enabled: bool = True
    tiers: list[str] | None = None        # also verify these tiers (default: only suspected glitches)
    tolerance: float = 0.02               # live price may be 2% above what we saw and still "confirmed"
    timeout_s: float = 8.0
    max_per_hour: int = 60
    bypass_quiet_hours: bool = True       # a confirmed glitch is worth waking up for


@dataclass
class VerifyResult:
    status: str                            # confirmed | lower | corrected | unverifiable
    live_price: float | None = None
    method: str = ""                       # api | page
    checked_at: float = 0.0
    detail: str = ""

    @property
    def live(self) -> bool:
        return self.status in ("confirmed", "lower")


def _fast_http(timeout: float) -> Http:
    from .sources.base import urllib_fetch
    return Http(fetcher=lambda u, h, b: urllib_fetch(u, h, b, timeout=timeout), retries=0)


class PriceVerifier:
    def __init__(self, cfg: VerifyConfig, sources: dict | None = None, http: Http | None = None, clock=time.time):
        self.cfg = cfg
        self.sources = sources or {}          # name → Source (for API-based verification)
        self.http = http or _fast_http(cfg.timeout_s)
        self.clock = clock
        self._recent: list[float] = []

    def wants(self, tier: str, glitch: bool) -> bool:
        return self.cfg.enabled and (glitch or tier in (self.cfg.tiers or []))

    def _budget_ok(self) -> bool:
        now = self.clock()
        self._recent = [t for t in self._recent if t > now - 3600]
        if len(self._recent) >= self.cfg.max_per_hour:
            return False
        self._recent.append(now)
        return True

    def live_price(self, offer: Offer) -> tuple[float | None, str, str]:
        src = self.sources.get(offer.source)
        if src is not None and hasattr(src, "live_price"):
            try:
                p = src.live_price(offer)
                if p is not None:
                    return p, "api", ""
            except FetchError as e:
                log.info("API verify failed for %s: %s", offer.title[:40], e)
        try:
            page = self.http.get_text(offer.url, {"Accept": "text/html,application/xhtml+xml"})
        except FetchError as e:
            return None, "page", f"page fetch failed ({e.status or 'network'})"
        product = extract_product(page)
        if not product or product.get("price") is None:
            return None, "page", "no machine-readable price on the page (often bot protection)"
        if product.get("in_stock") is False:
            return None, "page", "page says out of stock"
        return product["price"], "page", ""

    def verify(self, offer: Offer) -> VerifyResult:
        now = self.clock()
        if offer.price is None:
            return VerifyResult("unverifiable", checked_at=now, detail="no price to compare")
        if not self._budget_ok():
            return VerifyResult("unverifiable", checked_at=now, detail="verification budget exhausted")
        live, method, detail = self.live_price(offer)
        if live is None:
            return VerifyResult("unverifiable", None, method, now, detail)
        if live < offer.price * (1 - self.cfg.tolerance):
            return VerifyResult("lower", live, method, now)
        if live <= offer.price * (1 + self.cfg.tolerance):
            return VerifyResult("confirmed", live, method, now)
        return VerifyResult("corrected", live, method, now, f"now ${live:,.2f}")
