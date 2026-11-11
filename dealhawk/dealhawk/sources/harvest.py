"""Pull every product + price out of a retailer page, whatever the site.

Big retailers embed machine-readable data in their pages, in one of a few shapes:

  • schema.org JSON-LD  (<script type="application/ld+json">: Product, ItemList, Offer)
  • Next.js / app state (<script id="__NEXT_DATA__">, window.__PRELOADED_STATE__ = {...}, etc.)
  • plain JSON islands  (<script type="application/json">)

`harvest_products` parses all of them and walks the JSON looking for
"product-shaped" objects (a name + a price, optionally a was-price / URL / id).
That one heuristic covers deal, clearance, search and product pages on most
stores, including Walmart (`priceInfo.currentPrice.price`), Target
(`price.current_retail`), Home Depot, Best Buy, Costco, Newegg, B&H, Macy's ...
Site-specific regex fallbacks handle Amazon's HTML.
"""

from __future__ import annotations

import html as htmllib
import json
import re
from typing import Any, Iterator
from urllib.parse import urljoin

_SCRIPT = re.compile(r"<script([^>]*)>(.*?)</script>", re.I | re.S)
_ASSIGN = re.compile(r"window\.(?:__[A-Z_]+__|[A-Za-z]+State)\s*=\s*", re.S)

NAME_KEYS = ("name", "title", "productName", "product_name", "displayName", "itemName")
PRICE_KEYS = ("currentPrice", "current_retail", "salePrice", "offerPrice", "priceAmount", "finalPrice",
              "sellingPrice", "price", "lowPrice", "current_price", "priceValue")
WAS_KEYS = ("wasPrice", "listPrice", "regularPrice", "reg_retail", "comparisonPrice", "strikethroughPrice",
            "originalPrice", "compareAtPrice", "msrp", "was_price", "list_price", "highPrice")
URL_KEYS = ("url", "canonicalUrl", "productUrl", "canonical_url", "buy_url", "link", "href", "productPageUrl")
ID_KEYS = ("sku", "usItemId", "tcin", "asin", "itemId", "productId", "product_id", "id", "gtin13", "upc", "mpn")


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, str):
        m = re.search(r"\d[\d,]*(?:\.\d+)?", v)
        if m and len(v) < 40:
            try:
                f = float(m.group(0).replace(",", ""))
                return f if f > 0 else None
            except ValueError:
                return None
    if isinstance(v, dict):   # {"price": 12.3} / {"amount": ...} / {"currentPrice": {"price": ...}}
        for k in ("price", "amount", "value", "current_retail", "priceAmount", "currentPrice"):
            if k in v and not isinstance(v[k], (list,)):
                n = _num(v[k])
                if n is not None:
                    return n
    return None


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _name(d: dict) -> str | None:
    n = _first(d, NAME_KEYS)
    if isinstance(n, dict):   # Target: item.product_description.title
        n = _first(n, NAME_KEYS)
    if not isinstance(n, str):
        for sub in ("item", "product", "product_description"):
            if isinstance(d.get(sub), dict):
                n = _name(d[sub])
                if n:
                    break
    if isinstance(n, str) and 3 <= len(n.strip()) <= 300:
        return htmllib.unescape(n.strip())
    return None


def _price_block(d: dict) -> dict:
    """Merge the object with a nested price container (priceInfo / price / offers)."""
    merged = dict(d)
    for k in ("priceInfo", "price", "prices", "pricing", "offers", "offer", "priceRange"):
        v = d.get(k)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            v = v[0]
        if isinstance(v, dict):
            for kk, vv in v.items():
                merged.setdefault(kk, vv)
            if k in ("price", "offers") and _num(v) is not None:
                merged["__price"] = _num(v)
    return merged


def product_from(d: dict, base_url: str = "") -> dict | None:
    name = _name(d)
    if not name:
        return None
    m = _price_block(d)
    price = m.get("__price")
    if price is None:
        for k in PRICE_KEYS:
            if k in m:
                price = _num(m[k])
                if price is not None:
                    break
    if price is None or price > 100_000:
        return None
    was = None
    for k in WAS_KEYS:
        if k in m:
            was = _num(m[k])
            if was:
                break
    url = _first(d, URL_KEYS)
    pid = _first(d, ID_KEYS)
    avail = str(m.get("availability") or m.get("availabilityStatus") or "").lower()
    return {
        "name": name,
        "price": price,
        "was": was if was and was > price else None,
        "url": urljoin(base_url, url) if isinstance(url, str) else None,
        "id": str(pid) if isinstance(pid, (str, int)) and not isinstance(pid, bool) else None,
        "in_stock": None if not avail else not any(x in avail for x in ("outofstock", "out_of_stock", "sold out",
                                                                        "unavailable", "discontinued")),
    }


def _walk(node: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 40:
        return
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, depth + 1)


def _json_blobs(page: str) -> Iterator[Any]:
    for attrs, body in _SCRIPT.findall(page):
        body = body.strip()
        if not body:
            continue
        a = attrs.lower()
        if "ld+json" in a or "application/json" in a or "__next_data__" in a:
            try:
                yield json.loads(htmllib.unescape(body) if "ld+json" in a else body)
            except json.JSONDecodeError:
                continue
        else:
            m = _ASSIGN.search(body)
            if m:
                try:
                    obj, _ = json.JSONDecoder().raw_decode(body[m.end():])
                    yield obj
                except json.JSONDecodeError:
                    continue


def _amazon_html(page: str) -> list[dict]:
    t = re.search(r'id="productTitle"[^>]*>\s*(.*?)\s*<', page, re.S)
    p = re.search(r'class="a-price[^"]*"[^>]*>\s*<span class="a-offscreen">\s*\$([\d,]+\.\d\d)', page)
    if not (t and p):
        return []
    was = re.search(r'a-text-price[^>]*>\s*<span class="a-offscreen">\s*\$([\d,]+\.\d\d)', page)
    asin = re.search(r'name="ASIN"\s+value="([A-Z0-9]{10})"|/dp/([A-Z0-9]{10})', page)
    price = float(p.group(1).replace(",", ""))
    was_v = float(was.group(1).replace(",", "")) if was else None
    return [{"name": htmllib.unescape(t.group(1)), "price": price,
             "was": was_v if was_v and was_v > price else None, "url": None,
             "id": (asin.group(1) or asin.group(2)) if asin else None, "in_stock": None}]


def harvest_products(page: str, base_url: str = "") -> list[dict]:
    """Every distinct product found on the page: [{name, price, was, url, id, in_stock}]."""
    found: dict[tuple, dict] = {}
    for blob in _json_blobs(page):
        for d in _walk(blob):
            p = product_from(d, base_url)
            if p:
                k = str(p["id"] or p["url"] or p["name"]).lower()
                prev = found.get(k)
                # keep the richest record for each product
                if prev is None or (p["was"] and not prev["was"]) or (p["url"] and not prev["url"]):
                    found[k] = p if prev is None else {**prev, **{x: y for x, y in p.items() if y}}
    if not found:
        for p in _amazon_html(page):
            found[p["name"]] = p
    return list(found.values())
