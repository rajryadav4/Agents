"""Behavioural controls: fingerprints, intent binding, risk budgets, circuit breakers."""
from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from warden.core.models import canonical_json


# --------------------------------------------------------------------------- #
# Behavioural fingerprint: a per-agent Markov model over tool sequences.
# --------------------------------------------------------------------------- #
class BehaviorModel:
    """Learns each agent's normal tool-to-tool transitions.

    After a warm-up period, a call whose transition probability is very low
    (e.g. a support bot that always does db_query → send_email suddenly doing
    read_secret → http_post) produces an anomaly score in [0, 1].
    """

    def __init__(self, warmup: int = 12, alpha: float = 0.5) -> None:
        self.warmup = warmup
        self.alpha = alpha
        self._counts: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        self._last: dict[str, str] = {}
        self._seen: dict[str, int] = defaultdict(int)
        self._tools: dict[str, set[str]] = defaultdict(set)
        self._lock = threading.Lock()

    def score(self, agent_id: str, tool: str) -> dict[str, Any]:
        with self._lock:
            n = self._seen[agent_id]
            prev = self._last.get(agent_id, "^")
            row = self._counts[agent_id][prev]
            vocab = max(1, len(self._tools[agent_id]) + 1)
            total = sum(row.values())
            p = (row.get(tool, 0) + self.alpha) / (total + self.alpha * vocab)
            novel_tool = tool not in self._tools[agent_id]
        surprise = -math.log(max(p, 1e-9))
        # map surprise to [0,1]; novel tools after warm-up get a floor
        score = 1 - math.exp(-surprise / 3.0)
        if novel_tool:
            score = max(score, 0.8)
        warm = n >= self.warmup
        return {"score": round(score if warm else 0.0, 3), "raw": round(score, 3), "warm": warm,
                "transition": f"{prev} → {tool}", "probability": round(p, 4), "novel_tool": novel_tool and warm,
                "observations": n}

    def observe(self, agent_id: str, tool: str) -> None:
        with self._lock:
            prev = self._last.get(agent_id, "^")
            self._counts[agent_id][prev][tool] += 1
            self._last[agent_id] = tool
            self._seen[agent_id] += 1
            self._tools[agent_id].add(tool)

    def profile(self, agent_id: str) -> dict[str, Any]:
        with self._lock:
            trans = {a: dict(b) for a, b in self._counts[agent_id].items()}
            return {"agent_id": agent_id, "observations": self._seen[agent_id], "tools": sorted(self._tools[agent_id]),
                    "transitions": trans, "warm": self._seen[agent_id] >= self.warmup}


# --------------------------------------------------------------------------- #
# Intent binding: the declared purpose of a session constrains its actions.
# --------------------------------------------------------------------------- #
INTENT_LEXICON: dict[str, list[str]] = {
    "support": ["ticket", "customer", "support", "help", "complaint", "order", "reply", "issue", "case", "inquiry"],
    "refund": ["refund", "chargeback", "reimburse", "return", "money back", "credit"],
    "research": ["research", "competitor", "market", "summar", "news", "analy", "investigate", "read", "browse", "report"],
    "ops": ["deploy", "server", "restart", "log", "incident", "disk", "backup", "config", "maintenance", "patch", "build"],
    "comms": ["announce", "notify", "post", "message", "share", "update the team", "slack", "email", "newsletter"],
    "finance": ["invoice", "payment", "ledger", "reconcile", "billing", "payout", "expense"],
    "data": ["query", "database", "export", "dashboard", "metrics", "sql", "table"],
}
TOOL_INTENTS: dict[str, set[str]] = {
    "db_query": {"support", "refund", "data", "finance"},
    "send_email": {"support", "refund", "comms", "finance"},
    "issue_refund": {"refund", "finance"},
    "kb_search": {"support", "research", "refund"},
    "web_fetch": {"research"},
    "slack_post": {"comms", "research", "ops", "support"},
    "message_agent": {"support", "research", "ops", "comms", "refund", "finance", "data"},
    "shell_exec": {"ops"},
    "read_file": {"ops", "data", "research"},
    "write_file": {"ops", "data"},
    "delete_file": {"ops"},
    "read_secret": {"ops"},
    "http_post": {"ops", "comms"},
    "ledger_read": {"finance", "refund"},
    "summarize": {"research", "support", "comms", "data", "ops", "finance", "refund"},
}


def classify_intent(text: str) -> set[str]:
    t = text.lower()
    return {cat for cat, kws in INTENT_LEXICON.items() if any(k in t for k in kws)}


def intent_check(intent: str | None, tool: str) -> dict[str, Any] | None:
    """Return a mismatch description, or None when the tool fits the declared intent."""
    if not intent:
        return None
    cats = classify_intent(intent)
    allowed = TOOL_INTENTS.get(tool)
    if not cats or allowed is None:
        return None
    if cats & allowed:
        return None
    return {"intent": intent, "intent_categories": sorted(cats), "tool": tool, "tool_categories": sorted(allowed)}


