"""Best Buy's official Products API (free key: https://developer.bestbuy.com).

Best Buy's `regularPrice` is a real shelf price rather than an inflated MSRP,
so we trust it more than most reference prices.
"""

from __future__ import annotations

from urllib.parse import quote

from ..models import Offer
from .base import Source

FIELDS = "sku,name,salePrice,regularPrice,percentSavings,url,image,onlineAvailability,categoryPath.name"


class BestBuySource(Source):
    type = "bestbuy"
    default_interval = 300

    def __init__(self, name: str, api_key: str, searches: list[str] | None = None,
                 min_percent_savings: int = 30, page_size: int = 100, **kw):
        super().__init__(name, **kw)
        if not api_key:
            raise ValueError(f"source {name!r}: api_key is required (free at developer.bestbuy.com)")
        self.api_key = api_key
        self.searches = searches or [""]
        self.min_percent_savings = min_percent_savings
        self.page_size = page_size

    def build_url(self, search: str) -> str:
        terms = [f"search={w}" for w in search.split()] if search else []
        terms += ["onSale=true", f"percentSavings>={self.min_percent_savings}"]
        filt = quote("&".join(terms), safe="=&")
        return (f"https://api.bestbuy.com/v1/products({filt})?apiKey={self.api_key}&format=json"
                f"&show={FIELDS}&pageSize={self.page_size}&sort=percentSavings.dsc")

    def product_to_offer(self, p: dict) -> Offer | None:
        price = p.get("salePrice")
        if not price:
            return None
        cats = p.get("categoryPath") or []
        return Offer(
            source=self.name,
            retailer="Best Buy",
            title=p.get("name", ""),
            url=p.get("url") or f"https://www.bestbuy.com/site/{p.get('sku')}.p",
            price=float(price),
            reference_price=float(p["regularPrice"]) if p.get("regularPrice") else None,
            reference_trust=0.95,
            product_id=str(p.get("sku")),
            category=(cats[-1].get("name") or "").lower() if cats else None,
            in_stock=p.get("onlineAvailability"),
            image=p.get("image"),
        )

    def fetch(self) -> list[Offer]:
        offers = []
        for s in self.searches:
            data = self.http.get_json(self.build_url(s))
            offers.extend(o for o in map(self.product_to_offer, data.get("products", [])) if o)
        return offers

    def live_price(self, offer: Offer) -> float | None:
        """Exact current price for one SKU (used to verify suspected glitches)."""
        if not offer.product_id:
            return None
        data = self.http.get_json(f"https://api.bestbuy.com/v1/products/{offer.product_id}.json"
                                  f"?apiKey={self.api_key}&show=salePrice,onlineAvailability")
        if data.get("onlineAvailability") is False:
            return None
        return float(data["salePrice"]) if data.get("salePrice") else None
