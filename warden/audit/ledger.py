"""Flight recorder: an append-only, hash-chained ledger with Merkle inclusion proofs.

Every record stores the hash of the previous record, so editing, deleting or
re-ordering any historical record breaks the chain and is detected by
:meth:`Ledger.verify`.  Merkle proofs let you prove a single record is part of a
signed checkpoint without revealing the rest of the log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
import time
from typing import Any

from warden.core.models import canonical_json

GENESIS = "0" * 64


def sha256(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def record_hash(prev_hash: str, seq: int, ts: float, kind: str, agent_id: str | None, payload: str) -> str:
    return sha256(canonical_json([prev_hash, seq, repr(ts), kind, agent_id, payload]))


class Ledger:
    def __init__(self, path: str = ":memory:", signing_key: bytes = b"warden-ledger") -> None:
        self.path = path
        self._key = signing_key
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS ledger (
                seq INTEGER PRIMARY KEY,
                ts REAL NOT NULL,
                kind TEXT NOT NULL,
                agent_id TEXT,
                payload TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                hash TEXT NOT NULL)"""
        )
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, upto_seq INTEGER, root TEXT, signature TEXT)"""
        )
        self._db.commit()

    # ------------------------------------------------------------------ write
    def append(self, kind: str, payload: dict[str, Any], agent_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (row[0] + 1) if row else 1
            prev = row[1] if row else GENESIS
            ts = time.time()
            body = canonical_json(payload)
            h = record_hash(prev, seq, ts, kind, agent_id, body)
            self._db.execute(
                "INSERT INTO ledger (seq, ts, kind, agent_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
                (seq, ts, kind, agent_id, body, prev, h),
            )
            self._db.commit()
            return {"seq": seq, "ts": ts, "kind": kind, "agent_id": agent_id, "hash": h, "prev_hash": prev}

    # ------------------------------------------------------------------- read
    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0]

    def head(self) -> str:
        with self._lock:
            row = self._db.execute("SELECT hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
            return row[0] if row else GENESIS

    def records(self, kind: str | None = None, agent_id: str | None = None, limit: int | None = None,
                after_seq: int = 0, newest_first: bool = False) -> list[dict[str, Any]]:
        q = "SELECT seq, ts, kind, agent_id, payload, prev_hash, hash FROM ledger WHERE seq > ?"
        params: list[Any] = [after_seq]
        if kind:
            q += " AND kind = ?"
            params.append(kind)
        if agent_id:
            q += " AND agent_id = ?"
            params.append(agent_id)
        q += " ORDER BY seq " + ("DESC" if newest_first else "ASC")
        if limit:
            q += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._db.execute(q, params).fetchall()
        return [
            {"seq": r[0], "ts": r[1], "kind": r[2], "agent_id": r[3], "payload": json.loads(r[4]),
             "prev_hash": r[5], "hash": r[6]}
            for r in rows
        ]

    def get(self, seq: int) -> dict[str, Any] | None:
        recs = self.records(after_seq=seq - 1, limit=1)
        return recs[0] if recs and recs[0]["seq"] == seq else None

    # ----------------------------------------------------------------- verify
    def verify(self) -> dict[str, Any]:
        """Walk the full chain and report the first broken link, if any."""
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, ts, kind, agent_id, payload, prev_hash, hash FROM ledger ORDER BY seq ASC"
            ).fetchall()
        prev = GENESIS
        expected_seq = 1
        for seq, ts, kind, agent_id, payload, prev_hash, h in rows:
            if seq != expected_seq:
                return {"ok": False, "count": len(rows), "broken_at": expected_seq, "reason": "missing or reordered record"}
            if prev_hash != prev:
                return {"ok": False, "count": len(rows), "broken_at": seq, "reason": "prev_hash mismatch"}
            if record_hash(prev_hash, seq, ts, kind, agent_id, payload) != h:
                return {"ok": False, "count": len(rows), "broken_at": seq, "reason": "record content was modified"}
            prev = h
            expected_seq += 1
        return {"ok": True, "count": len(rows), "head": prev, "broken_at": None, "reason": None}

    # ----------------------------------------------------------------- merkle
    def _leaf_hashes(self, upto_seq: int | None = None) -> list[str]:
        # Recompute each leaf hash from the record's content rather than trusting
        # the stored hash column, so editing a payload invalidates the Merkle root
        # (and therefore any signed checkpoint) even if the hash column is left alone.
        q = "SELECT seq, ts, kind, agent_id, payload, prev_hash FROM ledger"
        params: list[Any] = []
        if upto_seq is not None:
            q += " WHERE seq <= ?"
            params.append(upto_seq)
        q += " ORDER BY seq ASC"
        with self._lock:
            rows = self._db.execute(q, params).fetchall()
        return [record_hash(r[5], r[0], r[1], r[2], r[3], r[4]) for r in rows]

    def merkle_root(self, upto_seq: int | None = None) -> str:
        return merkle_root(self._leaf_hashes(upto_seq))

    def proof(self, seq: int, upto_seq: int | None = None) -> dict[str, Any]:
        leaves = self._leaf_hashes(upto_seq)
        if seq < 1 or seq > len(leaves):
            raise KeyError(f"no ledger record {seq}")
        path = merkle_proof(leaves, seq - 1)
        return {"seq": seq, "leaf": leaves[seq - 1], "path": path, "root": merkle_root(leaves), "size": len(leaves)}

    def checkpoint(self) -> dict[str, Any]:
        """Sign the current Merkle root. Publish this somewhere outside WARDEN's control."""
        with self._lock:
            n = len(self)
            root = self.merkle_root()
            sig = hmac.new(self._key, f"{n}:{root}".encode(), hashlib.sha256).hexdigest()
            ts = time.time()
            self._db.execute("INSERT INTO checkpoints (ts, upto_seq, root, signature) VALUES (?,?,?,?)", (ts, n, root, sig))
            self._db.commit()
        return {"ts": ts, "upto_seq": n, "root": root, "signature": sig}

    def verify_checkpoint(self, cp: dict[str, Any]) -> bool:
        expected = hmac.new(self._key, f"{cp['upto_seq']}:{cp['root']}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, cp["signature"]):
            return False
        return self.merkle_root(cp["upto_seq"]) == cp["root"]

    def checkpoints(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT ts, upto_seq, root, signature FROM checkpoints ORDER BY id DESC").fetchall()
        return [{"ts": r[0], "upto_seq": r[1], "root": r[2], "signature": r[3]} for r in rows]

    # ------------------------------------------------------- test-only helper
    def _tamper(self, seq: int, payload: dict[str, Any]) -> None:
        """Simulate an attacker editing history (used by tests and the demo)."""
        with self._lock:
            self._db.execute("UPDATE ledger SET payload = ? WHERE seq = ?", (canonical_json(payload), seq))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _pair(a: str, b: str) -> str:
    return sha256("node:" + a + b)


def merkle_root(leaves: list[str]) -> str:
    if not leaves:
        return GENESIS
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(leaves: list[str], index: int) -> list[dict[str, str]]:
    path: list[dict[str, str]] = []
    level = list(leaves)
    idx = index
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = idx ^ 1
        path.append({"hash": level[sibling], "side": "left" if sibling < idx else "right"})
        level = [_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        idx //= 2
    return path


def verify_proof(leaf: str, path: list[dict[str, str]], root: str) -> bool:
    h = leaf
    for step in path:
        h = _pair(step["hash"], h) if step["side"] == "left" else _pair(h, step["hash"])
    return h == root
