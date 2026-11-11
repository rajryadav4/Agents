"""SQLite persistence: price history, sent alerts, and a log of scored deals."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import Deal, Offer

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    price REAL NOT NULL,
    ts REAL NOT NULL,
    source TEXT,
    title TEXT,
    url TEXT
);
CREATE INDEX IF NOT EXISTS obs_key_ts ON observations(key, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    price REAL,
    score INTEGER,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_key ON alerts(key, ts);

CREATE TABLE IF NOT EXISTS deals (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    retailer TEXT,
    title TEXT,
    url TEXT,
    price REAL,
    score INTEGER,
    tier TEXT,
    basis TEXT,
    watch TEXT,
    reasons TEXT,
    notified INTEGER DEFAULT 0,
    explore INTEGER DEFAULT 0,
    features TEXT
);
CREATE INDEX IF NOT EXISTS deals_ts ON deals(ts);

-- what you (or the crowd) said about a deal; the learner trains on these
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    deal_id INTEGER,
    key TEXT NOT NULL,
    label INTEGER NOT NULL,          -- 1 good deal, 0 not for me
    weight REAL NOT NULL,
    origin TEXT NOT NULL,            -- user | click | crowd
    features TEXT NOT NULL,
    ts REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS feedback_once ON feedback(key, origin, deal_id);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- products you asked DealHawk to watch by link
CREATE TABLE IF NOT EXISTS tracked (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    retailer TEXT,
    mode TEXT NOT NULL,              -- target | smart
    target_price REAL,
    active INTEGER NOT NULL DEFAULT 1,
    created_ts REAL NOT NULL,
    last_price REAL,
    low_price REAL,
    last_checked REAL,
    status TEXT,                     -- pending | ok | error
    error TEXT,
    hits INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT NOT NULL,
    price REAL,
    verdict TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (key, price)
);
"""


