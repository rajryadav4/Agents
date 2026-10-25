# WARDEN — Control Plane for AI Agents (SaaS)

**Pitch:** "Okta + Cloudflare for AI agents." Give every AI agent an identity,
permissions, a firewall, a kill switch, and a public Trust Score.

**Status:** Proposed flagship (open-core: OSS on GitHub + paid hosted cloud)

## Problem
- Companies are deploying agents wired to real tools (MCP servers, APIs, databases, email).
- Nobody knows which agents exist, what they can access, or what they actually did.
- Agents get hijacked by prompt injection hidden in web pages, emails, tickets, tool outputs.
- Costs run away (agent loops burning tokens).
- Security/compliance teams block agent rollouts because there is no audit trail.

## Product
1. **Agent Identity** — every agent gets an ID and short-lived, scoped credentials (no shared API keys).
2. **Runtime Firewall / Gateway** — a proxy between agents and tools (MCP-native). Every tool call is
   checked against policy, scanned for injection/exfiltration, PII-redacted, rate- and budget-limited.
3. **Policy as code** — YAML "constitution" compiled to OPA/Cedar. Human-approval gates for risky actions.
4. **Kill switch** — revoke any agent instantly, everywhere.
5. **Flight recorder** — hash-chained audit log, full trace replay of every agent run.
6. **Red-Team Swarm (the crazy part)** — your own adversarial agents attack the customer's agents
   every night with fresh injection/jailbreak/exfiltration scenarios.
7. **Trust Score + badge** — a public, verifiable score (like an SSL padlock / SOC 2 badge for agents)
   that companies embed to show their agent is safe. Doubles as viral marketing.

## Business model
| Tier | Price (draft) | For |
|---|---|---|
| Open source | Free | Self-hosted gateway + SDK; drives GitHub stars & adoption |
| Starter | ~$49/mo | Indie devs / startups: hosted gateway, N agents, basic red-team |
| Team | ~$299–499/mo | Dashboard, approvals in Slack, nightly red-team, Trust badge |
| Enterprise | Custom (annual) | SSO, on-prem, compliance reports (SOC 2 / EU AI Act evidence) |
Plus usage-based: per 1K governed tool calls.

## Moat / ML angle
- Fine-tuned small injection/exfiltration classifier (fast, cheap, runs inline in the gateway).
- Proprietary dataset that grows from red-team runs → model gets better with every customer (data flywheel).
- Benchmark: attack success rate with vs without WARDEN.

## Market notes
- Big security vendors have been buying AI-security startups — proof the market is real.
- Wedge: developer-first, open-source, MCP-native, cheap self-serve tier for small teams that
  enterprise vendors ignore.

## MVP (~8–10 weeks)
1. Gateway proxy for MCP tool calls + YAML policies + audit log.
2. Python/TS SDK (2-line integration) + LangGraph / Claude Agent SDK / OpenAI Agents adapters.
3. Injection classifier v1 + budget/rate limits + kill switch.
4. Red-team swarm v1 (100 scenarios) + Trust Score report.
5. Dashboard, Stripe billing, landing page, launch (GitHub, Hacker News, Product Hunt).
