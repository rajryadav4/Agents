"""Human-in-the-loop approvals with quorum (two-person rule) and expiry."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from warden.core.models import new_id


class ApprovalError(Exception):
    pass


@dataclass
class ApprovalRequest:
    id: str
    call: dict[str, Any]
    reason: str
    required: int
    requested_by: str
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    approvals: list[dict[str, Any]] = field(default_factory=list)
    denials: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"  # pending | approved | denied | expired
    result: Any = None
    result_status: str | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "call": self.call, "reason": self.reason, "required": self.required,
            "requested_by": self.requested_by, "created_at": self.created_at, "expires_at": self.expires_at,
            "approvals": self.approvals, "denials": self.denials, "status": self.status,
            "result": self.result, "result_status": self.result_status, "findings": self.findings,
            "remaining": max(0, self.required - len(self.approvals)),
        }


class ApprovalQueue:
    def __init__(self, ttl_s: float = 3600) -> None:
        self.ttl_s = ttl_s
        self._reqs: dict[str, ApprovalRequest] = {}
        self._lock = threading.RLock()

    def create(self, call: dict[str, Any], reason: str, required: int, requested_by: str,
               findings: list[dict[str, Any]] | None = None) -> ApprovalRequest:
        req = ApprovalRequest(id=new_id("apr"), call=call, reason=reason, required=max(1, int(required)),
                              requested_by=requested_by, expires_at=time.time() + self.ttl_s, findings=findings or [])
        with self._lock:
            self._reqs[req.id] = req
        return req

    def get(self, req_id: str) -> ApprovalRequest:
        with self._lock:
            req = self._reqs.get(req_id)
            if not req:
                raise ApprovalError(f"unknown approval request {req_id}")
            self._expire(req)
            return req

    def _expire(self, req: ApprovalRequest) -> None:
        if req.status == "pending" and time.time() > req.expires_at:
            req.status = "expired"

    def vote(self, req_id: str, approver: str, approve: bool, note: str = "") -> ApprovalRequest:
        if not approver or not approver.strip():
            raise ApprovalError("approver identity is required")
        with self._lock:
            req = self.get(req_id)
            if req.status != "pending":
                raise ApprovalError(f"request is already {req.status}")
            if approver == req.requested_by or approver == req.call.get("agent_id"):
                raise ApprovalError("the requesting agent cannot approve its own action")
            if any(v["by"] == approver for v in req.approvals + req.denials):
                raise ApprovalError(f"{approver} has already voted (quorum needs distinct people)")
            vote = {"by": approver, "at": time.time(), "note": note}
            if approve:
                req.approvals.append(vote)
                if len(req.approvals) >= req.required:
                    req.status = "approved"
            else:
                req.denials.append(vote)
                req.status = "denied"  # any single veto denies
            return req

    def pending(self) -> list[ApprovalRequest]:
        with self._lock:
            for r in self._reqs.values():
                self._expire(r)
            return sorted((r for r in self._reqs.values() if r.status == "pending"), key=lambda r: r.created_at)

    def all(self) -> list[ApprovalRequest]:
        with self._lock:
            for r in self._reqs.values():
                self._expire(r)
            return sorted(self._reqs.values(), key=lambda r: -r.created_at)


@dataclass
class Incident:
    id: str
    title: str
    severity: str
    agent_id: str
    call_ids: list[str]
    detectors: list[str]
    summary: str
    created_at: float = field(default_factory=time.time)
    status: str = "open"  # open | contained | resolved
    actions: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class IncidentLog:
    def __init__(self) -> None:
        self._items: dict[str, Incident] = {}
        self._lock = threading.Lock()

    def open(self, title: str, severity: str, agent_id: str, call_id: str, detectors: list[str], summary: str,
             actions: list[str] | None = None, evidence: dict[str, Any] | None = None) -> Incident:
        with self._lock:
            # coalesce repeated incidents for the same agent + title within 10 minutes
            for inc in self._items.values():
                if inc.agent_id == agent_id and inc.title == title and inc.status != "resolved" and time.time() - inc.created_at < 600:
                    inc.call_ids.append(call_id)
                    for d in detectors:
                        if d not in inc.detectors:
                            inc.detectors.append(d)
                    return inc
            inc = Incident(id=new_id("inc"), title=title, severity=severity, agent_id=agent_id, call_ids=[call_id],
                           detectors=list(detectors), summary=summary, actions=list(actions or []), evidence=evidence or {})
            self._items[inc.id] = inc
            return inc

    def get(self, inc_id: str) -> Incident | None:
        with self._lock:
            return self._items.get(inc_id)

    def all(self) -> list[Incident]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: -i.created_at)

    def set_status(self, inc_id: str, status: str) -> Incident:
        with self._lock:
            inc = self._items[inc_id]
            inc.status = status
            return inc
