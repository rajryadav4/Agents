"""DealHawk's own price API — a self-hosted take on services like PriceAPI.

    lookup(url)      live price of one product page + its price history + Steal Score
    search(query)    the same product across many stores, cheapest first

Every price it reads is stored, so DealHawk builds its own price database over
time (that history is what makes the Steal Score work).

Exposed three ways:
    dashboard        🔎 Check price / Compare stores in the top bar
    HTTP JSON API    GET /api/price?url=…        GET /api/search?q=…&stores=walmart,bestbuy
    command line     dealhawk price <url>         dealhawk search "macbook air"

Prices are read from structured page data (JSON-LD, Next.js/embedded state,
Shopify JSON, Amazon HTML), falling back to a headless browser for JS-heavy
pages when Playwright is installed. Results are cached for a few minutes so
repeated calls don't hammer the stores.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from .models import Offer, normalize_title, slug
from .retailers import detect_retailer
from .scoring import ScoringConfig, score_offer
from .sources.base import FetchError, Http
from .sources.harvest import harvest_products
from .sources.pagewatch import extract_product
from .sources.retail import RETAILERS, BrowserFetcher, RetailSource, _polite_wait

log = logging.getLogger("dealhawk.priceapi")

# stores searched when none are given: ones whose search pages usually render server-side
DEFAULT_STORES = ["walmart", "bestbuy", "newegg", "costco", "homedepot", "ebay", "bhphoto", "microcenter"]
BROWSER_STORES = {k for k, v in RETAILERS.items() if v.get("browser")}


def browser_available() -> bool:
    try:
        import playwright  # noqa: F401  (availability check)
        return True
    except ImportError:
        return False


class PriceReader:
    """Read {name, price, id, in_stock, method} from one product link."""

    def __init__(self, http: Http | None = None, browser_fallback: bool = True, min_gap_s: float = 3.0):
        self.http = http or Http(retries=1)
        self.browser = BrowserFetcher() if browser_fallback and browser_available() else None
        self.min_gap_s = min_gap_s

    def read(self, url: str) -> dict:
        _polite_wait(urlparse(url).hostname or "", self.min_gap_s, self.http.sleep)
        if "/products/" in url:                                  # Shopify stores expose product JSON
            try:
                data = self.http.get_json(url.split("?")[0].rstrip("/") + ".json").get("product") or {}
                vid = url.split("variant=")[1].split("&")[0] if "variant=" in url else None
                vs = data.get("variants") or []
                v = next((x for x in vs if str(x.get("id")) == vid), vs[0] if vs else None)
                if v and v.get("price"):
                    return {"name": data.get("title"), "price": float(v["price"]), "id": str(v.get("id")),
                            "in_stock": v.get("available"), "method": "shopify-json"}
            except (FetchError, ValueError, KeyError, TypeError):
                pass
        error: FetchError | None = None
        try:
            page = self.http.get_text(url, {"Accept": "text/html,application/xhtml+xml",
                                            "Accept-Language": "en-US,en;q=0.9"})
            found = self.from_page(page, url)
            if found:
                return {**found, "method": "page"}
            error = FetchError("no price found on the page — it may need a real browser (pip install playwright) "
                               "or the store hides prices from bots")
        except FetchError as e:
            error = e
        if self.browser is not None:
            try:
                found = self.from_page(self.browser(url), url)
                if found:
                    return {**found, "method": "browser"}
                error = FetchError("no price found, even in a real browser")
            except FetchError as e:
                error = e
        raise error or FetchError("couldn't read the price")

    @staticmethod
    def from_page(page: str, url: str) -> dict | None:
        p = extract_product(page)
        if p and p.get("price"):
            return {"name": p.get("name"), "price": p["price"], "id": p.get("sku"), "in_stock": p.get("in_stock")}
        products = harvest_products(page, url)
        if len(products) == 1:
            q = products[0]
            return {"name": q["name"], "price": q["price"], "id": q["id"], "in_stock": q["in_stock"]}
        return None


def _url_id(url: str) -> str:
    return hashlib.sha1(url.split("?")[0].encode()).hexdigest()[:16]


class PriceAPI:
    def __init__(self, store, reader: PriceReader | None = None, scoring: ScoringConfig | None = None,
                 cache_ttl: float = 300, browser_fallback: bool = True, http_factory=None, clock=time.time):
        self.store = store
        self.reader = reader or PriceReader(browser_fallback=browser_fallback)
        self.scoring = scoring or ScoringConfig()
        self.cache_ttl = cache_ttl
        self.http_factory = http_factory or (lambda: Http(retries=1))
        self.browser_ok = browser_fallback and browser_available()
        self.clock = clock
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    # ---- cache ----
    def _cached(self, key: tuple) -> dict | None:
        with self._lock:
            hit = self._cache.get(key)
        if hit and self.clock() - hit[0] < self.cache_ttl:
            return {**hit[1], "cached": True}
        return None

    def _remember(self, key: tuple, value: dict) -> dict:
        with self._lock:
            self._cache[key] = (self.clock(), value)
            if len(self._cache) > 500:
                for k in sorted(self._cache, key=lambda k: self._cache[k][0])[:100]:
                    self._cache.pop(k, None)
        return {**value, "cached": False}

    # ---- shared: score + store a price we just read ----
    def _describe(self, o: Offer) -> dict:
        history = self.store.history(o.key)
        s = score_offer(o, history, self.scoring, self.clock())
        self.store.record(o, self.clock())
        points = self.store.price_points(o.key, 120)
        prices = [p for _, p in points]
        return {
            "title": o.title, "retailer": o.retailer, "url": o.url, "price": o.price, "currency": "USD",
            "was_price": o.reference_price, "in_stock": o.in_stock, "product_id": o.product_id,
            "checked_at": self.clock(),
            "score": {"value": s.value, "tier": s.tier, "basis": s.basis, "reasons": s.reasons[:4],
                      "glitch_suspect": s.glitch_suspect, "record_low": s.record_low},
            "history": {"count": len(prices), "low": min(prices) if prices else None,
                        "high": max(prices) if prices else None,
                        "typical": s.typical_price if s.basis == "history" else None, "points": points},
        }

    # ---- lookup ----
    def lookup(self, url: str, fresh: bool = False) -> dict:
        from .tracking import validate_url
        url = validate_url(url)
        key = ("lookup", url.split("#")[0])
        if not fresh and (hit := self._cached(key)):
            return hit
        p = self.reader.read(url)
        title = p.get("name") or url
        retailer = detect_retailer(url, title)
        o = Offer("price-api", retailer, title, url, p["price"], product_id=str(p["id"]) if p.get("id") else
                  _url_id(url), in_stock=p.get("in_stock"))
        return self._remember(key, {**self._describe(o), "method": p.get("method")})

    # ---- search ----
    def search(self, query: str, stores: list[str] | None = None, limit: int = 40, fresh: bool = False) -> dict:
        from .tracking import explain
        query = " ".join((query or "").split())
        if len(query) < 2:
            raise ValueError("type what you're looking for, e.g. \"macbook air m3\"")
        wanted = [s.strip().lower() for s in (stores or DEFAULT_STORES) if s.strip()]
        unknown = [s for s in wanted if s not in RETAILERS or not RETAILERS[s]["search"]]
        if unknown:
            raise ValueError(f"no search for {unknown}; choose from "
                             f"{sorted(k for k, v in RETAILERS.items() if v['search'])}")
        key = ("search", query.lower(), tuple(sorted(wanted)))
        if not fresh and (hit := self._cached(key)):
            return hit
        words = normalize_title(query).split()
        status: dict[str, dict] = {}

        def one(name: str) -> list[Offer]:
            if name in BROWSER_STORES and not self.browser_ok:
                status[name] = {"ok": False, "count": 0,
                                "error": "needs a real browser — pip install playwright && playwright install chromium"}
                return []
            try:
                src = RetailSource(f"search-{name}", retailer=name, pages=[], search=[query],
                                   http=self.http_factory(), browser=name in BROWSER_STORES, min_gap_s=2.0)
                found = src.fetch()
            except (FetchError, ValueError) as e:
                status[name] = {"ok": False, "count": 0, "error": explain(e)}
                return []
            # search pages also list ads/recommendations: keep titles containing every query word
            relevant = [o for o in found if o.price and all(w in normalize_title(o.title).split() or
                                                            w in normalize_title(o.title) for w in words)]
            status[name] = {"ok": True, "count": len(relevant)}
            return relevant

        with ThreadPoolExecutor(max_workers=min(8, len(wanted) or 1)) as pool:
            offers = [o for batch in pool.map(one, wanted) for o in batch]
        results = sorted((self._describe(o) for o in offers), key=lambda r: r["price"])[:limit]
        for r in results:
            r["history"].pop("points", None)          # keep search responses small
        return self._remember(key, {"query": query, "stores": status, "count": len(results),
                                    "cheapest": results[0] if results else None, "results": results})


def history_key(retailer: str, product_id: str) -> str:
    return f"{slug(retailer)}:{product_id}"
