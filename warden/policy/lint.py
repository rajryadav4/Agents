"""Policy linter: catches broken, conflicting and dead rules before they ship."""
from __future__ import annotations

import fnmatch
from typing import Any

from warden.core.models import Mode, Severity, canonical_json
from warden.policy import conditions
from warden.policy.engine import VALID_ACTIONS, Policy


def _issue(level: str, code: str, message: str, rule_id: str | None = None) -> dict[str, Any]:
    return {"level": level, "code": code, "rule_id": rule_id, "message": message}


def _tool_patterns(when: dict[str, Any]) -> list[str] | None:
    spec = (when or {}).get("tool")
    if spec is None:
        return None
    if isinstance(spec, str):
        return [spec]
    if isinstance(spec, list):
        return [str(s) for s in spec]
    if isinstance(spec, dict):
        for op in ("glob", "eq", "in"):
            if op in spec:
                v = spec[op]
                return [str(x) for x in (v if isinstance(v, list) else [v])]
    return None


def lint(policy: Policy, known_tools: list[str] | None = None, sensitive_tools: list[str] | None = None) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    signatures: dict[str, list[Any]] = {}

    for rule in policy.rules:
        rid = rule.id
        if not rid or not str(rid).strip():
            issues.append(_issue("error", "missing-id", "rule without an id"))
        if rid in seen_ids:
            issues.append(_issue("error", "duplicate-id", f"duplicate rule id '{rid}'", rid))
        seen_ids.add(rid)

        if rule.action not in VALID_ACTIONS:
            issues.append(_issue("error", "bad-action", f"action '{rule.action}' is not one of {sorted(VALID_ACTIONS)}", rid))
        if rule.mode not in Mode._value2member_map_:
            issues.append(_issue("error", "bad-mode", f"mode '{rule.mode}' is not observe|warn|enforce", rid))
        if rule.severity not in Severity._value2member_map_:
            issues.append(_issue("error", "bad-severity", f"severity '{rule.severity}' is invalid", rid))
        if rule.action == "require_approval" and (not isinstance(rule.approvals, int) or rule.approvals < 1):
            issues.append(_issue("error", "bad-approvals", "require_approval needs approvals >= 1", rid))
        if rule.action == "require_approval" and isinstance(rule.approvals, int) and rule.approvals > 5:
            issues.append(_issue("warning", "many-approvals", f"{rule.approvals} approvals may stall work", rid))

        for problem in conditions.validate(rule.when) + conditions.validate(rule.unless, "unless"):
            issues.append(_issue("error", "bad-condition", problem, rid))

        if not rule.when:
            issues.append(_issue("warning", "matches-everything", "empty 'when' matches every call", rid))
        if not rule.description:
            issues.append(_issue("info", "no-description", "add a description so humans know why this rule exists", rid))
        if rule.mode == "observe":
            issues.append(_issue("info", "observe-only", "rule is in observe mode and will not block anything", rid))
        if not rule.enabled:
            issues.append(_issue("info", "disabled", "rule is disabled", rid))

        patterns = _tool_patterns(rule.when)
        if known_tools is not None and patterns is not None:
            if not any(fnmatch.fnmatch(t, p) for p in patterns for t in known_tools):
                issues.append(_issue("warning", "unknown-tool", f"tool pattern {patterns} matches no registered tool", rid))

        sig = canonical_json({"when": rule.when, "unless": rule.unless})
        signatures.setdefault(sig, []).append(rule)

    for rules in signatures.values():
        if len(rules) < 2:
            continue
        actions = {r.action for r in rules}
        ids = [r.id for r in rules]
        if len(actions) > 1:
            issues.append(_issue("warning", "conflict",
                                 f"rules {ids} match identical calls but disagree on action {sorted(actions)}; the strictest wins", ids[0]))
        else:
            issues.append(_issue("warning", "duplicate-rule", f"rules {ids} are identical duplicates", ids[0]))

    if sensitive_tools:
        covered = set()
        for rule in policy.rules:
            pats = _tool_patterns(rule.when)
            if pats is None or rule.mode != "enforce" or not rule.enabled:
                continue
            for t in sensitive_tools:
                if any(fnmatch.fnmatch(t, p) for p in pats):
                    covered.add(t)
        for t in sensitive_tools:
            if t not in covered:
                issues.append(_issue("info", "uncovered-tool", f"sensitive tool '{t}' has no enforced rule"))

    order = {"error": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda i: order[i["level"]])
    return issues


def summary(issues: list[dict[str, Any]]) -> dict[str, int]:
    out = {"error": 0, "warning": 0, "info": 0}
    for i in issues:
        out[i["level"]] += 1
    return out
