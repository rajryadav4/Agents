import json
import threading
import urllib.error
import urllib.request

import pytest

from dealhawk.brain import Brain
from dealhawk.feedback import FeedbackConfig
from dealhawk.priceapi import PriceAPI, PriceReader
from dealhawk.scoring import ScoringConfig
from dealhawk.sources.base import FetchError, Http
from dealhawk.store import Store
from dealhawk.tracking import TrackError
from dealhawk.web import WebConfig, make_server

from .conftest import FakeFetcher, fixture_text

PAGE = ('<script type="application/ld+json">{"@type":"Product","name":"Sony WH-1000XM5","sku":"SONY5",'
        '"offers":{"price":"%s","availability":"https://schema.org/InStock"}}</script>')
URL = "https://www.bhphotovideo.com/c/product/1/sony.html"


class Shop:
    def __init__(self, price=399.0):
        self.price = price
        self.calls = 0

    def __call__(self, url, headers, body):
        self.calls += 1
        return 200, (PAGE % self.price).encode()


def api(fetcher, clock=None, **kw):
    http = Http(fetcher=fetcher, retries=0, sleep=lambda s: None)
    return PriceAPI(Store(":memory:"), reader=PriceReader(http, browser_fallback=False, min_gap_s=0),
                    browser_fallback=False, http_factory=lambda: http, **({"clock": clock} if clock else {}), **kw)


def test_lookup_returns_price_history_and_score(clock):
    shop = Shop(399.0)
    a = api(shop, clock)
    r = a.lookup(URL)
    assert (r["title"], r["price"], r["retailer"], r["in_stock"], r["method"]) == \
        ("Sony WH-1000XM5", 399.0, "B&H Photo", True, "page")
    assert r["history"]["count"] == 1 and r["cached"] is False and r["product_id"] == "SONY5"
    assert a.lookup(URL)["cached"] is True and shop.calls == 1          # cached for a few minutes
    for p in (399, 389, 399, 399):
        clock.advance(7 * 3600); shop.price = p; a.lookup(URL, fresh=True)
    clock.advance(7 * 3600); shop.price = 199.0
    r = a.lookup(URL, fresh=True)
    assert r["score"]["tier"] == "steal" and r["history"]["low"] == 199.0 and r["history"]["count"] == 6
    assert r["history"]["points"][-1][1] == 199.0


def test_lookup_errors():
    a = api(FakeFetcher({"bhphoto": (403, "captcha")}))
    with pytest.raises(FetchError):
        a.lookup(URL)
    with pytest.raises(TrackError):
        a.lookup("http://192.168.1.1/admin")


def test_search_compares_stores_cheapest_first():
    f = FakeFetcher({
        "walmart.com/search": (200, fixture_text("walmart_deals.html")),     # AirPods Pro 2 $89 + air fryer
        "bestbuy.com/site/searchpage": (200, '<script type="application/ld+json">{"@type":"ItemList",'
                                             '"itemListElement":[{"@type":"Product","name":"Apple AirPods Pro 2",'
                                             '"url":"/site/airpods/1.p","sku":"BB1","offers":{"price":"99.99"}}]}'
                                             '</script>'),
        "newegg.com": (403, "blocked"),
    })
    r = api(f).search("airpods pro", ["walmart", "bestbuy", "newegg", "target"])
    assert [x["price"] for x in r["results"]] == [89.0, 99.99]               # the air fryer is filtered out
    assert r["cheapest"]["retailer"] == "Walmart"
    assert r["results"][1]["url"] == "https://www.bestbuy.com/site/airpods/1.p"
    st = r["stores"]
    assert st["walmart"] == {"ok": True, "count": 1} and st["bestbuy"]["count"] == 1
    assert not st["newegg"]["ok"] and "blocked" in st["newegg"]["error"]
    assert not st["target"]["ok"] and "playwright" in st["target"]["error"]   # JS store without a browser
    assert "points" not in r["results"][0]["history"]


def test_search_validation():
    a = api(FakeFetcher({}))
    with pytest.raises(ValueError, match="type what"):
        a.search(" ")
    with pytest.raises(ValueError, match="no search for"):
        a.search("tv", ["nope"])


def test_http_api_with_key():
    shop = Shop(349.0)
    a = api(shop)
    store = a.store
    srv = make_server(WebConfig(port=0, password="pw", api_key="k3y"), FeedbackConfig(),
                      Brain(store, ScoringConfig(), judge=False), store, priceapi=a)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def get(path, key=None):
        req = urllib.request.Request(base + path, headers={"X-API-Key": key} if key else {})
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, json.loads(body) if body.startswith(b"{") else body.decode()
    try:
        assert get(f"/api/price?url={URL}")[0] == 401                       # no key, no password
        assert get(f"/api/price?url={URL}", "wrong")[0] == 401
        code, body = get(f"/api/price?url={URL}", "k3y")
        assert code == 200 and body["price"] == 349.0
        assert get("/", "k3y")[0] == 401                                     # key is for /api only
        assert get("/api/price?url=notaurl", "k3y")[0] == 400
        assert get("/api/search?q=x&stores=nope", "k3y")[0] == 400
    finally:
        srv.shutdown()


def test_cli_price_reports_plain_error(tmp_path, monkeypatch, capsys):
    from dealhawk import cli
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    with pytest.raises(SystemExit) as e:
        cli.main(["price", URL])                                             # network is disabled in tests
    assert "couldn't read the price" in str(e.value)
    cli.main(["search", "airpods", "--stores", "walmart"])
    assert "✗ walmart" in capsys.readouterr().out
