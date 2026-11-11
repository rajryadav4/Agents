"""Scan the big retailers' own deal / clearance / search pages directly.

    - type: retailer
      retailer: walmart              # preset: known deal & clearance pages
      pages: [deals, clearance]      # preset page names, or full URLs
      search: ["macbook", "oled tv"] # also scan these search-result pages
      browser: false                 # true = render with headless Chromium (JS-heavy sites)
      interval: 300

Every page is run through the generic harvester (harvest.py), so new stores
work without code: give `retailer: other` and full URLs.

Reality check — read before turning this up:
  • Walmart, Target and Amazon run aggressive bot protection. Expect 403s or
    captcha pages, especially without `browser: true`, from data-centre IPs,
    or at short intervals. DealHawk backs off automatically when blocked.
  • Their terms of use restrict automated access. Keep intervals modest, scan
    the handful of pages you care about, and prefer official APIs/feeds
    (Keepa, Best Buy API, community feeds) where they exist.
  • Preset URLs are best-effort: retailers move pages. Check one with
    `dealhawk once --source <name> -v` and override with full URLs.
"""

from __future__ import annotations

import logging
import threading
import time
from urllib.parse import quote_plus

from ..models import Offer
from .base import FetchError, Source
from .harvest import harvest_products

log = logging.getLogger("dealhawk.retail")

RETAILERS: dict[str, dict] = {
    "walmart": {"name": "Walmart", "base": "https://www.walmart.com",
                "pages": {"deals": "/shop/deals", "flash": "/shop/deals/flash-deals",
                          "rollbacks": "/shop/deals/rollbacks", "clearance": "/shop/deals/clearance"},
                "search": "/search?q={q}&sort=price_low"},
    "target": {"name": "Target", "base": "https://www.target.com", "browser": True,
               "pages": {"deals": "/c/top-deals/-/N-4xw74", "clearance": "/c/clearance/-/N-5q0ga"},
               "search": "/s?searchTerm={q}"},
    "amazon": {"name": "Amazon", "base": "https://www.amazon.com", "browser": True,
               "pages": {"deals": "/deals", "outlet": "/outlet", "warehouse": "/Warehouse-Deals/b?node=10158976011"},
               "search": "/s?k={q}"},
    "bestbuy": {"name": "Best Buy", "base": "https://www.bestbuy.com",
                "pages": {"deal-of-the-day": "/site/misc/deal-of-the-day/pcmcat248000050016.c",
                          "top-deals": "/site/misc/deals/pcmcat1563299784494.c",
                          "open-box": "/site/misc/open-box/pcmcat1528819595254.c"},
                "search": "/site/searchpage.jsp?st={q}"},
    "costco": {"name": "Costco", "base": "https://www.costco.com",
               "pages": {"deals": "/online-offers.html", "whats-new": "/whats-new.html"},
               "search": "/CatalogSearch?keyword={q}"},
    "homedepot": {"name": "Home Depot", "base": "https://www.homedepot.com",
                  "pages": {"special-buy": "/SpecialBuy/SpecialBuyEvent"}, "search": "/s/{q}"},
    "lowes": {"name": "Lowe's", "base": "https://www.lowes.com",
              "pages": {"deals": "/l/shop/deals", "clearance": "/pl/clearance"}, "search": "/search?searchTerm={q}"},
    "newegg": {"name": "Newegg", "base": "https://www.newegg.com",
               "pages": {"todays-deals": "/todays-deals", "shell-shocker": "/d/Shell-Shocker"},
               "search": "/p/pl?d={q}"},
    "bhphoto": {"name": "B&H Photo", "base": "https://www.bhphotovideo.com",
                "pages": {"deals": "/c/browse/deals-on-sale/ci/4250"}, "search": "/c/search?q={q}"},
    "microcenter": {"name": "Micro Center", "base": "https://www.microcenter.com",
                    "pages": {"deals": "/site/content/deals.aspx"}, "search": "/search/search_results.aspx?Ntt={q}"},
    "samsclub": {"name": "Sam's Club", "base": "https://www.samsclub.com",
                 "pages": {"deals": "/shop/deals"}, "search": "/s/{q}"},
    "macys": {"name": "Macy's", "base": "https://www.macys.com",
              "pages": {"deals": "/shop/sale/deals-of-the-day?id=28794"}, "search": "/shop/featured/{q}"},
    "kohls": {"name": "Kohl's", "base": "https://www.kohls.com",
              "pages": {"clearance": "/catalog/clearance.jsp?CN=Promotions:Clearance"}, "search": "/search.jsp?search={q}"},
    "dell": {"name": "Dell", "base": "https://www.dell.com",
             "pages": {"deals": "/en-us/shop/deals"}, "search": "/en-us/search/{q}"},
    "apple-refurb": {"name": "Apple", "base": "https://www.apple.com",
                     "pages": {"mac": "/shop/refurbished/mac", "ipad": "/shop/refurbished/ipad"}, "search": ""},
    "ebay": {"name": "eBay", "base": "https://www.ebay.com",
             "pages": {"deals": "/deals"}, "search": "/sch/i.html?_nkw={q}&LH_BIN=1&_sop=15"},
    "gamestop": {"name": "GameStop", "base": "https://www.gamestop.com",
                 "pages": {"deals": "/deals"}, "search": "/search/?q={q}"},
    "wayfair": {"name": "Wayfair", "base": "https://www.wayfair.com",
                "pages": {"daily-sales": "/daily-sales"}, "search": "/keyword.php?keyword={q}"},
    "other": {"name": None, "base": "", "pages": {}, "search": ""},
}

