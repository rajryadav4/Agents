"""Tiny HTTP endpoint so 👍 / 👎 buttons on your phone train the model.

    POST /fb/<deal_id>/good?t=<token>    → "good deal" label   (GET shows a confirm button)
    POST /fb/<deal_id>/bad?t=<token>     → "not for me" label
    GET  /go/<deal_id>?t=<token>         → counts a click (weak 👍), redirects to the store
    POST /wa/twilio?t=<wa-token>         → WhatsApp replies via Twilio ("good 123" / "bad 123")
    GET|POST /wa/cloud?t=<wa-token>      → WhatsApp replies via Meta Cloud API (+ webhook verification)

Tokens are an HMAC of the deal id, so nobody can poison your model by guessing URLs.
Your phone must be able to reach this server: run DealHawk on a VPS, or put
your home box on Tailscale, and set feedback.public_url accordingly.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("dealhawk.feedback")


@dataclass
class FeedbackConfig:
    public_url: str = ""         # e.g. https://dealhawk.example.com or http://my-box.tailnet.ts.net:8787
    secret: str = ""
    host: str = "0.0.0.0"
    port: int = 8787
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


def make_server(cfg: FeedbackConfig, brain, store) -> ThreadingHTTPServer:
    route = re.compile(r"^/(fb)/(\d+)/(good|bad)$|^/(go)/(\d+)$")
    wa_token = webhook_token(cfg.secret)

    def handle_reply(sender: str, text: str) -> str:
        if cfg.whatsapp_number and _digits(sender) != _digits(cfg.whatsapp_number):
            return ""
        parsed = parse_reply(text)
        if not parsed:
            return 'Reply like "good 123" or "bad 123" (the number is in each alert).'
        deal_id, label = parsed
        if apply_label(brain, store, deal_id, label):
            return f"Got it — deal {deal_id} {'👍' if label else '👎'}. I'll learn from that."
        return f"I don't know deal {deal_id}."

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code: int, body: str, location: str | None = None, ctype: str = "text/plain"):
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode())

        def _whatsapp(self, u) -> None:
            q = parse_qs(u.query)
            if u.path == "/wa/cloud" and self.command == "GET":     # Meta webhook verification
                if q.get("hub.verify_token", [""])[0] == wa_token:
                    return self._reply(200, q.get("hub.challenge", [""])[0])
                return self._reply(403, "bad verify token")
            if not hmac.compare_digest(q.get("t", [""])[0], wa_token):
                return self._reply(403, "bad token")
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
            if u.path == "/wa/twilio":
                form = parse_qs(raw)
                answer = handle_reply(form.get("From", [""])[0], form.get("Body", [""])[0])
                twiml = f"<Response><Message>{html.escape(answer)}</Message></Response>" if answer else "<Response/>"
                return self._reply(200, twiml, ctype="text/xml")
            try:   # Meta Cloud API: we just record the label (replying needs the notifier's token)
                data = json.loads(raw or "{}")
                for entry in data.get("entry", []):
                    for ch in entry.get("changes", []):
                        for msg in ch.get("value", {}).get("messages", []):
                            if msg.get("type") == "text":
                                handle_reply(msg.get("from", ""), msg.get("text", {}).get("body", ""))
            except (ValueError, AttributeError) as e:
                log.warning("bad WhatsApp webhook payload: %s", e)
            return self._reply(200, "ok")

        def _handle(self):
            u = urlparse(self.path)
            if u.path in ("/wa/twilio", "/wa/cloud"):
                return self._whatsapp(u)
            m = route.match(u.path)
            if not m:
                return self._reply(404, "not found")
            deal_id = int(m.group(2) or m.group(5))
            t = parse_qs(u.query).get("t", [""])[0]
            if not hmac.compare_digest(t, token(cfg.secret, deal_id)):
                return self._reply(403, "bad token")
            if m.group(4) == "go":
                deal = store.get_deal(deal_id)
                if not deal:
                    return self._reply(404, "unknown deal")
                apply_label(brain, store, deal_id, 1, origin="click")
                return self._reply(302, "", location=deal["url"])
            if self.command == "GET":
                # link previewers fetch URLs; only an explicit POST may change the model
                label = "👍 Good deal" if m.group(3) == "good" else "👎 Not for me"
                return self._reply(200, CONFIRM_PAGE.format(action=html.escape(self.path, quote=True), label=label), ctype="text/html")
            ok = apply_label(brain, store, deal_id, 1 if m.group(3) == "good" else 0)
            return self._reply(200 if ok else 404, "thanks — learned 👍" if ok else "unknown deal")

        do_GET = _handle
        do_POST = _handle

        def log_message(self, fmt, *args):  # route through logging, not stderr
            log.debug(fmt, *args)

    return ThreadingHTTPServer((cfg.host, cfg.port), Handler)


def serve_in_background(cfg: FeedbackConfig, brain, store) -> ThreadingHTTPServer:
    srv = make_server(cfg, brain, store)
    threading.Thread(target=srv.serve_forever, daemon=True, name="feedback").start()
    log.info("feedback endpoint on %s:%d (public: %s)", cfg.host, cfg.port, cfg.public_url)
    return srv
