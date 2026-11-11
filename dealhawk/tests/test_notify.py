import base64
import json
from urllib.parse import parse_qs, urlparse

import pytest

from dealhawk.models import Deal, Offer, Score
from dealhawk.notify import (ConsoleNotifier, DiscordNotifier, NtfyNotifier, TelegramNotifier, WhatsAppNotifier,
                             whatsapp_text)
from dealhawk.sources.base import Http


def deal(with_links=True):
    d = Deal(Offer("sd", "Amazon", 'MacBook Pro 14" M4', "https://amazon.com/dp/X", 1299.0, image="https://img"),
             Score(96, "steal", "history", reasons=["new all-time low"], typical_price=1999.0, low_price=1799.0,
                   record_low=True), "MacBooks", id=42)
    if with_links:
        d.links = {"good": "https://h/fb/42/good?t=a", "bad": "https://h/fb/42/bad?t=a", "go": "https://h/go/42?t=a"}
    return d


class Rec:
    def __init__(self):
        self.calls = []

    def __call__(self, url, headers, body):
        self.calls.append((url, headers, body))
        return 200, b"{}"


def http():
    r = Rec()
    return Http(fetcher=r, sleep=lambda s: None), r


def test_deal_text():
    d = deal()
    assert d.headline == "🚨 STEAL (96/100): $1,299.00 at Amazon"
    assert "Usually ~$1,999.00 → now $1,299.00 (35% off)" in d.body and "NEW RECORD LOW" in d.body
    assert d.open_url == "https://h/go/42?t=a" and deal(False).open_url == "https://amazon.com/dp/X"


def test_ntfy_payload_has_feedback_buttons():
    h, r = http()
    NtfyNotifier(topic="t0p1c", http=h).send(deal())
    body = json.loads(r.calls[0][2])
    assert body["topic"] == "t0p1c" and body["priority"] == 5 and body["click"] == "https://h/go/42?t=a"
    assert [a["label"] for a in body["actions"]] == ["🛒 Open deal", "👍 Good deal", "👎 Not for me"]
    assert body["actions"][1]["method"] == "POST"
    with pytest.raises(ValueError):
        NtfyNotifier(topic="")


def test_whatsapp_callmebot():
    h, r = http()
    WhatsAppNotifier(provider="callmebot", to="+1 617 555 0000", apikey="K", http=h).send(deal())
    q = parse_qs(urlparse(r.calls[0][0]).query)
    assert q["phone"] == ["16175550000"] and q["apikey"] == ["K"]
    assert "https://h/go/42?t=a" in q["text"][0] and 'Reply "good 42"' in q["text"][0]


def test_whatsapp_twilio():
    h, r = http()
    WhatsAppNotifier(provider="twilio", to="+16175550000", from_number="whatsapp:+14155238886",
                     account_sid="AC1", auth_token="tok", http=h).send(deal())
    url, headers, body = r.calls[0]
    assert url == "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json"
    assert headers["Authorization"] == "Basic " + base64.b64encode(b"AC1:tok").decode()
    form = parse_qs(body.decode())
    assert form["From"] == ["whatsapp:+14155238886"] and form["To"] == ["whatsapp:+16175550000"]
    assert "🛒 https://h/go/42?t=a" in form["Body"][0] and "*🚨 STEAL" in form["Body"][0]


def test_whatsapp_cloud_text_and_template():
    h, r = http()
    WhatsAppNotifier(provider="cloud", to="+16175550000", phone_number_id="123", access_token="T", http=h).send(deal())
    url, headers, body = r.calls[0]
    msg = json.loads(body)
    assert url.endswith("/123/messages") and headers["Authorization"] == "Bearer T"
    assert msg["messaging_product"] == "whatsapp" and msg["to"] == "16175550000"
    assert msg["text"]["preview_url"] is True and "🛒 https://h/go/42?t=a" in msg["text"]["body"]
    h2, r2 = http()
    WhatsAppNotifier(provider="cloud", to="+1", phone_number_id="1", access_token="T", template="deal_alert",
                     http=h2).send(deal())
    t = json.loads(r2.calls[0][2])["template"]
    assert t["name"] == "deal_alert" and t["components"][0]["parameters"][2]["text"] == "https://h/go/42?t=a"


def test_whatsapp_validation():
    with pytest.raises(ValueError, match="provider"):
        WhatsAppNotifier(provider="sms", to="+1")
    with pytest.raises(ValueError, match="auth_token"):
        WhatsAppNotifier(provider="twilio", to="+1", account_sid="a", from_number="+2")


def test_whatsapp_text_without_id():
    d = deal(False)
    d.id = None
    assert "Reply" not in whatsapp_text(d) and "https://amazon.com/dp/X" in whatsapp_text(d)


def test_telegram_and_discord_and_console():
    h, r = http()
    TelegramNotifier(bot_token="B", chat_id=1, http=h).send(deal())
    kb = json.loads(r.calls[0][2])["reply_markup"]["inline_keyboard"]
    assert kb[1][0]["text"] == "👍 Good deal"
    h, r = http()
    DiscordNotifier(webhook_url="https://discord/x", http=h).send(deal())
    assert "👍 Good deal" in json.loads(r.calls[0][2])["embeds"][0]["description"]
    out = []
    ConsoleNotifier(printer=out.append).send(deal())
    assert "dealhawk feedback 42 good|bad" in out[0]
