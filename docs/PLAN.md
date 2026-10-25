# WARDEN — Project Plan

This document records how WARDEN was planned and built, phase by phase.

## 1. Initialization

| Decision | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Lingua franca of AI agents; every framework has a Python SDK |
| API server | FastAPI + Uvicorn | Typed, async, auto OpenAPI docs at `/docs` |
| Storage | SQLite (stdlib) | Zero-setup; the audit ledger is a single file you can copy and verify |
| ML | Pure-Python logistic regression over hashed n-grams | No GPU, no heavy deps, trains in seconds, fully explainable |
| UI | Hand-written HTML/CSS/vanilla JS, no build step | `git clone` → run. Nothing to compile, nothing to break |
| Agent protocol | Python SDK, REST, and an MCP-compatible JSON-RPC endpoint | Works with any agent framework |
| Tests | pytest + hypothesis + Playwright (E2E) | Unit, property-based, integration, adversarial and browser tests |

## 2. Planning — the threat model

WARDEN assumes **the agent is not trustworthy**. An LLM agent can be hijacked by text it reads
(prompt injection), can loop, can be over-privileged, and can collude with other agents. So
WARDEN never relies on the agent "behaving"; it sits between agents and their tools and
enforces rules at the only place an agent can actually affect the world: the tool call.

Threats covered:

1. Prompt injection from untrusted content (web pages, emails, tickets, tool output)
2. Data exfiltration (customer PII, secrets) to external destinations
3. Privilege escalation (calling tools outside an agent's grant, forging/stripping tokens)
4. Runaway behaviour (loops, spend explosions)
5. Multi-agent laundering (agent A passes secrets to agent B, who leaks them)
6. Insider / audit tampering (editing logs after the fact)
7. Goal hijack (agent drifting away from its declared task)

## 3. Architecture

```
 agents (any framework) ──SDK / REST / MCP──►  WARDEN GATEWAY  ──►  real tools
                                               │
   identity → kill-switch → capability token → circuit breaker → canaries →
   information-flow (taint) → injection ML → PII vault → policy engine →
   intent binding → behavioural anomaly → risk budget → verdict
                                               │
   allow ─ execute & label output   approval ─ quorum queue   deny ─ explain
   quarantine ─ route to honeypot tools (deception) + incident + postmortem
                                               │
                      hash-chained ledger (Merkle proofs) + live event bus → dashboard
```

## 4. Implementation phases

1. Core models, storage, event bus, hash-chained ledger
2. Identity: agents, attenuable capability tokens, cascading kill switch
3. Policy engine, YAML constitution, linter, natural-language compiler, backtester
4. Detectors: injection classifier (+ dataset generator), PII vault, canaries, taint tracking,
   anomaly, intent, risk budget, circuit breaker
5. Gateway pipeline, approvals, quarantine/honeypot, incidents, postmortems
6. Demo world (Acme Corp), scripted scenarios, red-team swarm, genetic fuzzer, self-hardening
7. Trust score + signed attestation + badge, blast-radius simulator, compliance pack
8. REST API, SSE live stream, MCP endpoint, Python SDK, CLI
9. Dashboard UI
10. Tests (1000+), E2E browser tests, benchmarks, documentation

## 5. Testing strategy

- **Unit**: every module, including parametrised operator matrices
- **Property-based** (hypothesis): token attenuation can never widen privileges; ledger tamper detection
- **Adversarial corpus**: hundreds of injection attacks that must be caught + benign texts that must pass
- **Integration**: full gateway pipeline and every demo scenario end to end
- **API**: every REST and MCP endpoint
- **E2E**: Playwright drives the real dashboard in Chromium and captures screenshots
- **Performance**: gateway latency benchmark (p50 / p99)
