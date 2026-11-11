"""Source plumbing: a tiny stdlib HTTP client and the Source interface."""

from __future__ import annotations

import gzip
import json
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
import zlib
from typing import Any, Callable

from ..models import Offer

log = logging.getLogger("dealhawk.sources")

DEFAULT_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36 DealHawk/0.1"


class FetchError(Exception):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


# A fetcher takes (url, headers, body) and returns (status, bytes). Tests inject fakes.
Fetcher = Callable[[str, dict, bytes | None], tuple[int, bytes]]


def make_ssl_context() -> ssl.SSLContext:
    """Trust the system's certificates *and* certifi's bundle.

    Python from python.org on macOS ships without access to the Mac's keychain, so
    every https request fails with CERTIFICATE_VERIFY_FAILED unless certifi's
    Mozilla bundle is loaded. SSL_CERT_FILE / corporate proxy CAs still work:
    the default context honours them and certifi is only added on top.
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError) as e:  # pragma: no cover - depends on install
        log.debug("certifi bundle not loaded: %s", e)
    return ctx


_SSL = make_ssl_context()


def urllib_fetch(url: str, headers: dict, body: bytes | None = None, timeout: float = 20) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            data = resp.read()
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                data = gzip.decompress(data)
            elif enc == "deflate":
                data = zlib.decompress(data)
            return resp.status, data
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"{url}: {e}") from e


class Http:
    """GET/POST with retries, exponential backoff on 429/5xx, and a polite UA."""

    def __init__(self, fetcher: Fetcher | None = None, user_agent: str = DEFAULT_UA,
                 retries: int = 2, backoff: float = 2.0, sleep: Callable[[float], None] = time.sleep):
        self.fetcher = fetcher or urllib_fetch
        self.user_agent = user_agent
        self.retries = retries
        self.backoff = backoff
        self.sleep = sleep

    def request(self, url: str, headers: dict | None = None, body: bytes | None = None) -> bytes:
        h = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip", "Accept": "*/*", **(headers or {})}
        for attempt in range(self.retries + 1):
            try:
                status, data = self.fetcher(url, h, body)
            except FetchError:
                if attempt == self.retries:
                    raise
                self.sleep(self.backoff * 2 ** attempt + random.random())
                continue
            if 200 <= status < 300:
                return data
            if status in (429, 500, 502, 503, 504) and attempt < self.retries:
                self.sleep(self.backoff * 2 ** attempt + random.random())
                continue
            raise FetchError(f"{url}: HTTP {status}", status)
        raise FetchError(f"{url}: gave up")  # pragma: no cover

    def get_text(self, url: str, headers: dict | None = None) -> str:
        return self.request(url, headers).decode("utf-8", errors="replace")

    def get_json(self, url: str, headers: dict | None = None) -> Any:
        return json.loads(self.request(url, {"Accept": "application/json", **(headers or {})}))

    def post_json(self, url: str, payload: Any, headers: dict | None = None) -> Any:
        body = json.dumps(payload).encode()
        return json.loads(self.request(url, {"Content-Type": "application/json", "Accept": "application/json",
                                             **(headers or {})}, body))


class Source:
    """Subclasses implement fetch() -> list[Offer]. One instance per configured source."""

    type: str = "base"
    default_interval: int = 300
    trust: str = "first-party"     # see Offer.trust

    def __init__(self, name: str, http: Http | None = None, interval: int | None = None):
        self.name = name
        self.http = http or Http()
        self.interval = interval or self.default_interval

    def fetch(self) -> list[Offer]:  # pragma: no cover - interface
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} every {self.interval}s>"