# One request per host at a time, at most every `min_gap` seconds — across all sources.
_host_lock = threading.Lock()
_host_last: dict[str, float] = {}


def _polite_wait(host: str, min_gap: float, sleep=time.sleep) -> None:
    with _host_lock:
        wait = _host_last.get(host, 0) + min_gap - time.monotonic()
        _host_last[host] = time.monotonic() + max(wait, 0)
    if wait > 0:
        sleep(wait)


class BrowserFetcher:
    """Render pages in headless Chromium (needs `pip install playwright`; a browser binary)."""

    def __init__(self, wait_ms: int = 2500):
        self.wait_ms = wait_ms

    def __call__(self, url: str) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - optional dependency
            raise FetchError("browser: true needs `pip install playwright && playwright install chromium`") from e
        with sync_playwright() as p:  # pragma: no cover - needs a browser
            browser = p.chromium.launch()
            try:
                page = browser.new_page(locale="en-US", viewport={"width": 1366, "height": 900})
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(self.wait_ms)
                if resp is not None and resp.status >= 400:
                    raise FetchError(f"{url}: HTTP {resp.status}", resp.status)
                return page.content()
            finally:
                browser.close()


class RetailSource(Source):
    type = "retailer"
    default_interval = 300

    def __init__(self, name: str, retailer: str, pages: list[str] | None = None, search: list[str] | None = None,
                 urls: list[str] | None = None, browser: bool | None = None, min_gap_s: float = 5.0,
                 display_name: str | None = None, **kw):
        super().__init__(name, **kw)
        if retailer not in RETAILERS:
            raise ValueError(f"source {name!r}: unknown retailer {retailer!r}; choose from {sorted(RETAILERS)}")
        spec = RETAILERS[retailer]
        self.retailer_key = retailer
        self.display = display_name or spec["name"]
        self.base = spec["base"]
        self.urls: list[str] = []
        for pg in pages if pages is not None else list(spec["pages"])[:2]:
            if pg.startswith("http"):
                self.urls.append(pg)
            elif pg in spec["pages"]:
                self.urls.append(self.base + spec["pages"][pg])
            else:
                raise ValueError(f"source {name!r}: {retailer} has no page {pg!r}; known: {sorted(spec['pages'])}")
        for q in search or []:
            if not spec["search"]:
                raise ValueError(f"source {name!r}: {retailer} has no search preset; pass full URLs instead")
            self.urls.append(self.base + spec["search"].format(q=quote_plus(q)))
        self.urls += urls or []
        if not self.urls:
            raise ValueError(f"source {name!r}: nothing to scan (give pages, search or urls)")
        use_browser = spec.get("browser", False) if browser is None else browser
        self.render = BrowserFetcher() if use_browser else None
        self.min_gap_s = min_gap_s

    def _get(self, url: str) -> str:
        from urllib.parse import urlparse
        _polite_wait(urlparse(url).hostname or "", self.min_gap_s, self.http.sleep)
        if self.render is not None:
            return self.render(url)
        return self.http.get_text(url, {"Accept": "text/html,application/xhtml+xml",
                                        "Accept-Language": "en-US,en;q=0.9"})

    def page_offers(self, page: str, url: str) -> list[Offer]:
        from ..retailers import detect_retailer
        offers = []
        for p in harvest_products(page, url):
            link = p["url"] or url
            offers.append(Offer(
                source=self.name,
                retailer=self.display or detect_retailer(link, p["name"]),
                title=p["name"],
                url=link,
                price=p["price"],
                reference_price=p["was"],
                reference_trust=0.75,
                product_id=p["id"] or (p["url"].split("?")[0] if p["url"] else None),
                in_stock=p["in_stock"],
            ))
        return offers

    def fetch(self) -> list[Offer]:
        offers: list[Offer] = []
        blocked = failed = 0
        last_error: FetchError | None = None
        for url in self.urls:
            try:
                page = self._get(url)
            except FetchError as e:
                failed += 1
                last_error = e
                if e.status in (403, 429, 503):
                    blocked += 1
                log.warning("%s: %s", self.name, e)
                continue
            got = self.page_offers(page, url)
            if not got and ("captcha" in page.lower() or "robot" in page.lower()):
                blocked += 1
                log.warning("%s: %s looks like a bot-check page", self.name, url)
            offers.extend(got)
        if blocked and not offers:
            # surface as a failure so the scheduler backs off this source
            raise FetchError(f"{self.name}: blocked on {blocked}/{len(self.urls)} pages", 403)
        if failed == len(self.urls):
            raise FetchError(f"{self.name}: every page failed ({last_error})",
                             last_error.status if last_error else None)
        return offers

    def live_price(self, offer: Offer) -> float | None:
        """Used by glitch verification: re-read the product page."""
        try:
            products = harvest_products(self._get(offer.url), offer.url)
        except FetchError:
            return None
        if offer.product_id:
            for p in products:
                if p["id"] == offer.product_id:
                    return p["price"]
        return products[0]["price"] if len(products) == 1 else None
