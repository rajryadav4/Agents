"""Information-flow control (taint tracking) across agents.

Every tool output is labelled on its way back to an agent:

* **confidentiality** — data from confidential/secret sources (customer DB, secrets store)
* **integrity**       — data from untrusted sources (web pages, inbound email, tickets)

WARDEN remembers distinctive fragments of labelled data ("atoms" such as email
addresses, keys, IDs, URLs, plus 6-word shingles of prose). When an agent later
calls a *sink* tool, its arguments are checked for those fragments:

* confidential data flowing to an external sink         → exfiltration (deny)
* untrusted data choosing a destination or a command    → injection-driven action (deny / approval)

Because the store is shared by all agents, WARDEN also catches **laundering**:
agent A reads customer data, hands it to agent B over ``message_agent``, and B
emails it out. The finding includes the full A → B → sink path.
"""
from __future__ import annotations

import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

from warden.core.models import flatten_text

_WORDS = re.compile(r"[a-z0-9@._%+-]+")
_ATOMS = [
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),          # emails
    re.compile(r"https?://[^\s\"'<>)]+"),                                    # urls
    re.compile(r"⟦pii:[a-z_0-9]+:[0-9a-f]{10}⟧"),                            # vault handles
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\b(?:sk|pk|rk)[-_][A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{10,}\b"),  # ids / tokens
    re.compile(r"\b\d{6,}\b"),                                                # long numbers
]
SHINGLE = 6
GENERIC_ATOMS = {"https://", "http://"}


def atoms(text: str) -> set[str]:
    out: set[str] = set()
    for pat in _ATOMS:
        for m in pat.finditer(text):
            a = m.group(0).strip(".,;:").lower()
            if len(a) >= 6 and a not in GENERIC_ATOMS:
                out.add(a)
    return out


def shingles(text: str, k: int = SHINGLE) -> set[int]:
    words = _WORDS.findall(text.lower())
    if len(words) < k:
        return set()
    return {zlib.crc32(" ".join(words[i:i + k]).encode()) for i in range(len(words) - k + 1)}


@dataclass
class TaintRecord:
    label: str  # "confidential" | "secret" | "untrusted"
    origin_agent: str
    origin_tool: str
    origin_call: str
    ts: float = field(default_factory=time.time)
    note: str = ""


@dataclass
class FlowHit:
    label: str
    record: TaintRecord
    fragment: str
    arg: str


class TaintTracker:
    def __init__(self, max_entries: int = 200_000) -> None:
        self._atoms: dict[str, list[TaintRecord]] = {}
        self._shingles: dict[int, list[TaintRecord]] = {}
        self._edges: list[dict[str, Any]] = []  # agent → agent messages
        self._lock = threading.Lock()
        self._max = max_entries
        self.domain_allowlist: set[str] = set()

    # ------------------------------------------------------------- labelling
    def label_output(self, output: Any, label: str, agent_id: str, tool: str, call_id: str, note: str = "") -> int:
        text = flatten_text(output)
        rec = TaintRecord(label=label, origin_agent=agent_id, origin_tool=tool, origin_call=call_id, note=note)
        new_atoms = {a for a in atoms(text) if not self._allowlisted(a)}
        new_sh = shingles(text)
        with self._lock:
            if len(self._atoms) + len(self._shingles) > self._max:
                self._atoms.clear()
                self._shingles.clear()
            for a in new_atoms:
                self._atoms.setdefault(a, []).append(rec)
            for s in new_sh:
                self._shingles.setdefault(s, []).append(rec)
        return len(new_atoms) + len(new_sh)

    def _allowlisted(self, atom: str) -> bool:
        return any(atom.endswith("@" + d) or f"://{d}" in atom or atom.endswith("." + d) for d in self.domain_allowlist)

    def record_message(self, src: str, dst: str, call_id: str, text: str) -> None:
        with self._lock:
            self._edges.append({"from": src, "to": dst, "call_id": call_id, "ts": time.time()})
        # the message body now "belongs" to the receiver as well: propagate labels
        hits = self.trace(text)
        for h in hits:
            self.label_output(h.fragment, h.label, dst, "message_agent", call_id, note=f"relayed from {src}")

    # --------------------------------------------------------------- tracing
    def trace(self, value: Any, arg: str = "") -> list[FlowHit]:
        text = flatten_text(value)
        hits: list[FlowHit] = []
        seen: set[tuple[str, str]] = set()
        with self._lock:
            for a in atoms(text):
                for rec in self._atoms.get(a, []):
                    key = (rec.label, rec.origin_call)
                    if key not in seen:
                        seen.add(key)
                        hits.append(FlowHit(rec.label, rec, a, arg))
            words = _WORDS.findall(text.lower())
            for i in range(len(words) - SHINGLE + 1):
                frag = " ".join(words[i:i + SHINGLE])
                for rec in self._shingles.get(zlib.crc32(frag.encode()), []):
                    key = (rec.label, rec.origin_call)
                    if key not in seen:
                        seen.add(key)
                        hits.append(FlowHit(rec.label, rec, frag, arg))
        return hits

    def trace_args(self, args: dict[str, Any], only: list[str] | None = None) -> list[FlowHit]:
        hits: list[FlowHit] = []
        for name, value in args.items():
            if only is not None and name not in only:
                continue
            hits.extend(self.trace(value, name))
        return hits

    def path(self, src: str, dst: str) -> list[str] | None:
        """Shortest agent-to-agent message path src → … → dst (BFS)."""
        if src == dst:
            return [src]
        with self._lock:
            adj: dict[str, set[str]] = {}
            for e in self._edges:
                adj.setdefault(e["from"], set()).add(e["to"])
        frontier, prev = [src], {src: None}
        while frontier:
            nxt = []
            for node in frontier:
                for n in adj.get(node, ()):
                    if n not in prev:
                        prev[n] = node
                        if n == dst:
                            out = [n]
                            while prev[out[-1]] is not None:
                                out.append(prev[out[-1]])
                            return list(reversed(out))
                        nxt.append(n)
            frontier = nxt
        return None

    def edges(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._edges)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"atoms": len(self._atoms), "shingles": len(self._shingles), "messages": len(self._edges)}
