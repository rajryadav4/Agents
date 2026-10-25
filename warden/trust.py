"""Trust Score + signed attestation + embeddable badge.

A single, explainable 0–100 score for how well-governed an agent deployment is,
built from five pillars. Each score is signed (HMAC) so a badge can be verified,
and the badge is served as inline SVG (light/dark aware).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

PILLARS = {
    "defense": ("Attack resistance", 0.35, "share of red-team attacks blocked (severity-weighted)"),
    "coverage": ("Policy coverage", 0.20, "share of sensitive tools governed by an enforced rule"),
    "least_privilege": ("Least privilege", 0.15, "agents constrained by scoped capability tokens"),
    "auditability": ("Auditability", 0.15, "audit log present, hash-chain intact, checkpoints signed"),
    "hygiene": ("Policy hygiene", 0.15, "policy free of lint errors and conflicts"),
}

GRADES = [(95, "A+"), (90, "A"), (85, "A-"), (80, "B+"), (75, "B"), (70, "B-"),
          (65, "C+"), (60, "C"), (50, "D"), (0, "F")]


def grade(score: float) -> str:
    for cut, g in GRADES:
        if score >= cut:
            return g
    return "F"


def compute(warden, redteam: dict[str, Any] | None = None) -> dict[str, Any]:
    from warden.policy.lint import lint, summary
    from warden.policy.recommend import coverage

    # defense
    if redteam is None:
        from warden.redteam.attacks import run_campaign

        redteam = run_campaign(policy=warden.policy, settings=warden.settings)
    defense = redteam["weighted_block_rate"]

    # coverage
    cov = coverage(warden)["coverage_pct"]

    # least privilege: active agents that hold scoped tokens
    agents = [a for a in warden.agents.all() if a.status != "killed"]
    issued = warden.tokens.issued()
    scoped = {m["agent"] for m in issued if m.get("tools")}
    least = (len(scoped & {a.id for a in agents}) / len(agents)) if agents else 1.0
    if not warden.settings.get("require_tokens", True):
        least *= 0.3

    # auditability
    v = warden.ledger.verify()
    audit = 0.0
    if len(warden.ledger) > 0:
        audit = 0.6 + (0.3 if v["ok"] else 0.0) + (0.1 if warden.ledger.checkpoints() else 0.0)

    # hygiene
    issues = lint(warden.policy, known_tools=warden.tools.names())
    counts = summary(issues)
    hygiene = max(0.0, 1.0 - 0.34 * counts["error"] - 0.08 * counts["warning"])
    if not warden.policy.rules:
        hygiene = 0.0

    raw = {"defense": defense, "coverage": cov, "least_privilege": least, "auditability": audit, "hygiene": hygiene}
    total = sum(raw[k] * PILLARS[k][1] for k in PILLARS) * 100
    breakdown = {k: {"label": PILLARS[k][0], "weight": PILLARS[k][1], "score": round(raw[k] * 100, 1),
                     "points": round(raw[k] * PILLARS[k][1] * 100, 1), "about": PILLARS[k][2]} for k in PILLARS}
    return {
        "org": warden.org, "score": round(total, 1), "grade": grade(total), "ran_at": time.time(),
        "breakdown": breakdown,
        "evidence": {"attacks_tested": redteam["attacks"], "attacks_blocked": redteam["blocked"],
                     "ledger_records": len(warden.ledger), "ledger_intact": v["ok"],
                     "sensitive_gaps": coverage(warden)["sensitive_gaps"], "lint": counts,
                     "agents": len(agents), "policy_fingerprint": warden.policy.fingerprint()},
    }


def attest(report: dict[str, Any], key: bytes) -> dict[str, Any]:
    body = f"{report['org']}:{report['score']}:{report['grade']}:{int(report['ran_at'])}:{report['evidence']['policy_fingerprint']}"
    sig = hmac.new(key, body.encode(), hashlib.sha256).hexdigest()
    return {**report, "attestation": {"body": body, "signature": sig, "alg": "HMAC-SHA256"}}


def verify_attestation(report: dict[str, Any], key: bytes) -> bool:
    att = report.get("attestation")
    if not att:
        return False
    expected = hmac.new(key, att["body"].encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, att["signature"])


def badge_svg(report: dict[str, Any], dark: bool = False) -> str:
    score, g = report["score"], report["grade"]
    color = "#16a34a" if score >= 85 else "#2563eb" if score >= 70 else "#d97706" if score >= 55 else "#dc2626"
    bg = "#0b1020" if dark else "#ffffff"
    fg = "#e5e7eb" if dark else "#111827"
    sub = "#9ca3af" if dark else "#6b7280"
    border = "#1f2937" if dark else "#e5e7eb"
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="184" height="52" role="img" aria-label="WARDEN score {score}">
  <rect x="0.5" y="0.5" width="183" height="51" rx="9" fill="{bg}" stroke="{border}"/>
  <circle cx="27" cy="26" r="15" fill="none" stroke="{border}" stroke-width="4"/>
  <circle cx="27" cy="26" r="15" fill="none" stroke="{color}" stroke-width="4" stroke-linecap="round"
          stroke-dasharray="{round(94.2 * score / 100, 1)} 94.2" transform="rotate(-90 27 26)"/>
  <text x="27" y="30" font-family="system-ui,Segoe UI,Arial" font-size="12" font-weight="700" fill="{fg}" text-anchor="middle">{g}</text>
  <text x="52" y="21" font-family="system-ui,Segoe UI,Arial" font-size="12" font-weight="700" fill="{fg}">Secured by WARDEN</text>
  <text x="52" y="37" font-family="system-ui,Segoe UI,Arial" font-size="11" fill="{sub}">Agent Trust Score {score}/100</text>
</svg>'''
