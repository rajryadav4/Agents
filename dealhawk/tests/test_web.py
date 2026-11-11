import base64
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from dealhawk.brain import Brain, LearningConfig
from dealhawk.demo import seed
from dealhawk.feedback import FeedbackConfig
from dealhawk.models import Deal, Offer, Score
from dealhawk.scoring import ScoringConfig
from dealhawk.store import Store
from dealhawk.verify import VerifyResult
from dealhawk.web import WebConfig, make_server

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(store, title="MacBook Pro", price=1299.0, tier="steal", glitch=False, verified=None, ts=None):
    o = Offer("walmart", "Walmart", title, "https://www.walmart.com/ip/1", price, product_id=title)
    s = Score(96 if tier == "steal" else 55, tier, "history", reasons=["new all-time low"], typical_price=1974.0,
              discount=1 - price / 1974, record_low=True, glitch_suspect=glitch)
    d = Deal(o, s, "MacBooks", features={"bias": 1.0, "w=macbook": 1.0})
    if verified:
        d.verification = VerifyResult(verified, price, "page", time.time())
    store.record(o, ts=(ts or time.time()) - 1)
    return store.log_deal(d, notified=True, ts=ts)


def start(password="", demo=False, store=None):
    store = store or Store(":memory:")
    brain = Brain(store, ScoringConfig(), LearningConfig(), judge=False)
    srv = make_server(WebConfig(port=0, password=password), FeedbackConfig(), brain, store, demo=demo)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", store


@pytest.fixture
def web():
    srv, base, store = start()
    yield base, store
    srv.shutdown()


def get(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with OPENER.open(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_dashboard_page_served(web):
    base, _ = web
    code, html, headers = get(base + "/")
    assert code == 200 and "text/html" in headers["Content-Type"]
    assert "<title>DealHawk</title>" in html and "EventSource" in html and "DEMO MODE" in html
    assert "cdn" not in html.lower()                       # fully self-contained: works offline


def test_deals_api_fields_and_filters(web):
    base, store = web
    a = log(store, "MacBook Pro", glitch=True, verified="confirmed")
    log(store, "Toaster", 20.0, tier="good")
    code, body, _ = get(base + "/api/deals")
    deals = json.loads(body)
    assert code == 200 and [d["title"] for d in deals] == ["Toaster", "MacBook Pro"]   # newest first
    mac = deals[1]
    assert (mac["id"], mac["price"], mac["typical"], mac["glitch"], mac["verified"], mac["record_low"]) == \
        (a, 1299.0, 1974.0, 1, "confirmed", 1)
    assert "features" not in mac and mac["label"] is None
    assert [d["title"] for d in json.loads(get(base + "/api/deals?min_score=90")[1])] == ["MacBook Pro"]
    assert [d["title"] for d in json.loads(get(base + f"/api/deals?after_id={a}")[1])] == ["Toaster"]


def test_label_api_trains_model(web):
    base, store = web
    deal_id = log(store)
    code, body, _ = get(base + f"/api/deals/{deal_id}/label", {"Content-Type": "application/json"},
                        json.dumps({"label": "good"}).encode())
    assert code == 200 and json.loads(body)["ok"]
    assert json.loads(get(base + "/api/deals")[1])[0]["label"] == 1
    assert get(base + f"/api/deals/{deal_id}/label", data=b'{"label":"maybe"}')[0] == 400
    assert get(base + "/api/deals/999/label", data=b'{"label":"bad"}')[0] == 404


def test_history_and_stats(web):
    base, store = web
    deal_id = log(store)
    pts = json.loads(get(base + f"/api/history/{deal_id}")[1])["points"]
    assert pts and pts[-1][1] == 1299.0
    store.set_source_status("walmart", {"name": "walmart", "type": "retailer", "ok": False, "last_run": time.time(),
                                        "interval": 600, "failures": 2, "error": "HTTP 403"})
    s = json.loads(get(base + "/api/stats")[1])
    assert s["deals_24h"] == 1 and s["steals_24h"] == 1 and s["sources"][0]["error"] == "HTTP 403"
    assert get(base + "/api/history/999")[0] == 404 and get(base + "/nope")[0] == 404


def test_stream_pushes_new_deals_instantly(web):
    base, store = web
    first = log(store, "Old deal")
    got = []

    def listen():
        with OPENER.open(base + f"/api/stream?after_id={first}", timeout=10) as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    got.append(json.loads(line[5:]))
                    return
    t = threading.Thread(target=listen, daemon=True)
    t.start()
    time.sleep(0.5)
    t0 = time.time()
    log(store, "Brand new steal")
    t.join(timeout=6)
    assert got and got[0]["title"] == "Brand new steal"
    assert time.time() - t0 < 3                       # pushed within ~1s, no page refresh


def test_password_protects_dashboard_but_not_phone_routes():
    srv, base, store = start(password="hunter2")
    try:
        code, _, headers = get(base + "/api/deals")
        assert code == 401 and "Basic" in headers["WWW-Authenticate"]
        auth = {"Authorization": "Basic " + base64.b64encode(b"me:hunter2").decode()}
        assert get(base + "/api/deals", auth)[0] == 200
        assert get(base + "/", {"Authorization": "Basic " + base64.b64encode(b"me:nope").decode()})[0] == 401
        assert get(base + "/fb/1/good?t=x")[0] == 404             # feedback route reached (not configured)
    finally:
        srv.shutdown()


def test_demo_mode_is_flagged_and_has_no_links():
    store = Store(":memory:")
    seed(store)
    srv, base, _ = start(demo=True, store=store)
    try:
        assert json.loads(get(base + "/api/meta")[1])["demo"] is True
        deals = json.loads(get(base + "/api/deals")[1])
        assert len(deals) == 7
        assert all(d["title"].startswith("Sample:") and d["url"] == "" for d in deals)
        assert any(d["tier"] == "steal" for d in deals) and any(d["glitch"] for d in deals)
    finally:
        srv.shutdown()


def test_old_database_is_upgraded(tmp_path):
    import sqlite3
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE deals (id INTEGER PRIMARY KEY, key TEXT NOT NULL, ts REAL NOT NULL, retailer TEXT,"
                " title TEXT, url TEXT, price REAL, score INTEGER, tier TEXT, basis TEXT, watch TEXT, reasons TEXT,"
                " notified INTEGER DEFAULT 0, explore INTEGER DEFAULT 0, features TEXT)")
    con.commit()
    con.close()
    s = Store(db)
    assert log(s) == 1 and s.recent_deals()[0]["typical"] == 1974.0


def test_config_requires_password_when_exposed():
    from dealhawk.config import ConfigError, parse_config
    with pytest.raises(ConfigError, match="web.password"):
        parse_config({"web": {"host": "0.0.0.0"}})
    cfg = parse_config({"web": {"host": "0.0.0.0", "password": "x", "port": "9000"}})
    assert cfg.web.port == 9000                       # env-substituted strings are coerced
