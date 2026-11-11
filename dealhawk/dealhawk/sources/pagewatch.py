"""Watch specific product pages on any store, via schema.org structured data.

Most retailers embed `<script type="application/ld+json">` Product markup
(for Google Shopping). We read the price from it on every poll, so a page you
care about builds its own price history and a sudden drop scores high.

Big-box sites (Walmart, Target, Amazon) run aggressive bot protection and may
return 403/captcha pages; prefer their APIs/feeds, keep intervals long, and
respect each site's terms.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from ..models import Offer
from ..retailers import detect_retailer
from .base import FetchError, Source, log

_LD = re.compile(r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""", re.I | re.S)
_META_PRICE = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)=["'](?:product:price:amount|og:price:amount|price)["'][^>]+content=["']([\d.,]+)["']""",
    re.I,
)
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _walk(node: Any):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for k in ("@graph", "mainEntity", "itemListElement", "hasVariant"):
            if k in node:
                yield from _walk(node[k])


def _is_product(d: dict) -> bool:
    t = d.get("@type")
    return (t if isinstance(t, str) else " ".join(t or [])).lower().find("product") >= 0


def _num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def extract_product(page: str) -> dict | None:
    """Return {name, price, sku, in_stock, image} from a product page, or None."""
    for block in _LD.findall(page):
        try:
            data = json.loads(html.unescape(block.strip()))
        except json.JSONDecodeError:
            continue
        for d in _walk(data):
            if not _is_product(d):
                continue
            offers = d.get("offers")
            offers = offers[0] if isinstance(offers, list) and offers else offers
            if not isinstance(offers, dict):
                continue
            price = _num(offers.get("price") or offers.get("lowPrice"))
            if price is None and isinstance(offers.get("priceSpecification"), dict):
                price = _num(offers["priceSpecification"].get("price"))
            if price is None:
                continue
            avail = str(offers.get("availability", "")).lower()
            image = d.get("image")
            image = image[0] if isinstance(image, list) and image else image
            return {
                "name": d.get("name"),
                "price": price,
                "sku": d.get("sku") or d.get("gtin13") or d.get("gtin12") or d.get("mpn"),
                "in_stock": None if not avail else ("instock" in avail or "limitedavailability" in avail),
                "image": image if isinstance(image, str) else None,
            }
    # Next.js / embedded-state pages (Walmart, Target, ...): use the generic harvester
    from .harvest import harvest_products
    products = harvest_products(page)
    if len(products) >= 1:
        p = products[0]
        return {"name": p["name"], "price": p["price"], "sku": p["id"], "in_stock": p["in_stock"], "image": None}
    m = _META_PRICE.search(page)
    if m:
        t = _TITLE.search(page)
        return {"name": html.unescape(t.group(1).strip()) if t else None, "price": _num(m.group(1)),
                "sku": None, "in_stock": None, "image": None}
    return None


class PageWatchSource(Source):
    type = "watch"
    default_interval = 900

    def __init__(self, name: str, urls: list, **kw):
        super().__init__(name, **kw)
        if not urls:
            raise ValueError(f"source {name!r}: needs at least one url")
        self.urls = [u if isinstance(u, dict) else {"url": u} for u in urls]

    def fetch(self) -> list[Offer]:
        offers = []
        for entry in self.urls:
            url = entry["url"]
            try:
                page = self.http.get_text(url, {"Accept": "text/html,application/xhtml+xml"})
            except FetchError as e:
                log.warning("watch %s: %s", url, e)
                continue
            p = extract_product(page)
            if not p or p["price"] is None:
                log.warning("watch %s: no price found (blocked or no structured data?)", url)
                continue
            title = entry.get("name") or p["name"] or url
            offers.append(Offer(
                source=self.name,
                retailer=detect_retailer(url, title),
                title=title,
                url=url,
                price=p["price"],
                product_id=str(p["sku"]) if p["sku"] else url.split("?")[0],
                in_stock=p["in_stock"],
                image=p["image"],
            ))
        return offers
