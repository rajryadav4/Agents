"""Track a product by its link: alert at your target price, or let DealHawk decide.

    mode "target" → alert when the price is at or below the price you set
    mode "smart"  → DealHawk builds the item's own price history and alerts when it
                    hits its lowest price so far, or the Steal Score says it's a
                    great/steal deal (Claude weighs in early, when enabled)

In both modes a suspected price glitch is re-verified live and sent at once.

Prices are read by priceapi.PriceReader (Shopify JSON → structured page data →
headless browser fallback), the same reader the price API uses.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from urllib.parse import urlparse

from .models import Offer
from .retailers import detect_retailer
from .sources.base import FetchError, Http, Source

log = logging.getLogger("dealhawk.tracking")
MODES = ("target", "smart")


class TrackError(ValueError):
    pass


def validate_url(url: str) -> str:
    """Accept only public http(s) product links (the server fetches them — no internal addresses)."""
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise TrackError("paste a full product link starting with http:// or https://")
    host = u.hostname.lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise TrackError("that address isn't a public store")
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addrs = {ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(host, None)}
        except (socket.gaierror, UnicodeError, OSError):
            addrs = set()          # can't resolve now; the fetch will report it
    if any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast for a in addrs):
        raise TrackError("that address isn't a public store")
    return url


def explain(e: Exception, url: str = "") -> str:
    """Turn a fetch error into something a person can act on."""
    msg = str(e)
    if url and msg.startswith(url):
        msg = msg[len(url):].lstrip(": ")
    low = msg.lower()
    status = getattr(e, "status", None)
    if "certificate_verify_failed" in low or "certificate verify failed" in low:
        return ("Python can't verify https certificates on this computer — run `pip install --upgrade certifi` "
                "and restart DealHawk (on a Mac you can also run 'Install Certificates.command' in your Python folder)")
    if "tunnel connection failed" in low or "proxy" in low:
        return "this computer's network or proxy refused the connection to the store"
    if status in (403, 429, 503) or "captcha" in low:
        return f"the store blocked the request (bot protection, HTTP {status or 403}) — it will retry later"
    if status == 404:
        return "the store says this page doesn't exist (HTTP 404) — check the link"
    if "timed out" in low or "timeout" in low:
        return "the store didn't respond in time — it will retry"
    if "name or service not known" in low or "nodename nor servname" in low or "getaddrinfo" in low:
        return "couldn't find that website — check the link"
    return msg[:200]



class Tracker:
    """Reads current prices for tracked links and feeds them to the engine."""

    def __init__(self, store, engine=None, http: Http | None = None, browser_fallback: bool = True,
                 min_gap_s: float = 3.0, clock=time.time):
        from .priceapi import PriceReader
        self.store = store
        self.engine = engine
        self.reader = PriceReader(http or Http(retries=1), browser_fallback=browser_fallback, min_gap_s=min_gap_s)
        self.clock = clock

    # ---- managing the list ----
    def add(self, url: str, mode: str = "smart", target_price: float | None = None, check: bool = True) -> dict:
        url = validate_url(url)
        if mode not in MODES:
            raise TrackError(f"mode must be one of {MODES}")
        if mode == "target":
            if target_price is None or target_price <= 0:
                raise TrackError("set the price you want to be alerted at")
        for t in self.store.list_tracked():
            if t["url"] == url:
                raise TrackError(f"already tracking that link (#{t['id']})")
        tid = self.store.add_tracked(url, mode, float(target_price) if target_price else None,
                                     retailer=detect_retailer(url, ""), ts=self.clock())
        if check:
            self.check_in_background(tid)
        return self.store.get_tracked(tid)

    # ---- reading prices ----
    def read_price(self, url: str) -> dict:
        """{name, price, id, in_stock, method} — shared with the price API."""
        return self.reader.read(url)

    # ---- checking ----
    def offer_for(self, t: dict) -> Offer:
        """Read the live price of one tracked item. Updates its status; raises FetchError on failure."""
        now = self.clock()
        try:
            p = self.read_price(t["url"])
        except FetchError as e:
            self.store.update_tracked(t["id"], status="error", error=explain(e, t["url"]), last_checked=now)
            raise
        title = t["title"] or p["name"] or t["url"]
        if not t["title"] and p["name"]:
            self.store.update_tracked(t["id"], title=p["name"])
        return Offer(source="tracked", retailer=t["retailer"] or detect_retailer(t["url"], title), title=title,
                     url=t["url"], price=p["price"], product_id=f"track-{t['id']}", in_stock=p["in_stock"],
                     tracked_id=t["id"])

    def check(self, tid: int):
        """Check one item now and run it through the engine (alerts if it's time)."""
        t = self.store.get_tracked(tid)
        if not t:
            return None
        try:
            offer = self.offer_for(t)
        except FetchError as e:
            log.info("tracked #%d: %s", tid, e)
            return None
        return self.engine.process([offer]) if self.engine else offer

    def check_in_background(self, tid: int) -> None:
        threading.Thread(target=self.check, args=(tid,), daemon=True, name=f"track-{tid}").start()


class TrackedSource(Source):
    """Puts every active tracked link into the normal polling loop."""

    type = "tracked"
    default_interval = 600

    def __init__(self, name: str, tracker: Tracker, **kw):
        super().__init__(name, **kw)
        self.tracker = tracker

    def fetch(self) -> list[Offer]:
        offers, errors = [], 0
        items = self.tracker.store.list_tracked(active_only=True)
        for t in items:
            try:
                offers.append(self.tracker.offer_for(t))
            except FetchError:
                errors += 1
        if items and errors == len(items):
            raise FetchError(f"couldn't read any of {len(items)} tracked links")
        return offers
