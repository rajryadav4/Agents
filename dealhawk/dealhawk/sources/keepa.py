"""Amazon via Keepa (https://keepa.com/#!api — paid, ~€19/mo for the entry tier).

Scraping Amazon directly is against its terms and gets blocked fast. Keepa has
tracked Amazon prices for years and its /deal endpoint returns products whose
price just dropped relative to their own averages — exactly what we want.

NOTE: written against Keepa's documented response format; verify with your key
(`dealhawk once --source amazon-keepa -v`) before relying on it.
"""

from __future__ import annotations

import json
from urllib.parse import quote

from ..models import Offer
from .base import Source

PRICE_TYPES = {"amazon": 0, "new": 1, "used": 2, "warehouse": 9, "lightning": 8}
DOMAINS = {"com": 1, "co.uk": 2, "de": 3, "fr": 4, "co.jp": 5, "ca": 6, "it": 8, "es": 9, "in": 10, "com.mx": 11}
_RANGE_90D = 3   # avg/deltaPercent index: 0=day 1=week 2=month 3=90 days


def _cents(v) -> float | None:
    return v / 100 if isinstance(v, (int, float)) and v > 0 else None


def _decode_image(img) -> str | None:
    # Keepa sends the image filename as a list of char codes in deal objects
    if isinstance(img, list):
        try:
            img = "".join(map(chr, img))
        except (TypeError, ValueError):
            return None
    return f"https://m.media-amazon.com/images/I/{img}" if img else None


class KeepaSource(Source):
    type = "keepa"
    default_interval = 300
    trust = "price-tracker"   # Keepa records Amazon's own listed prices

    def __init__(self, name: str, api_key: str, domain: str = "com", price_type: str = "new",
                 min_drop_percent: int = 40, title_search: str | None = None,
                 include_categories: list[int] | None = None, only_lowest: bool = False, **kw):
        super().__init__(name, **kw)
        if not api_key:
            raise ValueError(f"source {name!r}: api_key is required")
        self.api_key = api_key
        self.domain = domain
        self.domain_id = DOMAINS[domain]
        self.price_type = PRICE_TYPES[price_type]
        self.selection = {
            "page": 0,
            "domainId": self.domain_id,
            "priceTypes": [self.price_type],
            "deltaPercentRange": [min_drop_percent, 100],
            "dateRange": 0,               # changed within the last day
            "isFilterEnabled": True,
            "isRangeEnabled": True,
            "filterErotic": True,
            "sortType": 4,                # by percentage delta
        }
        if title_search:
            self.selection["titleSearch"] = title_search
        if include_categories:
            self.selection["includeCategories"] = include_categories
        if only_lowest:
            self.selection["isLowest"] = True

    def deal_to_offer(self, d: dict) -> Offer | None:
        pt = self.price_type
        current = d.get("current") or []
        price = _cents(current[pt]) if len(current) > pt else None
        if price is None:
            return None
        avg = d.get("avg") or []
        typical = None
        if len(avg) > _RANGE_90D and len(avg[_RANGE_90D]) > pt:
            typical = _cents(avg[_RANGE_90D][pt])
        # lowest of the day/week/month/90d averages — a rough "usual low"
        lows = [_cents(a[pt]) for a in avg if isinstance(a, list) and len(a) > pt]
        lows = [x for x in lows if x]
        asin = d.get("asin", "")
        return Offer(
            source=self.name,
            retailer="Amazon",
            title=d.get("title", asin),
            url=f"https://www.amazon.{self.domain}/dp/{asin}",
            price=price,
            product_id=asin,
            typical_price=typical,
            low_price=min(lows) if lows else None,
            image=_decode_image(d.get("image")),
            tags=["keepa"],
            trust=self.trust,
        )

    def fetch(self) -> list[Offer]:
        sel = quote(json.dumps(self.selection, separators=(",", ":")))
        data = self.http.get_json(f"https://api.keepa.com/deal?key={self.api_key}&selection={sel}")
        deals = (data.get("deals") or {}).get("dr") or []
        return [o for o in map(self.deal_to_offer, deals) if o]
