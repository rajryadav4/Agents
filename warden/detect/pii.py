"""PII detection and a reversible PII vault.

Agents never need to *see* a customer's real email or card number to do their
job. The vault swaps PII for opaque handles like ``⟦pii:email:7f3a2c⟧`` before a
tool result reaches the agent, and swaps them back only when the call is going
to an approved destination (e.g. emailing that same customer). A hijacked agent
that tries to leak data leaks meaningless handles.
"""
from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from typing import Any


def luhn_ok(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(iban: str) -> bool:
    s = re.sub(r"\s+", "", iban).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    rearranged = s[4:] + s[:4]
    num = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(num) % 97 == 1


PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
    "ssn": re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    "phone": re.compile(r"(?<![\w-])(?:\+?\d{1,3}[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]\d{3}[ .-]\d{4}(?![\w-])"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"),
    "aws_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "api_key": re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test|proj)?[-_]?[A-Za-z0-9]{16,}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}
VALIDATORS = {"credit_card": luhn_ok, "iban": iban_ok}
HANDLE = re.compile(r"⟦pii:([a-z_0-9]+):([0-9a-f]{10})⟧")


@dataclass
class PIIMatch:
    kind: str
    value: str
    start: int
    end: int


def find_pii(text: str, kinds: list[str] | None = None) -> list[PIIMatch]:
    if not text:
        return []
    found: list[PIIMatch] = []
    taken: list[tuple[int, int]] = []
    order = ["private_key", "jwt", "aws_key", "api_key", "email", "iban", "credit_card", "ssn", "phone", "ipv4"]
    for kind in order:
        if kinds and kind not in kinds:
            continue
        for m in PATTERNS[kind].finditer(text):
            s, e = m.span()
            if any(s < te and e > ts for ts, te in taken):
                continue
            if HANDLE.search(text[max(0, s - 20):e + 20]) and "⟦" in text[max(0, s - 20):s]:
                continue
            v = VALIDATORS.get(kind)
            if v and not v(m.group()):
                continue
            found.append(PIIMatch(kind, m.group(), s, e))
            taken.append((s, e))
    found.sort(key=lambda m: m.start)
    return found


class PIIVault:
    def __init__(self, secret: bytes = b"warden-pii") -> None:
        self._secret = secret
        self._store: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def handle_for(self, kind: str, value: str) -> str:
        digest = hashlib.blake2b(value.encode(), key=self._secret, digest_size=5).hexdigest()
        with self._lock:
            self._store[digest] = (kind, value)
        return f"⟦pii:{kind}:{digest}⟧"

    def tokenize_text(self, text: str, kinds: list[str] | None = None) -> tuple[str, list[str]]:
        matches = find_pii(text, kinds)
        if not matches:
            return text, []
        out, last, kinds_found = [], 0, []
        for m in matches:
            out.append(text[last:m.start])
            out.append(self.handle_for(m.kind, m.value))
            kinds_found.append(m.kind)
            last = m.end
        out.append(text[last:])
        return "".join(out), kinds_found

    def tokenize(self, value: Any, kinds: list[str] | None = None) -> tuple[Any, list[str]]:
        found: list[str] = []

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                t, k = self.tokenize_text(v, kinds)
                found.extend(k)
                return t
            if isinstance(v, dict):
                return {key: walk(val) for key, val in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v

        return walk(value), found

    def lookup(self, digest: str) -> tuple[str, str] | None:
        with self._lock:
            return self._store.get(digest)

    def handles_in(self, value: Any) -> list[tuple[str, str]]:
        from warden.core.models import flatten_text

        return [(m.group(1), m.group(2)) for m in HANDLE.finditer(flatten_text(value))]

    def detokenize(self, value: Any, allow: Any = None) -> Any:
        """Replace handles with real values. ``allow(kind, real_value) -> bool`` gates each one."""

        def sub(m: re.Match) -> str:
            entry = self.lookup(m.group(2))
            if not entry:
                return m.group(0)
            kind, real = entry
            if allow is not None and not allow(kind, real):
                return f"[redacted {kind}]"
            return real

        def walk(v: Any) -> Any:
            if isinstance(v, str):
                return HANDLE.sub(sub, v)
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            return v

        return walk(value)

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
