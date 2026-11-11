"""The live dashboard: a web page that shows deals the moment DealHawk finds them.

    dealhawk run            → hunts deals AND serves http://localhost:8686
    dealhawk serve          → dashboard only, over an existing dealhawk.db
    dealhawk serve --demo   → clearly-labelled SAMPLE data, to preview the page

Routes
    GET  /                          the dashboard (single static page, no external assets)
    GET  /api/meta                  version, demo flag
    GET  /api/deals?limit&min_score&after_id
    GET  /api/stats                 counters, learner, per-source health
    GET  /api/history/<deal_id>     price history of that product (for the chart)
    GET  /api/stream?after_id=N     Server-Sent Events: pushes each new deal instantly
    POST /api/deals/<id>/label      {"label": "good" | "bad"} — trains the model
    GET  /api/tracked               products you track by link (+ their price history)
    POST /api/tracked               {"url", "mode": "target"|"smart", "target_price"}
    POST /api/tracked/<id>          change mode / target / active
    POST /api/tracked/<id>/check    check the price right now
    DELETE /api/tracked/<id>        stop tracking
    /fb/… /go/… /wa/…               phone feedback routes (feedback.py; token-protected)

Hosting it: set `web.host: 0.0.0.0` and a `web.password` (HTTP Basic auth; any
username). Feedback routes stay reachable for your phone/Twilio/Meta because
they carry their own HMAC tokens.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__
from . import feedback as fb

log = logging.getLogger("dealhawk.web")
INDEX = Path(__file__).with_name("web") / "index.html"


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "127.0.0.1"        # 0.0.0.0 to reach it from other devices / when hosting
    port: int = 8686
    password: str = ""             # required when host isn't localhost


def _int(q: dict, k: str, default: int | None) -> int | None:
    try:
        return int(q[k][0]) if k in q else default
    except (ValueError, IndexError):
        return default


def make_server(web: WebConfig, feedback_cfg: fb.FeedbackConfig, brain, store, demo: bool = False,
                sources: list | None = None, tracker=None) -> ThreadingHTTPServer:
    index_html = INDEX.read_text(encoding="utf-8")
    stopping = threading.Event()
    source_names = [s.name for s in (sources or [])]

    class Handler(BaseHTTPRequestHandler):
        server_version = f"DealHawk/{__version__}"
        protocol_version = "HTTP/1.1"

        # ---- helpers used by feedback.handle too ----
        def reply(self, code: int, body: str | bytes, location: str | None = None, ctype: str = "text/plain",
                  headers: dict | None = None):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(min(n, 1_000_000)) if n else b""

        def json(self, obj, code: int = 200):
            self.reply(code, json.dumps(obj, default=str), ctype="application/json")

        def authorized(self) -> bool:
            if not web.password:
                return True
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic "):
                try:
                    _, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                    if hmac.compare_digest(pw, web.password):
                        return True
                except (ValueError, UnicodeDecodeError):
                    pass
            self.reply(401, "login required", headers={"WWW-Authenticate": 'Basic realm="DealHawk"'})
            return False

        # ---- routing ----
        def _route(self):
            if fb.handle(self, feedback_cfg, brain, store):     # phone routes: own tokens, no password
                return
            if not self.authorized():
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            if self.command == "GET" and path in ("/", "/index.html"):
                return self.reply(200, index_html, ctype="text/html")
            if self.command == "GET" and path == "/api/meta":
                return self.json({"version": __version__, "demo": demo, "now": time.time(),
                                  "sources": source_names})
            if self.command == "GET" and path == "/api/deals":
                return self.json(store.recent_deals(limit=min(_int(q, "limit", 200) or 200, 1000),
                                                    min_score=_int(q, "min_score", 0) or 0,
                                                    after_id=_int(q, "after_id", None)))
            if self.command == "GET" and path == "/api/stats":
                return self.json(self._stats())
            m = re.fullmatch(r"/api/history/(\d+)", path)
            if self.command == "GET" and m:
                deal = store.get_deal(int(m.group(1)))
                if not deal:
                    return self.json({"error": "unknown deal"}, 404)
                return self.json({"key": deal["key"], "points": store.price_points(deal["key"])})
            if self.command == "GET" and path == "/api/stream":
                return self._stream(_int(q, "after_id", None))
            if path == "/api/tracked" or path.startswith("/api/tracked/"):
                return self._tracked(path)
            m = re.fullmatch(r"/api/deals/(\d+)/label", path)
            if self.command == "POST" and m:
                try:
                    label = json.loads(self.body() or b"{}").get("label")
                except ValueError:
                    label = None
                if label not in ("good", "bad"):
                    return self.json({"error": 'label must be "good" or "bad"'}, 400)
                ok = fb.apply_label(brain, store, int(m.group(1)), 1 if label == "good" else 0)
                return self.json({"ok": ok, "labels": brain.learner.n_labels}, 200 if ok else 404)
            return self.reply(404, "not found")

        do_GET = _route
        do_POST = _route
        do_DELETE = _route

        def _tracked(self, path: str):
            from .models import slug
            from .tracking import TrackError
            if tracker is None:
                return self.json({"error": "tracking is turned off"}, 503)
            m = re.fullmatch(r"/api/tracked(?:/(\d+))?(/check)?", path)
            if not m:
                return self.reply(404, "not found")
            tid = int(m.group(1)) if m.group(1) else None
            if tid is None and self.command == "GET":
                items = store.list_tracked()
                for t in items:
                    t["points"] = store.price_points(f"{slug(t['retailer'] or 'Unknown')}:track-{t['id']}", 120)
                return self.json(items)
            if tid is None and self.command == "POST":
                try:
                    data = json.loads(self.body() or b"{}")
                    target = data.get("target_price")
                    t = tracker.add(data.get("url", ""), data.get("mode", "smart"),
                                    float(target) if target not in (None, "") else None)
                except (TrackError, ValueError, TypeError) as e:
                    return self.json({"error": str(e)}, 400)
                return self.json(t, 201)
            if tid is None or not store.get_tracked(tid):
                return self.json({"error": "unknown item"}, 404)
            if m.group(2) and self.command == "POST":
                tracker.check_in_background(tid)
                return self.json({"ok": True, "checking": tid}, 202)
            if self.command == "DELETE":
                return self.json({"ok": store.delete_tracked(tid)})
            if self.command == "POST":
                try:
                    data = json.loads(self.body() or b"{}")
                    fields = {}
                    if "mode" in data:
                        if data["mode"] not in ("target", "smart"):
                            raise ValueError("mode must be target or smart")
                        fields["mode"] = data["mode"]
                    if "target_price" in data:
                        tp = data["target_price"]
                        fields["target_price"] = float(tp) if tp not in (None, "") else None
                    if "active" in data:
                        fields["active"] = 1 if data["active"] else 0
                    t = {**store.get_tracked(tid), **fields}
                    if t["mode"] == "target" and not t["target_price"]:
                        raise ValueError("set the price you want to be alerted at")
                except (ValueError, TypeError) as e:
                    return self.json({"error": str(e)}, 400)
                store.update_tracked(tid, **fields)
                return self.json(store.get_tracked(tid))
            return self.reply(405, "method not allowed")

        def _stats(self) -> dict:
            day = time.time() - 86400
            today = store.recent_deals(limit=1000, since=day)
            L = brain.learner
            return {
                **store.stats(),
                "deals_24h": len(today),
                "steals_24h": sum(1 for d in today if d["tier"] == "steal"),
                "glitches_24h": sum(1 for d in today if d["glitch"]),
                "model_influence": L.confidence,
                "llm": bool(brain.judge),
                "sources": store.source_statuses(),
            }

        def _stream(self, after_id: int | None):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")        # don't let nginx buffer the stream
            self.end_headers()
            self.close_connection = True
            last = after_id
            if last is None:
                newest = store.recent_deals(limit=1)
                last = newest[0]["id"] if newest else 0
            beat = time.monotonic()
            try:
                self.wfile.write(b"retry: 3000\n: connected\n\n")
                self.wfile.flush()
                while not stopping.is_set():
                    new = store.recent_deals(limit=100, after_id=last)
                    for d in reversed(new):
                        self.wfile.write(f"id: {d['id']}\nevent: deal\ndata: {json.dumps(d, default=str)}\n\n"
                                         .encode())
                        last = d["id"]
                    if new:
                        self.wfile.flush()
                    if time.monotonic() - beat > 15:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        beat = time.monotonic()
                    stopping.wait(1.0)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass   # browser went away

        def log_message(self, fmt, *args):
            log.debug("%s " + fmt, self.address_string(), *args)

    try:
        srv = ThreadingHTTPServer((web.host, web.port), Handler)
    except OSError as e:
        if e.errno in (48, 98, 10048):   # address in use (macOS, Linux, Windows)
            raise OSError(e.errno, f"port {web.port} is already in use by another app — set web.port in "
                                   f"dealhawk.yaml or pass --port") from e
        raise
    srv.daemon_threads = True
    _orig_shutdown = srv.shutdown

    def shutdown():
        stopping.set()
        _orig_shutdown()
    srv.shutdown = shutdown
    return srv


def serve_in_background(web: WebConfig, feedback_cfg, brain, store, demo: bool = False,
                        sources: list | None = None, tracker=None) -> ThreadingHTTPServer:
    srv = make_server(web, feedback_cfg, brain, store, demo=demo, sources=sources, tracker=tracker)
    threading.Thread(target=srv.serve_forever, daemon=True, name="web").start()
    host = "localhost" if web.host in ("127.0.0.1", "0.0.0.0", "::") else web.host
    log.info("dashboard: http://%s:%d", host, srv.server_address[1])
    return srv