# --------------------------------------------------------------------------- #
# Risk budget: every action spends "risk points"; big budgets need a human.
# --------------------------------------------------------------------------- #
@dataclass
class RiskBudget:
    limit: int = 100
    window_s: float = 3600
    _spent: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def cost(tool_risk: int, args: dict[str, Any], external: bool) -> int:
        c = tool_risk
        amount = args.get("amount")
        if isinstance(amount, (int, float)) and amount > 0:
            c += int(min(40, math.log10(amount + 1) * 6))
        if external:
            c += 5
        recipients = args.get("to")
        if isinstance(recipients, list) and len(recipients) > 3:
            c += min(20, len(recipients))
        return c

    def used(self, key: str, now: float | None = None) -> int:
        now = now or time.time()
        with self._lock:
            q = self._spent[key]
            while q and q[0][0] < now - self.window_s:
                q.popleft()
            return sum(c for _, c in q)

    def would_exceed(self, key: str, cost: int) -> bool:
        return self.used(key) + cost > self.limit

    def charge(self, key: str, cost: int) -> None:
        with self._lock:
            self._spent[key].append((time.time(), cost))

    def reset(self, key: str) -> None:
        with self._lock:
            self._spent.pop(key, None)


# --------------------------------------------------------------------------- #
# Circuit breaker: loops, call floods and spend explosions.
# --------------------------------------------------------------------------- #
@dataclass
class CircuitBreaker:
    max_identical: int = 5            # identical calls within window → loop
    identical_window_s: float = 60
    max_rate_per_min: int = 120       # any calls per agent per minute
    max_spend_usd_per_hour: float = 25.0
    cooldown_s: float = 30
    _identical: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _rate: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _spend: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _open_until: dict[str, float] = field(default_factory=dict)
    _open_reason: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @staticmethod
    def call_key(tool: str, args: dict[str, Any]) -> str:
        return tool + ":" + hashlib.sha1(canonical_json(args).encode()).hexdigest()[:12]

    def check(self, agent_id: str, tool: str, args: dict[str, Any], cost_usd: float = 0.0, now: float | None = None) -> str | None:
        now = now or time.time()
        with self._lock:
            until = self._open_until.get(agent_id, 0)
            if until > now:
                return f"circuit open ({self._open_reason.get(agent_id)}); cooling down for {until - now:.0f}s"
            key = agent_id + "|" + self.call_key(tool, args)
            q = self._identical[key]
            while q and q[0] < now - self.identical_window_s:
                q.popleft()
            if len(q) >= self.max_identical:
                return self._trip(agent_id, f"loop detected: same {tool} call repeated {len(q) + 1}x in {self.identical_window_s:.0f}s", now)
            r = self._rate[agent_id]
            while r and r[0] < now - 60:
                r.popleft()
            if len(r) >= self.max_rate_per_min:
                return self._trip(agent_id, f"rate limit: more than {self.max_rate_per_min} calls/minute", now)
            s = self._spend[agent_id]
            while s and s[0][0] < now - 3600:
                s.popleft()
            spent = sum(c for _, c in s)
            if spent + cost_usd > self.max_spend_usd_per_hour:
                return self._trip(agent_id, f"spend limit: ${spent + cost_usd:.2f} would exceed ${self.max_spend_usd_per_hour:.2f}/hour", now)
        return None

    def _trip(self, agent_id: str, reason: str, now: float) -> str:
        self._open_until[agent_id] = now + self.cooldown_s
        self._open_reason[agent_id] = reason
        return reason

    def record(self, agent_id: str, tool: str, args: dict[str, Any], cost_usd: float = 0.0, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            self._identical[agent_id + "|" + self.call_key(tool, args)].append(now)
            self._rate[agent_id].append(now)
            if cost_usd:
                self._spend[agent_id].append((now, cost_usd))

    def report_spend(self, agent_id: str, usd: float) -> None:
        with self._lock:
            self._spend[agent_id].append((time.time(), usd))

    def reset(self, agent_id: str) -> None:
        with self._lock:
            self._open_until.pop(agent_id, None)
            for d in (self._rate, self._spend):
                d.pop(agent_id, None)
            for k in [k for k in self._identical if k.startswith(agent_id + "|")]:
                self._identical.pop(k, None)

    def state(self, agent_id: str) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            until = self._open_until.get(agent_id, 0)
            spent = sum(c for t, c in self._spend.get(agent_id, ()) if t > now - 3600)
            rate = sum(1 for t in self._rate.get(agent_id, ()) if t > now - 60)
            return {"open": until > now, "reason": self._open_reason.get(agent_id) if until > now else None,
                    "spend_usd_last_hour": round(spent, 4), "calls_last_minute": rate}


_SPLIT = re.compile(r"[,;\s]+")


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [s for s in _SPLIT.split(str(value)) if s]
