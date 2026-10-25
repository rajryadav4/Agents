"""Ablation study: switch defence layers off one at a time and re-run the red team.

Answers the question every reviewer asks: *which layer actually matters?*
"""
from __future__ import annotations

from typing import Any

from warden.policy.engine import Policy
from warden.redteam.attacks import run_campaign

ALL_OFF = {
    "require_tokens": False, "pii_vault": False, "enable_breaker": False, "enable_canaries": False, "enable_ifc": False,
    "enable_injection": False, "enable_pii_guard": False, "enable_policy": False, "enable_intent": False,
    "enable_anomaly": False, "enable_risk": False,
}

CONFIGS: dict[str, dict[str, Any]] = {
    "full WARDEN": {},
    "no policy rules": {"enable_policy": False},
    "no information-flow control": {"enable_ifc": False},
    "no injection detector": {"enable_injection": False},
    "no canaries": {"enable_canaries": False},
    "no capability tokens": {"require_tokens": False},
    "no behaviour controls": {"enable_breaker": False, "enable_intent": False, "enable_anomaly": False, "enable_risk": False},
    "policy only (typical guardrail)": {k: v for k, v in ALL_OFF.items() if k != "enable_policy"},
    "unprotected agents": ALL_OFF,
}


def run_ablation(policy: Policy | None = None, configs: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    rows = []
    for name, settings in (configs or CONFIGS).items():
        rep = run_campaign(policy=policy, settings=settings)
        rows.append({
            "config": name, "settings": settings, "blocked": rep["blocked"], "attacks": rep["attacks"],
            "block_rate": rep["block_rate"], "weighted_block_rate": rep["weighted_block_rate"],
            "missed": [o["id"] for o in rep["outcomes"] if not o["blocked"]],
        })
    full = rows[0]["weighted_block_rate"] if rows else 0
    for r in rows:
        r["contribution"] = round(full - r["weighted_block_rate"], 4)
    return {"rows": rows}
