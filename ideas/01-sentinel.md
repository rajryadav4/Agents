# SENTINEL — Governed AI Incident-Response Swarm

**Status:** Saved idea (parked; candidate to build later or as a product on top of WARDEN)

## Problem
- Production outages are expensive; on-call engineers burn out.
- Most incident time is spent on *diagnosis* (logs, metrics, traces, recent deploys).
- Companies want AI help but don't trust autonomous agents touching production.

## Solution
A swarm of specialized agents that investigates incidents, finds root cause, and
proposes/applies fixes under hard safety rules.

```
 Alert (Prometheus/PagerDuty)
          ▼
   Commander  — opens incident, plans investigation
          ▼
   GOVERNOR (policy engine): read-only default, scoped tokens,
   blast-radius limits, prod writes need human approval, audit log
          ▼
 Log Agent · Metrics Agent · Deploy Agent · Trace Agent  (sandboxed, read-only)
          ▼
   Hypothesis Board — agents post evidence, vote on root cause
          ▼
   Remediation Agent — tests fix in a SHADOW cluster first
          ▼
   Human approves in Slack → fix applied → postmortem auto-written
```

## Key features
1. Least privilege: read-only by default; short-lived capability tokens for writes.
2. Blast-radius policies as code (OPA/Rego): e.g. never scale below 2 replicas, never touch DB namespace.
3. Shadow verification of every fix on a cloned staging environment.
4. Sandboxed agents; cluster access only via Governor-controlled proxy.
5. Evidence-based coordination via a structured hypothesis board.
6. Hash-chained, tamper-evident audit log.

## ML / research angle
- Benchmark: microservices demo app (Online Boutique / DeathStarBench) + Chaos Mesh, 50+ fault types.
- Metrics: root-cause accuracy, time-to-diagnosis, unsafe actions attempted vs blocked, false-fix rate.
- GNN over the service dependency graph for root-cause ranking; compare LLM-only vs ML-only vs hybrid.
- Red-team: prompt injection hidden in log lines.

## Stack
Python, LangGraph / Claude Agent SDK, MCP servers (kubectl, Prometheus, Loki), k3d,
Chaos Mesh, OPA, Redis Streams, PyTorch Geometric, Grafana, React.

## Roadmap (~8 weeks)
1. Weeks 1–2: k3d cluster, demo app, observability stack, Commander + 2 read-only agents.
2. Weeks 3–4: Governor, capability tokens, sandboxing, Slack approvals, audit log.
3. Weeks 5–6: chaos fault library, benchmark harness, remediation with shadow verification.
4. Weeks 7–8: GNN root-cause model, log-injection red-teaming, dashboard, write-up, demo video.
