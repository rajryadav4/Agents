"""Any Shopify-powered store (thousands of brands) exposes /products.json publicly.

Because we poll the same SKUs repeatedly, DealHawk builds real price history
for every variant — the best input for the Steal Score.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..models import Offer
from .base import Source


def _f(v) -> float | None:
    try:
        f = float(v)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _tags(raw) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else (raw or [])
    return [t.strip() for t in items if isinstance(t, str) and t.strip()][:10]


class ShopifySource(Source):
    type = "shopify"
    default_interval = 900

    def __init__(self, name: str, stores: list, pages: int = 1, **kw):
        super().__init__(name, **kw)
        if not stores:
            raise ValueError(f"source {name!r}: needs at least one store")
        # each store is a URL string or {url, name}
        self.stores = [s if isinstance(s, dict) else {"url": s} for s in stores]
        self.pages = pages

    def products_to_offers(self, products: list[dict], base: str, store_name: str) -> list[Offer]:
        offers = []
        for p in products:
            image = (p.get("images") or [{}])[0].get("src")
            for v in p.get("variants", []):
                price = _f(v.get("price"))
                if price is None:
                    continue
                vt = v.get("title") or ""
                title = p.get("title", "") + ("" if vt in ("", "Default Title") else f" — {vt}")
                offers.append(Offer(
                    source=self.name,
                    retailer=store_name,
                    title=title,
                    url=f"{base}/products/{p.get('handle')}?variant={v.get('id')}",
                    price=price,
                    reference_price=_f(v.get("compare_at_price")),
                    reference_trust=0.5,   # compare-at prices are frequently inflated
                    product_id=f"{p.get('id')}-{v.get('id')}",
                    category=(p.get("product_type") or "").lower() or None,
                    in_stock=v.get("available"),
                    image=image,
                    tags=_tags(p.get("tags")),
                ))
        return offers

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        for store in self.stores:
            base = store["url"].rstrip("/")
            if not base.startswith("http"):
                base = "https://" + base
            name = store.get("name") or (urlparse(base).hostname or base).removeprefix("www.")
            for page in range(1, self.pages + 1):
                data = self.http.get_json(f"{base}/products.json?limit=250&page={page}")
                products = data.get("products", [])
                offers.extend(o for o in self.products_to_offers(products, base, name) if o.in_stock is not False)
                if len(products) < 250:
                    break
        return offers

    def live_price(self, offer: Offer) -> float | None:
        """Re-read one variant's price from the store (used to verify suspected glitches)."""
        base, _, query = offer.url.partition("?variant=")
        if not query:
            return None
        product = self.http.get_json(base + ".json").get("product", {})
        for v in product.get("variants", []):
            if str(v.get("id")) == query:
                return _f(v.get("price")) if v.get("available", True) else None
        return None
