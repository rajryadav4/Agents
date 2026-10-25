# WARDEN — Architecture

## One sentence
WARDEN sits between your AI agents and their tools; every tool call passes through a decision pipeline that enforces identity, policy, information-flow and behavioural controls, and the whole thing is recorded in a tamper-evident ledger.

## The core idea: distrust the agent
An LLM agent can be hijacked by text it reads, can loop, can be over-privileged, and can collude with other agents. So WARDEN never relies on the agent "behaving". It enforces rules at the only place an agent can affect the world — **the tool call** — exactly as a network firewall enforces rules at the packet.

## Request flow

```
  your agents (any framework)
        │   Python SDK  ·  REST  ·  MCP
        ▼
┌──────────────────────── WARDEN GATEWAY (warden/gateway.py) ───────────────────────┐
│                                                                                    │
│  identity ─► kill switch ─► capability token ─► circuit breaker ─► canary scan ─►  │
│  information-flow (taint) ─► outbound injection ─► raw-PII egress ─► policy ─►      │
│  intent binding ─► behavioural anomaly ─► risk budget                              │
│                                                                                    │
│                       each stage can only make the verdict stricter                │
│                                                                                    │
│   ALLOW ─► run the real tool, label + sanitise its output (taint, PII vault)       │
│   WARN  ─► run, but flag for humans                                                 │
│   APPROVE ─► park in the quorum approval queue                                      │
│   DENY  ─► refuse, with an explanation and a fix                                    │
│   QUARANTINE ─► isolate the agent, route it to honeypot tools, open an incident     │
│                                                                                    │
└──────────────────────────────────────┬─────────────────────────────────────────┘
                                        │
         hash-chained ledger ◄──────────┤──────────► live event bus ──► dashboard (SSE)
         (Merkle proofs, checkpoints)   │
                                        ▼
                                   real tools / APIs / MCP servers
```

## Modules

| Area | Module | Responsibility |
|---|---|---|
| **Core** | `core/models.py` | Verdicts, findings, tool calls, decisions, result envelopes |
| | `core/tools.py` | Tool registry + metadata (category, trust, confidentiality, sink, risk) |
| | `core/bus.py` | Thread-safe pub/sub for the live dashboard |
| **Identity** | `identity/tokens.py` | Attenuable capability tokens (macaroons) |
| | `identity/agents.py` | Agent registry, delegation tree, status |
| **Policy** | `policy/engine.py` | Rules, verdict aggregation, modes |
| | `policy/conditions.py` | The `when` condition language + validator |
| | `policy/lint.py` | Static analysis of a policy |
| | `policy/nl.py` | Plain-English → rule compiler |
| | `policy/backtest.py` | Replay history under a candidate policy |
| | `policy/recommend.py` | Suggest rules + coverage report |
| **Detectors** | `detect/normalize.py` | De-obfuscation (base64, homoglyph, zero-width, hidden HTML…) |
| | `detect/classifier.py` | ML injection classifier (hashed n-grams + logistic regression) |
| | `detect/injection.py` | Injection detection = heuristics + ML over every view |
| | `detect/dataset.py` | Synthetic training/eval data generator |
| | `detect/pii.py` | PII detection + reversible vault |
| | `detect/canary.py` | Honeytokens |
| | `detect/taint.py` | Cross-agent information-flow tracking |
| | `detect/behavior.py` | Anomaly model, intent binding, risk budget, circuit breaker |
| **Governance** | `approvals.py` | Approval quorum + incident log |
| | `audit/ledger.py` | Hash-chained, Merkle-proofed audit ledger |
| **Assurance** | `redteam/attacks.py` | Red-team swarm (32 attacks) |
| | `redteam/ablation.py` | Layer ablation study |
| | `trust.py` | Trust Score, attestation, badge |
| | `simulate.py` | Blast-radius simulator + compliance pack |
| **Surface** | `gateway.py` | The decision pipeline (the heart) |
| | `sdk.py` | Python client (local + remote) |
| | `server/app.py` | FastAPI: REST + SSE + MCP + dashboard |
| | `cli.py` | `warden` command line |
| | `demo/` | The Acme Corp demo world, policy and scenarios |

## Key design choices
- **Fail-closed & monotone.** The pipeline starts at ALLOW and every stage can only escalate. A detector that errors doesn't silently pass a call.
- **The agent never sees the real secret.** PII is vaulted and only released to the correct destination; secrets are tracked and blocked from egress.
- **Deception over refusal for caught agents.** A quarantined agent gets believable fake responses, so an attacker doesn't learn what tripped the alarm.
- **Everything is explainable.** Every decision carries a human-readable reason and a suggested fix; every ML verdict can name its top n-grams.
- **Zero heavy dependencies.** Pure-Python ML, stdlib SQLite ledger, no build step for the UI — `git clone` and run.

## Deploying for real (beyond the demo)
The demo tools are in-memory simulations. In production you would:
1. Register your real tools (or MCP servers) with their metadata in a `ToolRegistry`.
2. Issue scoped capability tokens to each agent from your identity provider.
3. Point agents at the gateway via the SDK, REST, or MCP.
4. Persist the ledger to a durable SQLite/Postgres file and publish signed checkpoints somewhere outside WARDEN's control.
5. Wire approvals and incidents to Slack/PagerDuty.
