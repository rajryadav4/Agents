import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from dealhawk.brain import LearningConfig
from dealhawk.config import AlertConfig, Config, parse_config
from dealhawk.engine import Engine
from dealhawk.feedback import FeedbackConfig
from dealhawk.sources.base import FetchError, Http
from dealhawk.store import Store
from dealhawk.tracking import TrackError, TrackedSource, Tracker, validate_url
from dealhawk.verify import PriceVerifier, VerifyConfig
from dealhawk.web import WebConfig, make_server

from .conftest import FakeFetcher
from .test_engine import Sink

PAGE = ('<script type="application/ld+json">{"@type":"Product","name":"LG 65\\" C4 OLED","sku":"LG65",'
        '"offers":{"price":"%s","availability":"https://schema.org/InStock"}}</script>')
URL = "https://www.bestbuy.com/site/lg-65-oled/6578.p"


class Pages:
    """A product page whose price we can change between checks."""
    def __init__(self, price):
        self.price, self.calls = price, 0

    def __call__(self, url, headers, body):
        self.calls += 1
        if "blocked" in url:
            return 403, b"captcha"
        return 200, (PAGE % self.price).encode()


def setup(clock, price=1999.0):
    cfg = Config(database=":memory:", alerts=AlertConfig(min_score=70),
                 learning=LearningConfig(explore_per_day=0, crowd_labels=False))
    store, sink, pages = Store(":memory:"), Sink(), Pages(price)
    http = Http(fetcher=pages, retries=0, sleep=lambda s: None)
    verifier = PriceVerifier(VerifyConfig(), http=http, clock=clock)
    eng = Engine(cfg, store, notifiers=[sink], verifier=verifier, clock=clock)
    tr = Tracker(store, eng, http=http, browser_fallback=False, min_gap_s=0, clock=clock)
    return tr, store, sink, pages


def test_validate_url():
    assert validate_url(" https://www.walmart.com/ip/1 ") == "https://www.walmart.com/ip/1"
    for bad in ("walmart.com/ip/1", "ftp://x.com/a", "http://localhost:8686/", "http://127.0.0.1/x",
                "http://10.0.0.5/admin", "http://169.254.169.254/latest/meta-data", "http://printer.local/"):
        with pytest.raises(TrackError):
            validate_url(bad)


def test_add_validation(clock):
    tr, store, _, _ = setup(clock)
    with pytest.raises(TrackError, match="price"):
        tr.add(URL, "target", None, check=False)
    with pytest.raises(TrackError, match="mode"):
        tr.add(URL, "psychic", check=False)
    t = tr.add(URL, "smart", check=False)
    assert t["status"] == "pending" and t["retailer"] == "Best Buy"
    with pytest.raises(TrackError, match="already tracking"):
        tr.add(URL, "smart", check=False)


def test_target_price_alert(clock):
    tr, store, sink, pages = setup(clock, 1999.0)
    t = tr.add(URL, "target", 1500, check=False)
    tr.check(t["id"])
    row = store.get_tracked(t["id"])
    assert (row["status"], row["last_price"], row["title"]) == ("ok", 1999.0, 'LG 65" C4 OLED')
    assert sink.sent == []                                     # above target: quiet
    clock.advance(600); pages.price = 1499.0
    tr.check(t["id"])
    assert len(sink.sent) == 1
    d = sink.sent[0]
    assert d.watch == f"Tracking #{t['id']}" and d.score.reasons[0].startswith("🎯 at or below your target $1,500.00")
    clock.advance(600); tr.check(t["id"])
    assert len(sink.sent) == 1                                 # same price: no repeat
    clock.advance(600); pages.price = 1399.0
    tr.check(t["id"])
    assert len(sink.sent) == 2                                 # dropped further: alert again
    assert store.get_tracked(t["id"])["hits"] == 2 and store.get_tracked(t["id"])["low_price"] == 1399.0


def test_ai_watch_alerts_on_lowest_price_after_history(clock):
    tr, store, sink, pages = setup(clock)
    t = tr.add(URL, "smart", check=False)
    for p in (1999, 1999, 1949, 1999, 1999):
        pages.price = p; tr.check(t["id"]); clock.advance(7 * 3600)
    assert sink.sent == []                                     # normal prices: quiet
    pages.price = 1879.0                                       # small new low: 3.6% under previous low
    tr.check(t["id"])
    assert len(sink.sent) == 1 and "lowest price since you started tracking" in sink.sent[0].score.reasons[0]
    clock.advance(7 * 3600); pages.price = 1299.0              # big drop: steal
    tr.check(t["id"])
    assert sink.sent[-1].score.tier == "steal"


def test_ai_watch_does_not_alert_on_first_checks(clock):
    tr, store, sink, pages = setup(clock)
    t = tr.add(URL, "smart", check=False)
    for p in (1999, 1899, 1849):                               # every first check is a "new low" — ignore
        pages.price = p; tr.check(t["id"]); clock.advance(7 * 3600)
    assert sink.sent == []


