# WARDEN — Demo Guide

A 5-minute, offline, deterministic demo. Meet **Acme Corp**, which runs four AI agents
(support, research, finance, ops) behind WARDEN. You'll watch normal work sail through,
and attacks get stopped.

## Fastest path

```bash
pip install -e .          # or: pip install fastapi uvicorn pyyaml
warden serve              # → http://localhost:8000
```

Open the dashboard, click **Run all scenarios** (top-right of Overview), then explore.
Or stay in the terminal:

```bash
warden demo               # run all 11 scenarios with pass/fail
warden redteam            # launch the 32-attack red-team campaign
warden redteam --ablation # show which defence layer catches what
warden trust              # print the Trust Score
warden blast support-bot  # what could this agent reach if compromised?
warden compile "Require two approvals for refunds over $500"
```

## The two-minute live demo (what to click)

1. **Overview** → *Run all scenarios*. Watch the counters, the per-minute chart, and
   "what's doing the blocking" fill in. Note the gateway p99 latency (sub-millisecond).
2. **Live Feed** → every tool call, colour-coded by verdict. Filter to **deny** to see
   only the blocked ones, each with a plain-English reason.
3. **Incidents** → the exfiltration and laundering scenarios auto-opened incidents.
4. **Red Team** → *Run campaign*. 32 attacks, **32 blocked**, with a per-category
   breakdown and which defence layer fired. Then *Ablation study* to show that a
   policy-only guardrail would stop barely half of them.
5. **Trust Score** → a signed 84/100 "B+", five pillars, and an embeddable badge.
6. **Blast Radius** → pick `ops-bot`: fully contained. This is least privilege, visualised.

## The scenarios (and what each proves)

| Scenario | Story | What WARDEN does |
|---|---|---|
| **A normal day** | support-bot refunds a damaged lamp | Everything executes; the agent only ever sees a PII *handle*, never the real email |
| **Poisoned web page** | hidden white text tells the agent to email the customer list to an attacker | Page flagged as injected; the exfil email is blocked by information-flow control |
| **Canary exfiltration** | ops-bot is tricked into uploading `.env` (which holds a honeytoken) | Instant quarantine; follow-up calls get honeypot responses |
| **Runaway loop** | finance-bot retries the same call forever | Circuit breaker trips after 5 |
| **Agent laundering** | support-bot passes customer data to research-bot, who emails it out | Blocked as collusion, with the A→B→sink path |
| **$2,400 refund** | needs two humans | Parked; self-approval rejected; executes after two distinct approvals |
| **Privilege escalation** | ungranted tool, then a forged token | Both denied by the capability layer |
| **Intent drift** | research-bot starts emailing the CEO mid-research | Held for a human — out of declared scope |
| **Obfuscated injection** | a base64 attack in the support inbox | Decoded and blocked |
| **Kill switch** | revoke a contractor agent | Agent and its sub-agent cut off instantly |
| **Ledger tampering** | an insider edits the audit log | Verification pinpoints the exact edited record |

## Connecting your own agent

```python
from warden.sdk import WardenClient

agent = WardenClient.remote("http://localhost:8000", "support-bot", token)
agent = agent.session("Resolve support ticket #4821")

# every call is now governed, logged, and (if needed) blocked or held
result = agent.call("db_query", order_id="ord_4821")
agent.call("send_email", to=result["customer"]["email"], subject="Refund", body="On its way!")
```

Or point any **MCP** client at `http://localhost:8000/mcp`, or POST to `/v1/call`.

## Screenshots
See `tests/screenshots/` — captured automatically by the browser E2E test
(`pytest -m e2e`).
