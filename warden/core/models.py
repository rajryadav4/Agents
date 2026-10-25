"""Core data types shared by every WARDEN component."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ALLOW = "allow"
    WARN = "warn"  # allowed, but flagged for humans
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    QUARANTINE = "quarantine"


VERDICT_RANK = {
    Verdict.ALLOW: 0,
    Verdict.WARN: 1,
    Verdict.REQUIRE_APPROVAL: 2,
    Verdict.DENY: 3,
    Verdict.QUARANTINE: 4,
}


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_RANK = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class Mode(str, Enum):
    """Progressive enforcement: a rule can be rolled out gradually."""

    OBSERVE = "observe"  # record what *would* happen, change nothing
    WARN = "warn"  # allow, but flag
    ENFORCE = "enforce"  # apply the verdict


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Finding:
    detector: str
    verdict: Verdict
    severity: Severity
    message: str
    rule_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None
    mode: Mode = Mode.ENFORCE

    def effective_verdict(self) -> Verdict:
        if self.mode == Mode.OBSERVE:
            return Verdict.ALLOW
        if self.mode == Mode.WARN and VERDICT_RANK[self.verdict] > VERDICT_RANK[Verdict.WARN]:
            return Verdict.WARN
        return self.verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "verdict": self.verdict.value,
            "effective": self.effective_verdict().value,
            "severity": self.severity.value,
            "message": self.message,
            "rule_id": self.rule_id,
            "evidence": self.evidence,
            "suggestion": self.suggestion,
            "mode": self.mode.value,
        }


@dataclass
class ToolCall:
    agent_id: str
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    token: str | None = None
    call_id: str = field(default_factory=lambda: new_id("call"))
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def args_text(self) -> str:
        """Flatten args into one string for content scanners."""
        return flatten_text(self.args)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("token", None)
        return d


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {flatten_text(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple, set)):
        return "\n".join(flatten_text(v) for v in value)
    return str(value)


def aggregate_verdict(findings: list[Finding]) -> Verdict:
    verdict = Verdict.ALLOW
    for f in findings:
        v = f.effective_verdict()
        if VERDICT_RANK[v] > VERDICT_RANK[verdict]:
            verdict = v
    return verdict


@dataclass
class Decision:
    call_id: str
    verdict: Verdict
    findings: list[Finding]
    risk_cost: int = 0
    explanation: str = ""
    latency_ms: float = 0.0
    approval_id: str | None = None
    incident_id: str | None = None
    approvals_required: int = 0

    @property
    def blocked(self) -> bool:
        return self.verdict in (Verdict.DENY, Verdict.QUARANTINE, Verdict.REQUIRE_APPROVAL)

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "verdict": self.verdict.value,
            "findings": [f.to_dict() for f in self.findings],
            "risk_cost": self.risk_cost,
            "explanation": self.explanation,
            "latency_ms": round(self.latency_ms, 3),
            "approval_id": self.approval_id,
            "incident_id": self.incident_id,
            "approvals_required": self.approvals_required,
        }


class CallStatus(str, Enum):
    EXECUTED = "executed"
    BLOCKED = "blocked"
    PENDING = "pending_approval"
    DECEIVED = "deceived"  # quarantined agent got a honeypot response
    ERROR = "error"


@dataclass
class CallResult:
    status: CallStatus
    decision: Decision
    result: Any = None
    labels: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (CallStatus.EXECUTED, CallStatus.DECEIVED)

    def to_dict(self, reveal_deception: bool = False) -> dict[str, Any]:
        status = self.status
        decision = self.decision.to_dict()
        if status == CallStatus.DECEIVED and not reveal_deception:
            # A quarantined agent must not learn it has been quarantined.
            status = CallStatus.EXECUTED
            decision = {"call_id": self.decision.call_id, "verdict": "allow", "findings": [],
                        "risk_cost": 0, "explanation": "", "latency_ms": decision["latency_ms"],
                        "approval_id": None, "incident_id": None, "approvals_required": 0}
        return {
            "status": status.value,
            "decision": decision,
            "result": self.result,
            "labels": self.labels if reveal_deception or status != CallStatus.DECEIVED else {},
            "error": self.error,
        }


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def truncate(value: Any, limit: int = 240) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"
