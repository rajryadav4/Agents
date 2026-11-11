import json
import threading
import urllib.error
import urllib.request

import pytest

from dealhawk.brain import Brain, LearningConfig
from dealhawk.feedback import FeedbackConfig, links, parse_reply, token, webhook_token
from dealhawk.web import WebConfig, make_server
from dealhawk.models import Deal, Offer, Score
from dealhawk.scoring import ScoringConfig
from dealhawk.store import Store


@pytest.fixture
def server():
    store = Store(":memory:")
    brain = Brain(store, ScoringConfig(), LearningConfig(), judge=False)
    deal = Deal(Offer("s", "Amazon", "MacBook", "https://amazon.com/dp/X", 999.0), Score(80, "steal", "history"),
                "w", features={"bias": 1.0, "rule": 0.8, "w=macbook": 1.0})
    deal_id = store.log_deal(deal, notified=True)
    cfg = FeedbackConfig(public_url="http://127.0.0.1", secret="s3cret", whatsapp_number="+1 617 555 0000")
    # a dashboard password must not block phone feedback routes (they carry their own tokens)
    srv = make_server(WebConfig(port=0, password="pw"), cfg, brain, store)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, cfg, store, brain, deal_id
    srv.shutdown()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def call(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def test_buttons_train_model(server):
    base, cfg, store, brain, deal_id = server
    t = token(cfg.secret, deal_id)
    code, body, _ = call(f"{base}/fb/{deal_id}/good?t={t}")              # GET = confirm page only
    assert code == 200 and "<form method=post" in body and store.stats()["labels_you"] == 0
    code, body, _ = call(f"{base}/fb/{deal_id}/good?t={t}", data=b"")    # POST = label
    assert code == 200 and store.stats()["labels_you"] == 1
    assert brain.learner.w["w=macbook"] > 0


def test_bad_token_and_unknown_routes(server):
    base, cfg, store, _, deal_id = server
    assert call(f"{base}/fb/{deal_id}/good?t=nope", data=b"")[0] == 403
    assert call(f"{base}/nope")[0] == 401        # everything else is behind the dashboard password
    assert store.stats()["labels_you"] == 0


def test_click_redirects_and_counts(server):
    base, cfg, store, _, deal_id = server
    code, _, headers = call(f"{base}/go/{deal_id}?t={token(cfg.secret, deal_id)}")
    assert code == 302 and headers["Location"] == "https://amazon.com/dp/X"
    assert store.stats()["labels_clicks"] == 1


def test_twilio_whatsapp_reply(server):
    base, cfg, store, _, deal_id = server
    wt = webhook_token(cfg.secret)
    body = f"From=whatsapp%3A%2B16175550000&Body=bad+{deal_id}".encode()
    code, xml, _ = call(f"{base}/wa/twilio?t={wt}", data=body)
    assert code == 200 and "<Message>Got it" in xml
    assert store.all_feedback()[0]["label"] == 0
    stranger = f"From=whatsapp%3A%2B19995550000&Body=good+{deal_id}".encode()
    assert call(f"{base}/wa/twilio?t={wt}", data=stranger)[1] == "<Response/>"
    assert call(f"{base}/wa/twilio?t=wrong", data=body)[0] == 403


def test_meta_cloud_webhook_verify_and_reply(server):
    base, cfg, store, _, deal_id = server
    wt = webhook_token(cfg.secret)
    code, body, _ = call(f"{base}/wa/cloud?hub.mode=subscribe&hub.verify_token={wt}&hub.challenge=abc")
    assert (code, body) == (200, "abc")
    assert call(f"{base}/wa/cloud?hub.mode=subscribe&hub.verify_token=x&hub.challenge=abc")[0] == 403
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "16175550000", "type": "text", "text": {"body": f"👍 {deal_id}"}}]}}]}]}
    code, _, _ = call(f"{base}/wa/cloud?t={wt}", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json"})
    assert code == 200 and store.all_feedback()[0]["label"] == 1


def test_links_and_parse_reply():
    cfg = FeedbackConfig(public_url="https://d.example/", secret="k")
    ls = links(cfg, 7)
    assert ls["good"].startswith("https://d.example/fb/7/good?t=") and ls["go"].startswith("https://d.example/go/7")
    assert parse_reply("good 12") == (12, 1) and parse_reply("12 no") == (12, 0) and parse_reply("hi") is None