class Store:
    # A price repeated on every poll would swamp the history, so an unchanged
    # price is only re-sampled this often (seconds).
    RESAMPLE_EVERY = 6 * 3600

    # columns added after the first release; older databases are upgraded on open
    DEAL_COLUMNS = {"typical": "REAL", "discount": "REAL", "glitch": "INTEGER DEFAULT 0",
                    "record_low": "INTEGER DEFAULT 0", "verified": "TEXT", "image": "TEXT",
                    "source": "TEXT", "category": "TEXT", "trust": "TEXT"}

    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        have = {r[1] for r in self._db.execute("PRAGMA table_info(deals)")}
        for col, typ in self.DEAL_COLUMNS.items():
            if col not in have:
                self._db.execute(f"ALTER TABLE deals ADD COLUMN {col} {typ}")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---- price history -------------------------------------------------
    def history(self, key: str, since: float | None = None, limit: int = 500) -> list[float]:
        q = "SELECT price FROM observations WHERE key=?"
        args: list = [key]
        if since:
            q += " AND ts>=?"
            args.append(since)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [r["price"] for r in self._db.execute(q, args)]

    def record(self, offer: Offer, ts: float | None = None) -> bool:
        """Store a price observation. Returns False if it was a redundant re-sample."""
        if offer.price is None or offer.price <= 0:
            return False
        ts = ts or time.time()
        with self._lock:
            last = self._db.execute(
                "SELECT price, ts FROM observations WHERE key=? ORDER BY ts DESC LIMIT 1", (offer.key,)
            ).fetchone()
            if last and abs(last["price"] - offer.price) < 0.005 and ts - last["ts"] < self.RESAMPLE_EVERY:
                return False
            self._db.execute(
                "INSERT INTO observations(key, price, ts, source, title, url) VALUES (?,?,?,?,?,?)",
                (offer.key, offer.price, ts, offer.source, offer.title, offer.url),
            )
            self._db.commit()
        return True

    # ---- alerts / dedupe -------------------------------------------------
    def last_alert(self, key: str) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(
                "SELECT price, score, ts FROM alerts WHERE key=? ORDER BY ts DESC LIMIT 1", (key,)
            ).fetchone()

    def max_price_since(self, key: str, ts: float) -> float | None:
        with self._lock:
            return self._db.execute(
                "SELECT MAX(price) FROM observations WHERE key=? AND ts>?", (key, ts)
            ).fetchone()[0]

    def deal_logged_since(self, key: str, price: float | None, ts: float) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM deals WHERE key=? AND price IS ? AND ts>=? LIMIT 1", (key, price, ts)
            ).fetchone() is not None

    def alerts_since(self, ts: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM alerts WHERE ts>=?", (ts,)).fetchone()[0]

    def save_alert(self, deal: Deal, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO alerts(key, price, score, ts) VALUES (?,?,?,?)",
                (deal.offer.key, deal.offer.price, deal.score.value, ts or time.time()),
            )
            self._db.commit()

    # ---- deal log (what `dealhawk deals` lists) --------------------------
    def log_deal(self, deal: Deal, notified: bool, ts: float | None = None) -> int:
        o, s = deal.offer, deal.score
        with self._lock:
            v = deal.verification
            cur = self._db.execute(
                "INSERT INTO deals(key, ts, retailer, title, url, price, score, tier, basis, watch, reasons,"
                " notified, explore, features, typical, discount, glitch, record_low, verified, image, source,"
                " category, trust) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (o.key, ts or time.time(), o.retailer, o.title, o.url, o.price, s.value, s.tier,
                 s.basis, deal.watch, json.dumps(s.reasons), int(notified), int(deal.explore),
                 json.dumps(deal.features) if deal.features else None,
                 s.typical_price or o.reference_price, s.discount, int(s.glitch_suspect), int(s.record_low),
                 getattr(v, "status", None), o.image, o.source, o.category, o.trust),
            )
            self._db.commit()
            return cur.lastrowid

    def mark_notified(self, deal_id: int) -> None:
        with self._lock:
            self._db.execute("UPDATE deals SET notified=1 WHERE id=?", (deal_id,))
            self._db.commit()

    def get_deal(self, deal_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"] or "[]")
        d["features"] = json.loads(d["features"]) if d["features"] else None
        return d

    # ---- learning ----------------------------------------------------------
    def add_feedback(self, key: str, label: int, weight: float, origin: str, features: dict,
                     deal_id: int | None = None, ts: float | None = None) -> bool:
        """Returns False if this (key, origin, deal) was already labelled; a user's later label replaces theirs."""
        with self._lock:
            if origin == "user":
                self._db.execute("DELETE FROM feedback WHERE key=? AND origin='user' AND deal_id IS ?", (key, deal_id))
            try:
                self._db.execute(
                    "INSERT INTO feedback(deal_id, key, label, weight, origin, features, ts) VALUES (?,?,?,?,?,?,?)",
                    (deal_id, key, int(label), weight, origin, json.dumps(features), ts or time.time()),
                )
            except sqlite3.IntegrityError:
                return False
            self._db.commit()
        return True

    def has_feedback(self, key: str, origin: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM feedback WHERE key=? AND origin=? LIMIT 1",
                                    (key, origin)).fetchone() is not None

    def all_feedback(self) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._db.execute("SELECT * FROM feedback ORDER BY ts")]
        for r in rows:
            r["features"] = json.loads(r["features"])
        return rows

    def unlabeled_deals(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT d.* FROM deals d WHERE NOT EXISTS (SELECT 1 FROM feedback f WHERE f.deal_id=d.id"
                " AND f.origin='user') AND d.features IS NOT NULL ORDER BY d.ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def explores_since(self, ts: float) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM deals WHERE explore=1 AND notified=1 AND ts>=?",
                                    (ts,)).fetchone()[0]

    # ---- tracked products ----------------------------------------------------
    TRACK_FIELDS = {"url", "title", "retailer", "mode", "target_price", "active", "last_price", "low_price",
                    "last_checked", "status", "error", "hits"}

    def add_tracked(self, url: str, mode: str, target_price: float | None = None, title: str | None = None,
                    retailer: str | None = None, ts: float | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO tracked(url, title, retailer, mode, target_price, created_ts, status)"
                " VALUES (?,?,?,?,?,?, 'pending')", (url, title, retailer, mode, target_price, ts or time.time()))
            self._db.commit()
            return cur.lastrowid

    def get_tracked(self, tid: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM tracked WHERE id=?", (tid,)).fetchone()
        return dict(r) if r else None

    def list_tracked(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM tracked" + (" WHERE active=1" if active_only else "") + " ORDER BY id DESC"
        with self._lock:
            return [dict(r) for r in self._db.execute(q)]

    def update_tracked(self, tid: int, **fields) -> None:
        bad = set(fields) - self.TRACK_FIELDS
        if bad:
            raise ValueError(f"unknown tracked fields {sorted(bad)}")
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE tracked SET {cols} WHERE id=?", (*fields.values(), tid))
            self._db.commit()

    def delete_tracked(self, tid: int) -> bool:
        with self._lock:
            n = self._db.execute("DELETE FROM tracked WHERE id=?", (tid,)).rowcount
            self._db.commit()
        return n > 0

    def kv_get(self, k: str) -> str | None:
        with self._lock:
            r = self._db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else None

    def kv_set(self, k: str, v: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO kv(k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
            self._db.commit()

    def llm_cached(self, key: str, price: float | None) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT verdict FROM llm_cache WHERE key=? AND price IS ?", (key, price)).fetchone()
        return json.loads(r[0]) if r else None

    def llm_store(self, key: str, price: float | None, verdict: dict, ts: float | None = None) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO llm_cache(key, price, verdict, ts) VALUES (?,?,?,?)",
                             (key, price, json.dumps(verdict), ts or time.time()))
            self._db.commit()

    def llm_calls_since(self, ts: float) -> int:
        v = self.kv_get("llm_call_log")
        return sum(1 for t in json.loads(v) if t >= ts) if v else 0

    def llm_note_call(self, ts: float) -> None:
        v = self.kv_get("llm_call_log")
        log = [t for t in (json.loads(v) if v else []) if t >= ts - 86400] + [ts]
        self.kv_set("llm_call_log", json.dumps(log))

    def recent_deals(self, limit: int = 25, min_score: int = 0, since: float | None = None,
                     after_id: int | None = None) -> list[dict]:
        """Newest first. `after_id` returns only deals logged after that row (for live updates)."""
        q = ("SELECT d.*, (SELECT f.label FROM feedback f WHERE f.deal_id=d.id AND f.origin='user'"
             " ORDER BY f.ts DESC LIMIT 1) AS label FROM deals d WHERE d.score>=?")
        args: list = [min_score]
        if since:
            q += " AND d.ts>=?"
            args.append(since)
        if after_id is not None:
            q += " AND d.id>?"
            args.append(after_id)
        q += " ORDER BY d.id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = [dict(r) for r in self._db.execute(q, args)]
        for r in rows:
            r["reasons"] = json.loads(r["reasons"] or "[]")
            r.pop("features", None)
        return rows

    def price_points(self, key: str, limit: int = 200) -> list[tuple[float, float]]:
        """(ts, price) oldest → newest, for the price-history chart."""
        with self._lock:
            rows = self._db.execute("SELECT ts, price FROM observations WHERE key=? ORDER BY ts DESC LIMIT ?",
                                    (key, limit)).fetchall()
        return [(r[0], r[1]) for r in reversed(rows)]

    def set_source_status(self, name: str, status: dict) -> None:
        self.kv_set(f"source:{name}", json.dumps(status))

    def source_statuses(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT v FROM kv WHERE k LIKE 'source:%' ORDER BY k").fetchall()
        return [json.loads(r[0]) for r in rows]

    def stats(self) -> dict:
        with self._lock:
            one = lambda q: self._db.execute(q).fetchone()[0]  # noqa: E731
            return {
                "products_tracked": one("SELECT COUNT(DISTINCT key) FROM observations"),
                "price_points": one("SELECT COUNT(*) FROM observations"),
                "deals_logged": one("SELECT COUNT(*) FROM deals"),
                "alerts_sent": one("SELECT COUNT(*) FROM alerts"),
                "labels_you": one("SELECT COUNT(*) FROM feedback WHERE origin='user'"),
                "labels_crowd": one("SELECT COUNT(*) FROM feedback WHERE origin='crowd'"),
                "labels_clicks": one("SELECT COUNT(*) FROM feedback WHERE origin='click'"),
            }
