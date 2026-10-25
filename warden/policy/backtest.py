"""Policy time-machine: replay recorded history under a candidate policy.

Before you ship a rule change, see exactly which past calls it would have
blocked (catching attacks you missed) or released (loosening you didn't mean).
"""
from __future__ import annotations

from typing import Any

from warden.core.models import VERDICT_RANK, ToolCall, Verdict, aggregate_verdict
from warden.policy.engine import Policy


def backtest(records: list[dict[str, Any]], current: Policy, candidate: Policy,
             roles: dict[str, str] | None = None, limit_examples: int = 25) -> dict[str, Any]:
    roles = roles or {}
    tighter, looser, unchanged = [], [], 0
    per_rule: dict[str, int] = {}
    total = 0
    for rec in records:
        if rec.get("kind") != "call":
            continue
        p = rec["payload"]
        call = ToolCall(agent_id=p["agent_id"], tool=p["tool"], args=p.get("args") or {}, ts=rec["ts"],
                        call_id=p.get("call_id", ""))
        role = roles.get(call.agent_id)
        old = aggregate_verdict(current.evaluate(call, role))
        new_findings = candidate.evaluate(call, role)
        new = aggregate_verdict(new_findings)
        for f in new_findings:
            per_rule[f.rule_id or "?"] = per_rule.get(f.rule_id or "?", 0) + 1
        total += 1
        example = {"seq": rec["seq"], "agent_id": call.agent_id, "tool": call.tool, "args": call.args,
                   "before": old.value, "after": new.value, "rules": [f.rule_id for f in new_findings],
                   "recorded_verdict": p.get("verdict")}
        if VERDICT_RANK[new] > VERDICT_RANK[old]:
            tighter.append(example)
        elif VERDICT_RANK[new] < VERDICT_RANK[old]:
            looser.append(example)
        else:
            unchanged += 1
    return {
        "calls_replayed": total,
        "newly_blocked_or_flagged": len(tighter),
        "newly_allowed": len(looser),
        "unchanged": unchanged,
        "rule_hits": dict(sorted(per_rule.items(), key=lambda kv: -kv[1])),
        "examples_tighter": tighter[:limit_examples],
        "examples_looser": looser[:limit_examples],
        "verdict_scale": [v.value for v in Verdict],
    }
