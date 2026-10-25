"""Canary honeytokens.

WARDEN plants convincing fake secrets (AWS keys, API tokens, a fake customer
record, a fake DB password) in places only a misbehaving agent would touch or
leak. They have no legitimate use, so the moment one appears in an outbound
tool call we *know* data is being exfiltrated — zero false positives — and the
agent is quarantined automatically.
"""
from __future__ import annotations

import secrets
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Canary:
    id: str
    kind: str
    value: str
    planted_in: str
    created_at: float = field(default_factory=time.time)
    triggered: int = 0
    last_triggered_by: str | None = None

    def to_dict(self, reveal: bool = False) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "value": self.value if reveal else self.value[:6] + "…",
                "planted_in": self.planted_in, "created_at": self.created_at, "triggered": self.triggered,
                "last_triggered_by": self.last_triggered_by}


def _rand(alphabet: str, n: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


GENERATORS = {
    "aws_key": lambda: "AKIA" + _rand(string.ascii_uppercase + string.digits, 16),
    "api_key": lambda: "sk_live_" + _rand(string.ascii_letters + string.digits, 24),
    "db_password": lambda: "pw-" + _rand(string.ascii_letters + string.digits, 18),
    "email": lambda: f"j.{_rand(string.ascii_lowercase, 6)}.{_rand(string.digits, 3)}@acme-vip.com",
    "url": lambda: f"https://internal-{_rand(string.ascii_lowercase, 8)}.acme.corp/admin",
}


class CanaryManager:
    def __init__(self) -> None:
        self._canaries: dict[str, Canary] = {}
        self._lock = threading.Lock()

    def plant(self, kind: str, planted_in: str, value: str | None = None) -> Canary:
        if kind not in GENERATORS and value is None:
            raise ValueError(f"unknown canary kind {kind!r}")
        c = Canary(id="cny_" + secrets.token_hex(4), kind=kind, value=value or GENERATORS[kind](), planted_in=planted_in)
        with self._lock:
            self._canaries[c.id] = c
        return c

    def scan(self, text: str) -> list[Canary]:
        if not text:
            return []
        low = text.lower()
        with self._lock:
            return [c for c in self._canaries.values() if c.value.lower() in low]

    def trigger(self, canary: Canary, agent_id: str) -> None:
        with self._lock:
            canary.triggered += 1
            canary.last_triggered_by = agent_id

    def all(self) -> list[Canary]:
        with self._lock:
            return list(self._canaries.values())

    def __len__(self) -> int:
        return len(self._canaries)