def test_blocked_page_reports_error_and_paused_items_skip(clock):
    tr, store, sink, pages = setup(clock)
    t = tr.add("https://www.walmart.com/ip/blocked/1", "target", 10, check=False)
    tr.check(t["id"])
    row = store.get_tracked(t["id"])
    assert row["status"] == "error" and row["error"].startswith("the store blocked the request")
    ok = tr.add(URL, "target", 5000, check=False)
    store.update_tracked(ok["id"], active=0)
    src = TrackedSource("tracked-links", tr)
    with pytest.raises(FetchError):                            # the only active item is blocked
        src.fetch()
    store.update_tracked(ok["id"], active=1)
    offers = src.fetch()
    assert [o.tracked_id for o in offers] == [ok["id"]]


def test_shopify_link_uses_product_json(clock):
    f = FakeFetcher({"/products/wool-runner.json": (200, '{"product":{"title":"Wool Runner","variants":'
                                                         '[{"id":7,"price":"60.00","available":true}]}}')})
    store = Store(":memory:")
    tr = Tracker(store, None, http=Http(fetcher=f, retries=0, sleep=lambda s: None), browser_fallback=False,
                 min_gap_s=0)
    assert tr.read_price("https://www.allbirds.com/products/wool-runner?variant=7") == \
        {"name": "Wool Runner", "price": 60.0, "id": "7", "in_stock": True, "method": "shopify-json"}


def test_dashboard_tracking_api(clock):
    tr, store, sink, pages = setup(clock)
    srv = make_server(WebConfig(port=0), FeedbackConfig(), tr.engine.brain, store, tracker=tr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(path, method="GET", body=None):
        req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())
    try:
        assert call("/api/tracked", "POST", {"url": "nope", "mode": "smart"})[0] == 400
        assert call("/api/tracked", "POST", {"url": URL, "mode": "target"})[1]["error"].startswith("set the price")
        code, t = call("/api/tracked", "POST", {"url": URL, "mode": "target", "target_price": "1500"})
        assert code == 201 and t["target_price"] == 1500.0
        for _ in range(50):                                    # first check runs in the background
            if store.get_tracked(t["id"])["status"] == "ok":
                break
            time.sleep(0.05)
        code, items = call("/api/tracked")
        assert items[0]["last_price"] == 1999.0 and items[0]["points"][-1][1] == 1999.0
        assert call(f"/api/tracked/{t['id']}", "POST", {"target_price": 2100})[1]["target_price"] == 2100
        assert call(f"/api/tracked/{t['id']}", "POST", {"mode": "target", "target_price": None})[0] == 400
        assert call(f"/api/tracked/{t['id']}", "POST", {"active": False})[1]["active"] == 0
        assert call(f"/api/tracked/{t['id']}/check", "POST")[0] == 202
        assert call(f"/api/tracked/{t['id']}", "DELETE")[1]["ok"] is True
        assert call(f"/api/tracked/{t['id']}", "DELETE")[0] == 404
    finally:
        srv.shutdown()


def test_tracking_disabled_returns_503():
    store = Store(":memory:")
    from dealhawk.brain import Brain
    from dealhawk.scoring import ScoringConfig
    srv = make_server(WebConfig(port=0), FeedbackConfig(), Brain(store, ScoringConfig(), judge=False), store)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with pytest.raises(urllib.error.HTTPError) as e:
            opener.open(f"http://127.0.0.1:{srv.server_address[1]}/api/tracked", timeout=5)
        assert e.value.code == 503
    finally:
        srv.shutdown()


def test_tracking_config_and_cli(tmp_path, monkeypatch, capsys):
    assert parse_config({"tracking": {"interval": "300"}}).tracking.interval == 300
    from dealhawk import cli
    monkeypatch.chdir(tmp_path)
    cli.main(["init"])
    cli.main(["track", "add", "https://www.bestbuy.com/site/x/1.p", "--target", "99"])
    out = capsys.readouterr().out
    assert "tracking #1 (alert at ≤ $99.00)" in out and "couldn't read the price yet" in out   # no network here
    cli.main(["track", "list"])
    assert "target $99.00" in capsys.readouterr().out
    cli.main(["track", "rm", "1"])
    assert "removed" in capsys.readouterr().out


def test_errors_are_explained():
    from dealhawk.tracking import explain
    u = "https://x.com/p"
    assert explain(FetchError(f"{u}: <urlopen error Tunnel connection failed: 403 Forbidden>"), u).startswith(
        "this computer's network")
    assert "bot protection" in explain(FetchError(f"{u}: HTTP 403", 403), u)
    assert "doesn't exist" in explain(FetchError(f"{u}: HTTP 404", 404), u)
    assert "check the link" in explain(FetchError(f"{u}: [Errno -2] Name or service not known"), u)

