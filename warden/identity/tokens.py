"""Attenuable capability tokens (macaroon-style).

A token is a list of *caveats* bound together by a chained HMAC:

    sig_0 = HMAC(root_key, token_id)
    sig_i = HMAC(sig_{i-1}, caveat_i)

Anyone holding a token can append caveats (narrowing it) and hand the result to a
sub-agent, but nobody can remove a caveat without the root key: doing so breaks
the signature chain.  So delegation can only ever *reduce* privileges.

Supported caveats (all must hold):

    agent = <agent_id>
    tools in <t1>,<t2>,...
    expires < <unix_ts>
    max_calls <= <n>
    budget_usd <= <x>
    session = <session_id>
    arg <tool>.<arg> glob <pattern>
    tid = <child_token_id>          # lineage marker added on every attenuation
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PREFIX = "wdn1"


class TokenError(Exception):
    pass


@dataclass
class Token:
    tid: str
    caveats: list[str]
    signature: str

    def serialize(self) -> str:
        body = base64.urlsafe_b64encode(json.dumps({"id": self.tid, "c": self.caveats}).encode()).decode().rstrip("=")
        return f"{PREFIX}.{body}.{self.signature}"

    @property
    def lineage(self) -> list[str]:
        return [self.tid] + [c.split("=", 1)[1].strip() for c in self.caveats if c.startswith("tid =")]

    @property
    def leaf_id(self) -> str:
        return self.lineage[-1]

    def caveat_values(self, key: str) -> list[str]:
        out = []
        for c in self.caveats:
            if c.startswith(key + " "):
                out.append(c[len(key):].strip())
        return out

    def agent(self) -> str | None:
        vals = [v.split("=", 1)[1].strip() for v in self.caveat_values("agent")]
        return vals[-1] if vals else None

    def allowed_tools(self) -> set[str] | None:
        """Intersection of every ``tools in`` caveat, or None when unrestricted."""
        allowed: set[str] | None = None
        for v in self.caveat_values("tools"):
            names = {t.strip() for t in v.split("in", 1)[1].split(",") if t.strip()}
            allowed = names if allowed is None else allowed & names
        return allowed

    def expires(self) -> float | None:
        vals = [float(v.split("<", 1)[1]) for v in self.caveat_values("expires")]
        return min(vals) if vals else None

    def describe(self) -> dict[str, Any]:
        tools = self.allowed_tools()
        return {
            "tid": self.tid,
            "leaf_id": self.leaf_id,
            "agent": self.agent(),
            "tools": sorted(tools) if tools is not None else None,
            "expires": self.expires(),
            "caveats": list(self.caveats),
            "depth": len(self.lineage) - 1,
        }


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def parse(serialized: str) -> Token:
    try:
        prefix, body, sig = serialized.split(".")
        if prefix != PREFIX:
            raise ValueError("bad prefix")
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
        caveats = data["c"]
        if not isinstance(caveats, list) or not all(isinstance(c, str) for c in caveats):
            raise ValueError("bad caveats")
        return Token(tid=str(data["id"]), caveats=caveats, signature=sig)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a malformed token
        raise TokenError(f"malformed token: {exc}") from exc


def attenuate(serialized: str, *new_caveats: str) -> str:
    """Narrow a token. Needs no secret: only the previous signature."""
    tok = parse(serialized)
    sig = bytes.fromhex(tok.signature)
    child_id = "tok_" + uuid.uuid4().hex[:12]
    caveats = list(tok.caveats)
    for c in (*new_caveats, f"tid = {child_id}"):
        _validate_caveat(c)
        sig = _sign(sig, c)
        caveats.append(c)
    return Token(tid=tok.tid, caveats=caveats, signature=sig.hex()).serialize()


_CAVEAT_KEYS = ("agent =", "tools in", "expires <", "max_calls <=", "budget_usd <=", "session =", "arg ", "tid =")


def _validate_caveat(c: str) -> None:
    if not isinstance(c, str) or not c.startswith(_CAVEAT_KEYS):
        raise TokenError(f"unknown caveat: {c!r}")
    if "\n" in c:
        raise TokenError("caveats cannot contain newlines")


@dataclass
class CheckResult:
    ok: bool
    reason: str = ""
    token: Token | None = None


@dataclass
class TokenAuthority:
    """Mints, verifies and revokes capability tokens."""

    root_key: bytes = field(default_factory=lambda: uuid.uuid4().bytes + uuid.uuid4().bytes)
    revoked: set[str] = field(default_factory=set)
    _calls: dict[str, int] = field(default_factory=dict)
    _spend: dict[str, float] = field(default_factory=dict)
    _issued: dict[str, dict[str, Any]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    # Returns the delegation ancestors of an agent, so a token minted for a parent
    # can be attenuated with ``agent = <child>`` and handed to that sub-agent.
    ancestors: Any = None

    def mint(self, agent_id: str, tools: list[str] | None = None, ttl_s: float | None = 3600,
             max_calls: int | None = None, budget_usd: float | None = None, extra: list[str] | None = None) -> str:
        tid = "tok_" + uuid.uuid4().hex[:12]
        caveats = [f"agent = {agent_id}"]
        if tools is not None:
            caveats.append("tools in " + ",".join(tools))
        if ttl_s is not None:
            caveats.append(f"expires < {time.time() + ttl_s:.0f}")
        if max_calls is not None:
            caveats.append(f"max_calls <= {max_calls}")
        if budget_usd is not None:
            caveats.append(f"budget_usd <= {budget_usd}")
        caveats.extend(extra or [])
        sig = _sign(self.root_key, tid)
        for c in caveats:
            _validate_caveat(c)
            sig = _sign(sig, c)
        token = Token(tid=tid, caveats=caveats, signature=sig.hex()).serialize()
        with self._lock:
            self._issued[tid] = {"tid": tid, "agent": agent_id, "issued_at": time.time(), "tools": tools}
        return token

    def _signature_ok(self, tok: Token) -> bool:
        sig = _sign(self.root_key, tok.tid)
        for c in tok.caveats:
            sig = _sign(sig, c)
        return hmac.compare_digest(sig.hex(), tok.signature)

    def verify(self, serialized: str | None, agent_id: str, tool: str, args: dict[str, Any] | None = None,
               session_id: str | None = None, cost_usd: float = 0.0, consume: bool = True) -> CheckResult:
        if not serialized:
            return CheckResult(False, "no capability token presented")
        try:
            tok = parse(serialized)
        except TokenError as exc:
            return CheckResult(False, str(exc))
        if not self._signature_ok(tok):
            return CheckResult(False, "signature invalid (token forged or caveats stripped)")
        with self._lock:
            revoked = [t for t in tok.lineage if t in self.revoked]
        if revoked:
            return CheckResult(False, f"token revoked ({revoked[0]})", tok)
        args = args or {}
        now = time.time()
        for c in tok.caveats:
            ok, why = self._check_caveat(c, tok, agent_id, tool, args, session_id, now, cost_usd)
            if not ok:
                return CheckResult(False, why, tok)
        if consume:
            with self._lock:
                self._calls[tok.leaf_id] = self._calls.get(tok.leaf_id, 0) + 1
                self._spend[tok.leaf_id] = self._spend.get(tok.leaf_id, 0.0) + cost_usd
        return CheckResult(True, "ok", tok)

    def _check_caveat(self, c: str, tok: Token, agent_id: str, tool: str, args: dict[str, Any],
                      session_id: str | None, now: float, cost: float) -> tuple[bool, str]:
        if c.startswith("agent ="):
            want = c.split("=", 1)[1].strip()
            lineage = [agent_id] + (list(self.ancestors(agent_id)) if self.ancestors else [])
            return (want in lineage, f"token belongs to agent '{want}', not '{agent_id}'")
        if c.startswith("tools in"):
            names = {t.strip() for t in c.split("in", 1)[1].split(",") if t.strip()}
            return (tool in names, f"tool '{tool}' is not granted by this token")
        if c.startswith("expires <"):
            return (now < float(c.split("<", 1)[1]), "token expired")
        if c.startswith("max_calls <="):
            limit = int(c.split("<=", 1)[1])
            with self._lock:
                used = self._calls.get(tok.leaf_id, 0)
            return (used < limit, f"token call quota exhausted ({limit})")
        if c.startswith("budget_usd <="):
            limit = float(c.split("<=", 1)[1])
            with self._lock:
                spent = self._spend.get(tok.leaf_id, 0.0)
            return (spent + cost <= limit + 1e-9, f"token spend budget exhausted (${limit})")
        if c.startswith("session ="):
            want = c.split("=", 1)[1].strip()
            return (want == session_id, "token is bound to a different session")
        if c.startswith("arg "):
            # arg <tool>.<arg> glob <pattern>
            try:
                target, _, pattern = c[4:].split(" ", 2)
                t_name, a_name = target.split(".", 1)
            except ValueError:
                return (False, f"malformed caveat {c!r}")
            if t_name != tool:
                return (True, "")
            value = args.get(a_name)
            values = value if isinstance(value, list) else [value]
            for v in values:
                if v is None or not fnmatch.fnmatch(str(v).lower(), pattern.lower()):
                    return (False, f"argument '{a_name}'={v!r} violates token constraint '{pattern}'")
            return (True, "")
        if c.startswith("tid ="):
            return (True, "")
        return (False, f"unknown caveat {c!r}")

    def revoke(self, tid: str) -> None:
        with self._lock:
            self.revoked.add(tid)

    def revoke_agent(self, agent_id: str) -> list[str]:
        with self._lock:
            tids = [t for t, meta in self._issued.items() if meta["agent"] == agent_id]
            self.revoked.update(tids)
        return tids

    def issued(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(m, revoked=m["tid"] in self.revoked, calls=self._calls.get(m["tid"], 0))
                     for m in self._issued.values()]
        return [m for m in items if agent_id is None or m["agent"] == agent_id]

    def usage(self, leaf_id: str) -> dict[str, Any]:
        with self._lock:
            return {"calls": self._calls.get(leaf_id, 0), "spend_usd": round(self._spend.get(leaf_id, 0.0), 6)}
