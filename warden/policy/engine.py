"""Policy-as-code: the "constitution" every agent call is checked against."""
from __future__ import annotations

import copy
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from warden.core.models import Finding, Mode, Severity, ToolCall, Verdict, canonical_json
from warden.policy import conditions

VALID_ACTIONS = {"deny", "require_approval", "quarantine", "warn"}
_ACTION_TO_VERDICT = {
    "deny": Verdict.DENY,
    "require_approval": Verdict.REQUIRE_APPROVAL,
    "quarantine": Verdict.QUARANTINE,
    "warn": Verdict.WARN,
}


@dataclass
class Rule:
    id: str
    when: dict[str, Any]
    action: str = "deny"
    description: str = ""
    severity: str = "medium"
    mode: str = "enforce"
    approvals: int = 1
    message: str | None = None
    suggestion: str | None = None
    unless: dict[str, Any] | None = None
    tags: list[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        known.setdefault("when", {})
        if "id" not in known:
            raise ValueError("every rule needs an 'id'")
        return cls(**known)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "description": self.description, "when": self.when,
                             "action": self.action, "severity": self.severity, "mode": self.mode}
        if self.action == "require_approval":
            d["approvals"] = self.approvals
        for k in ("message", "suggestion", "unless"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.tags:
            d["tags"] = self.tags
        if not self.enabled:
            d["enabled"] = False
        return d

    def matches(self, ctx: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        if not conditions.evaluate(self.when, ctx):
            return False
        if self.unless and conditions.evaluate(self.unless, ctx):
            return False
        return True

    def finding(self, ctx: dict[str, Any]) -> Finding:
        msg = self.message or self.description or f"policy rule '{self.id}' matched"
        return Finding(
            detector="policy",
            verdict=_ACTION_TO_VERDICT.get(self.action, Verdict.DENY),
            severity=Severity(self.severity) if self.severity in Severity._value2member_map_ else Severity.MEDIUM,
            message=msg,
            rule_id=self.id,
            evidence={"tool": ctx.get("tool"), "approvals": self.approvals if self.action == "require_approval" else 0},
            suggestion=self.suggestion,
            mode=Mode(self.mode) if self.mode in Mode._value2member_map_ else Mode.ENFORCE,
        )


@dataclass
class Policy:
    name: str = "default"
    version: int = 1
    rules: list[Rule] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "Policy":
        d = d or {}
        defaults = d.get("defaults") or {}
        rules = []
        for raw in d.get("rules") or []:
            merged = {**defaults, **raw}
            rules.append(Rule.from_dict(merged))
        return cls(name=d.get("name", "default"), version=int(d.get("version", 1)), rules=rules,
                   settings=dict(d.get("settings") or {}))

    @classmethod
    def from_yaml(cls, text: str) -> "Policy":
        return cls.from_dict(yaml.safe_load(text) or {})

    @classmethod
    def load(cls, path: str) -> "Policy":
        with open(path, encoding="utf-8") as fh:
            return cls.from_yaml(fh.read())

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version, "settings": self.settings,
                "rules": [r.to_dict() for r in self.rules]}

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)

    def fingerprint(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode()).hexdigest()[:16]

    def copy(self) -> "Policy":
        return Policy.from_dict(copy.deepcopy(self.to_dict()))

    def rule(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def upsert(self, rule: Rule) -> None:
        for i, r in enumerate(self.rules):
            if r.id == rule.id:
                self.rules[i] = rule
                break
        else:
            self.rules.append(rule)
        self.updated_at = time.time()

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        self.updated_at = time.time()
        return len(self.rules) != before

    def set_mode(self, rule_id: str, mode: str) -> Rule:
        if mode not in Mode._value2member_map_:
            raise ValueError(f"mode must be one of {[m.value for m in Mode]}")
        r = self.rule(rule_id)
        if not r:
            raise KeyError(rule_id)
        r.mode = mode
        self.updated_at = time.time()
        return r

    def evaluate(self, call: ToolCall, role: str | None = None, extra: dict[str, Any] | None = None) -> list[Finding]:
        ctx = {"tool": call.tool, "agent": call.agent_id, "role": role, "args": call.args, "now": call.ts}
        if extra:
            ctx.update(extra)
        return [r.finding(ctx) for r in self.rules if r.matches(ctx)]


def context_for(call: ToolCall, role: str | None) -> dict[str, Any]:
    return {"tool": call.tool, "agent": call.agent_id, "role": role, "args": call.args, "now": call.ts}
