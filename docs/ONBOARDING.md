# WARDEN — Onboarding

Welcome. This gets you from zero to "I understand the whole system" in about 20 minutes.

## 1. Install (2 min)

Requirements: Python 3.11+. (Node + Playwright only for the optional browser test.)

```bash
git clone <your-fork-url> warden && cd warden
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # runtime + test deps
```

Smoke-test it:

```bash
warden demo          # should print 11/11 scenarios passed
pytest -q -m "not e2e"   # should print ~1397 passed
```

## 2. See it run (3 min)

```bash
warden serve         # open http://localhost:8000, click "Run all scenarios"
```

Read `docs/DEMO.md` alongside and click through the pages.

## 3. Understand the model (5 min)

Read, in this order:
1. `docs/ARCHITECTURE.md` — the request flow diagram is the whole system on one page.
2. `warden/gateway.py` — `Warden.call()` and `_evaluate()` are the heart; every defence is one stage.
3. `warden/demo/acme_policy.yaml` — what a real policy looks like.

The one mental model to keep: **WARDEN distrusts the agent and enforces at the tool call.**

## 4. Make your first change (10 min)

**Add a policy rule** — edit `warden/demo/acme_policy.yaml`, or do it live:

```bash
warden compile "Never let support agents post to the #general channel"
# paste the generated rule into the YAML, then:
warden lint warden/demo/acme_policy.yaml
```

**Add a tool** — in `warden/demo/world.py`, register a `ToolSpec` with its metadata
(category, trust, confidentiality, sink, risk). The gateway governs it automatically.

**Add a red-team attack** — add a function to `warden/redteam/attacks.py` and an entry in
`ATTACKS`. Run `warden redteam` to confirm it's blocked (or find a gap to fix).

Re-run `pytest -q` after any change.

## 5. Where things live

```
warden/
  gateway.py        ← the decision pipeline (start here)
  identity/         ← agents + capability tokens
  policy/           ← rules, conditions, linter, NL compiler, backtest
  detect/           ← injection, PII, canaries, taint, behaviour, ML classifier
  audit/ledger.py   ← tamper-evident flight recorder
  redteam/          ← attack swarm + ablation
  trust.py          ← Trust Score + badge
  simulate.py       ← blast radius + compliance
  server/           ← FastAPI (REST + SSE + MCP) + the dashboard (static/)
  sdk.py            ← Python client
  cli.py            ← the `warden` command
  demo/             ← Acme Corp world, policy, scenarios
tests/              ← 1397 tests + a browser E2E test
docs/               ← you are here
```

## 6. Conventions

- **No heavy dependencies.** Pure-Python ML, stdlib SQLite, no UI build step. Keep it that way.
- **Every decision is explainable.** New detectors return a `Finding` with a message and a `suggestion`.
- **Fail closed.** A detector that can't decide should escalate, never silently allow.
- **Tests first for security logic.** If you add a control, add both a "blocks the bad thing" and a "lets the good thing through" test.

## 7. FAQ

**Is this production-ready?** The *architecture* is; the demo *tools* are simulations. See the last section of `docs/ARCHITECTURE.md` for the path to production.

**Why not use an LLM for detection?** We do, as an option — but the default detectors are deterministic, fast (<0.4 ms), explainable, and free, which is what you want inline on every tool call. An LLM judge can be layered on top for ambiguous cases.

**How do I reset the demo?** Restart the server, or `POST /v1/demo/reset`.
