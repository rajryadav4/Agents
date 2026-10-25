"""Policy recommender and coverage report.

Looks at what actually happened (the audit ledger, incidents, red-team results)
and suggests concrete guardrail rules the operator may want to add, plus a
coverage report showing which sensitive tools still have no enforced rule. This
is a defensive advisory tool: it only ever proposes *stricter* controls, and a
human reviews and applies each suggestion.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from warden.policy.engine import Policy
from warden.policy.lint import lint


def recommend(warden, max_items: int = 10) -> list[dict[str, Any]]:
    """Return suggested rules derived from recent activity. Never auto-applied."""
    suggestions: list[dict[str, Any]] = []
    seen_ids = {r.id for r in warden.policy.rules}

    # 1. Tools repeatedly blocked only by a detector (not yet by an explicit rule)
    #    → propose an explicit, reviewable policy rule so intent is documented.
    blocked_tools: Counter = Counter()
    ext_recipients: Counter = Counter()
    for rec in warden.ledger.records(kind="call"):
        p = rec["payload"]
        if p.get("verdict") in ("deny", "quarantine"):
            detectors = {f.get("detector") for f in p.get("findings", [])}
            if "policy" not in detectors:
                blocked_tools[p["tool"]] += 1

    for tool, n in blocked_tools.most_common():
        if n < 2:
            continue
        rid = f"suggest-restrict-{tool}"
        if rid in seen_ids:
            continue
        suggestions.append({
            "id": rid, "reason": f"{tool} was blocked {n}× by detectors but no explicit policy rule documents this intent.",
            "rule": {"id": rid, "description": f"Require human approval before {tool} (auto-suggested from {n} blocked attempts).",
                     "when": {"tool": tool}, "action": "require_approval", "severity": "medium", "mode": "observe"},
            "confidence": min(0.9, 0.4 + 0.1 * n),
        })

    # 2. Incidents involving exfiltration → suggest a destination allowlist rule.
    exfil = [i for i in warden.incidents.all() if any(d in ("information_flow", "canary", "collusion") for d in i.detectors)]
    if exfil and "suggest-destination-allowlist" not in seen_ids:
        suggestions.append({
            "id": "suggest-destination-allowlist",
            "reason": f"{len(exfil)} exfiltration-type incident(s) recorded. Consider an explicit allowlist of outbound destinations.",
            "rule": {"id": "suggest-destination-allowlist",
                     "description": "Deny outbound HTTP to any host not on the approved integrations list.",
                     "when": {"tool": "http_post", "args": {"url": {"not_glob": "https://*.acme.com/*"}}},
                     "action": "require_approval", "severity": "high", "mode": "observe"},
            "confidence": 0.7,
        })

    # 3. Sensitive tools with no enforced rule (coverage gaps).
    sensitive = [t.name for t in warden.tools.all() if t.category in ("payment", "exec", "write") or t.sink == "external"
                 or t.confidentiality in ("confidential", "secret")]
    for issue in lint(warden.policy, known_tools=warden.tools.names(), sensitive_tools=sensitive):
        if issue["code"] == "uncovered-tool":
            tool = issue["message"].split("'")[1]
            rid = f"suggest-cover-{tool}"
            if rid in seen_ids:
                continue
            suggestions.append({
                "id": rid, "reason": issue["message"],
                "rule": {"id": rid, "description": f"Flag all {tool} calls for review (coverage gap).",
                         "when": {"tool": tool}, "action": "warn", "severity": "low", "mode": "observe"},
                "confidence": 0.5,
            })

    suggestions.sort(key=lambda s: -s["confidence"])
    return suggestions[:max_items]


def coverage(warden) -> dict[str, Any]:
    """Which tools are governed by at least one enforced rule, and which aren't."""
    policy: Policy = warden.policy
    tools = warden.tools.all()
    covered: dict[str, list[str]] = {}
    for rule in policy.rules:
        if rule.mode != "enforce" or not rule.enabled:
            continue
        spec = rule.when.get("tool")
        names = []
        if isinstance(spec, str):
            names = [spec]
        elif isinstance(spec, list):
            names = [str(x) for x in spec]
        elif isinstance(spec, dict):
            for op in ("eq", "glob", "in"):
                if op in spec:
                    v = spec[op]
                    names = [str(x) for x in (v if isinstance(v, list) else [v])]
        import fnmatch

        for t in tools:
            if any(fnmatch.fnmatch(t.name, n) for n in names):
                covered.setdefault(t.name, []).append(rule.id)
    rows = []
    for t in tools:
        risky = t.category in ("payment", "exec", "write") or t.sink == "external" or t.confidentiality in ("confidential", "secret")
        rows.append({"tool": t.name, "category": t.category, "sensitive": risky,
                     "rules": covered.get(t.name, []), "covered": bool(covered.get(t.name))})
    gaps = [r["tool"] for r in rows if r["sensitive"] and not r["covered"]]
    return {"tools": rows, "sensitive_gaps": gaps,
            "coverage_pct": round(sum(1 for r in rows if r["covered"]) / max(1, len(rows)), 3)}
