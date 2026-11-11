"""Tiny HTTP endpoint so 👍 / 👎 buttons on your phone train the model.

    POST /fb/<deal_id>/good?t=<token>    → "good deal" label   (GET shows a confirm button)
    POST /fb/<deal_id>/bad?t=<token>     → "not for me" label
    GET  /go/<deal_id>?t=<token>         → counts a click (weak 👍), redirects to the store
    POST /wa/twilio?t=<wa-token>         → WhatsApp replies via Twilio ("good 123" / "bad 123")
    GET|POST /wa/cloud?t=<wa-token>      → WhatsApp replies via Meta Cloud API (+ webhook verification)

Tokens are an HMAC of the deal id, so nobody can poison your model by guessing URLs.
These routes are served by the dashboard web server (web.py). Your phone must be
able to reach it: run DealHawk on a VPS, or put your home box on Tailscale, and
set feedback.public_url accordingly.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("dealhawk.feedback")


@dataclass
class FeedbackConfig:
    public_url: str = ""         # e.g. https://dealhawk.example.com or http://my-box.tailnet.ts.net:8686
    secret: str = ""
    whatsapp_number: str = ""    # your number; WhatsApp replies from anyone else are ignored

    @property
    def active(self) -> bool:
        return bool(self.public_url and self.secret)


def token(secret: str, deal_id: int) -> str:
    return hmac.new(secret.encode(), str(deal_id).encode(), hashlib.sha256).hexdigest()[:20]


def links(cfg: FeedbackConfig, deal_id: int) -> dict[str, str]:
    base, t = cfg.public_url.rstrip("/"), token(cfg.secret, deal_id)
    return {"good": f"{base}/fb/{deal_id}/good?t={t}", "bad": f"{base}/fb/{deal_id}/bad?t={t}",
            "go": f"{base}/go/{deal_id}?t={t}"}


def webhook_token(secret: str) -> str:
    """Put ?t=<this> on the WhatsApp webhook URL; also the Meta verify token."""
    return hmac.new(secret.encode(), b"whatsapp-webhook", hashlib.sha256).hexdigest()[:24]


_REPLY = re.compile(r"(good|bad|yes|no|y|n|👍|👎)\W*#?(\d+)|#?(\d+)\W*(good|bad|yes|no|y|n|👍|👎)", re.I)


def parse_reply(text: str) -> tuple[int, int] | None:
    """'good 123', '👎 45', '45 bad' → (deal_id, label)."""
    m = _REPLY.search(text or "")
    if not m:
        return None
    word, num = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
    return int(num), 1 if word.lower() in ("good", "yes", "y", "👍") else 0


def _digits(n: str) -> str:
    return re.sub(r"\D", "", n or "")


def apply_label(brain, store, deal_id: int, label: int, origin: str = "user") -> bool:
    deal = store.get_deal(deal_id)
    if not deal or not deal["features"]:
        return False
    return brain.learn(deal["key"], label, origin, deal["features"], deal_id=deal_id)


CONFIRM_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>DealHawk</title><body style="font:18px system-ui;display:grid;place-items:center;height:90vh">
<form method=post action="{action}"><button style="font-size:28px;padding:16px 28px">{label}</button></form>"""

_ROUTE = re.compile(r"^/(fb)/(\d+)/(good|bad)$|^/(go)/(\d+)$")


def handle(req, cfg: FeedbackConfig, brain, store) -> bool:
    """Serve /fb, /go and /wa routes on `req` (a BaseHTTPRequestHandler). False if not ours."""
    u = urlparse(req.path)
    if u.path in ("/wa/twilio", "/wa/cloud"):
        _whatsapp(req, u, cfg, brain, store)
        return True
    m = _ROUTE.match(u.path)
    if not m:
        return False
    if not cfg.secret:
        req.reply(404, "feedback links are not configured")
        return True
    deal_id = int(m.group(2) or m.group(5))
    t = parse_qs(u.query).get("t", [""])[0]
    if not hmac.compare_digest(t, token(cfg.secret, deal_id)):
        req.reply(403, "bad token")
        return True
    if m.group(4) == "go":
        deal = store.get_deal(deal_id)
        if not deal:
            req.reply(404, "unknown deal")
            return True
        apply_label(brain, store, deal_id, 1, origin="click")
        req.reply(302, "", location=deal["url"])
        return True
    if req.command == "GET":
        # link previewers fetch URLs; only an explicit POST may change the model
        label = "👍 Good deal" if m.group(3) == "good" else "👎 Not for me"
        req.reply(200, CONFIRM_PAGE.format(action=html.escape(req.path, quote=True), label=label), ctype="text/html")
        return True
    ok = apply_label(brain, store, deal_id, 1 if m.group(3) == "good" else 0)
    req.reply(200 if ok else 404, "thanks — learned 👍" if ok else "unknown deal")
    return True


def _whatsapp(req, u, cfg: FeedbackConfig, brain, store) -> None:
    wa_token = webhook_token(cfg.secret) if cfg.secret else None
    q = parse_qs(u.query)
    if wa_token is None:
        return req.reply(404, "feedback is not configured")
    if u.path == "/wa/cloud" and req.command == "GET":     # Meta webhook verification
        if q.get("hub.verify_token", [""])[0] == wa_token:
            return req.reply(200, q.get("hub.challenge", [""])[0])
        return req.reply(403, "bad verify token")
    if not hmac.compare_digest(q.get("t", [""])[0], wa_token):
        return req.reply(403, "bad token")
    raw = req.body().decode("utf-8", "replace")

    def answer(sender: str, text: str) -> str:
        if cfg.whatsapp_number and _digits(sender) != _digits(cfg.whatsapp_number):
            return ""
        parsed = parse_reply(text)
        if not parsed:
            return 'Reply like "good 123" or "bad 123" (the number is in each alert).'
        deal_id, label = parsed
        if apply_label(brain, store, deal_id, label):
            return f"Got it — deal {deal_id} {'👍' if label else '👎'}. I'll learn from that."
        return f"I don't know deal {deal_id}."

    if u.path == "/wa/twilio":
        form = parse_qs(raw)
        text = answer(form.get("From", [""])[0], form.get("Body", [""])[0])
        twiml = f"<Response><Message>{html.escape(text)}</Message></Response>" if text else "<Response/>"
        return req.reply(200, twiml, ctype="text/xml")
    try:   # Meta Cloud API: record the label (replying would need the notifier's token)
        data = json.loads(raw or "{}")
        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                for msg in ch.get("value", {}).get("messages", []):
                    if msg.get("type") == "text":
                        answer(msg.get("from", ""), msg.get("text", {}).get("body", ""))
    except (ValueError, AttributeError) as e:
        log.warning("bad WhatsApp webhook payload: %s", e)
    return req.reply(200, "ok")


