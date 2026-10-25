# WARDEN — Test Report

Generated from the suite in `tests/`. Reproduce any number below with the command shown.

## Headline

| | Result |
|---|---|
| **Python tests** | **1397 passed**, 0 failed |
| **Browser E2E test** | 1 passed (11 dashboard pages, 0 console errors) |
| **Code coverage** | **87%** of `warden/` (statements) |
| **Red-team attacks blocked** | **32 / 32 (100%)**, severity-weighted 100% |
| **Injection classifier** | precision 1.00 · recall 0.9995 · F1 0.9997 on 4,000 held-out samples |
| **Gateway latency** | p50 **0.11 ms**, p99 **0.19 ms** (~8,900 decisions/sec/core) |
| **Injection scan latency** | 0.39 ms/call (~2,540/sec/core) |

```bash
pytest -q                       # full suite
pytest -q -m "not e2e"          # skip the browser test
pytest --cov=warden             # with coverage
```

## Test breakdown

| Suite | File | What it covers |
|---|---|---|
| Capability tokens | `test_tokens.py` | mint/verify, forgery, caveat stripping, expiry, quotas, arg constraints, revocation + **property-based** attenuation-monotonicity (a child token can never widen privileges) |
| Audit ledger | `test_ledger.py` | append, hash-chain verification, tamper localisation, Merkle proofs, signed checkpoints + **property-based** "any single-record edit is detected" |
| Condition language | `test_conditions.py` | every operator (eq/glob/regex/gt/…), nesting (any/all/not), time windows, list semantics, static validation |
| Policy engine | `test_policy.py` | YAML round-trip, verdict aggregation, modes, `unless`, linter (conflicts/dead rules/coverage), NL compiler, back-test |
| Injection detector | `test_injection.py` | curated attacks, hard negatives, obfuscation (base64/homoglyph/zero-width/leet/hidden-HTML), corpus recall ≥ 0.9 & FP ≤ 0.05 |
| Regression corpus | `test_corpus.py` | **720 frozen per-sample decisions** that guard against detector regressions |
| PII & taint | `test_pii_taint.py` | Luhn/IBAN validation, vault tokenise/detokenise/gating, cross-agent taint paths, allowlist + property-based round-trips |
| Behaviour controls | `test_behavior.py` | anomaly warm-up/novelty, intent binding matrix, risk budget, circuit breaker (loop/rate/spend) |
| Gateway (integration) | `test_gateway.py` | the full decision pipeline on the Acme world, incl. a least-privilege matrix |
| Scenarios & red team | `test_scenarios_redteam.py` | all 11 demo scenarios + all 32 attacks individually + ablation + trust + compliance + blast radius |
| HTTP / MCP API | `test_api.py` | every REST route, SSE stream, and the MCP JSON-RPC endpoint |
| Python SDK | `test_sdk.py` | local + remote transports, sessions, `guard()`, block/approval exceptions |
| Browser E2E | `test_e2e.py` | Playwright drives the real dashboard in Chromium, screenshots every page, asserts zero console errors |

## Defence-in-depth: ablation study

Each row disables one layer and re-runs all 32 attacks (`pytest -k ablation`, or `warden redteam --ablation`).
Severity-weighted block rate (critical attacks count 5×):

| Configuration | Blocked | Weighted | Attacks that get through |
|---|---|---|---|
| **Full WARDEN** | **32/32** | **100%** | — |
| no canaries | 31/32 | 95.7% | quarantine-escape |
| no behaviour controls | 28/32 | 92.2% | refund-split, loop/rate flood, intent hijack |
| no information-flow control | 32/32 | 100% | *(other layers cover it)* |
| no injection detector | 26/32 | 85.2% | markdown/base64/homoglyph/zero-width/leet relays, agent worm |
| no capability tokens | 27/32 | 86.1% | ungranted/forged/stolen/expired token, sub-agent escalation |
| no policy rules | 26/32 | 79.1% | refund cap, destructive shell, RCE, self-approval, double-vote |
| **policy only** (a typical LLM guardrail) | 14/32 | **52.2%** | 18 attacks |
| **unprotected agents** | 1/32 | **4.3%** | 31 attacks |

**Takeaway:** a policy/guardrail-only approach (what most teams ship today) stops ~52% of these attacks by severity; WARDEN's layered design stops 100%. No single layer is load-bearing — removing any one still blocks the large majority, which is the definition of defence in depth.

## Notes on test integrity

- The red-team campaign runs each attack against a **fresh, isolated** sandbox world, so attacks can't interfere with each other or with the demo.
- The regression corpus in `test_corpus.py` is filtered to currently-correct decisions at collection time; its job is to catch *regressions*. The *absolute* error rate is measured separately on unfiltered data in `test_injection.py`.
- Property-based tests (Hypothesis) explore thousands of generated inputs for the token and ledger invariants.
