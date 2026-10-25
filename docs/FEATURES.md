# WARDEN — 20 Standout Features

Most "AI guardrail" projects are a prompt and a regex. WARDEN is a control plane.
Here are the twenty features that make it stand out, each with where it lives and why it matters.

### Identity & least privilege
1. **Attenuable capability tokens** (`identity/tokens.py`) — macaroon-style tokens bound by a chained HMAC. Any holder can *narrow* a token and hand it to a sub-agent, but nobody can *widen* one without the root key. Delegation can only ever reduce privilege — proven by a property-based test. Supports per-tool, per-argument, expiry, call-count and spend caveats.
2. **Cascading kill switch** (`gateway.py::kill_agent`) — revoke an agent and every credential in its delegation subtree instantly, everywhere.
3. **Blast-radius simulator** (`simulate.py`) — "if this agent were fully compromised right now, what could it actually reach?" Computed statically from its tokens + policy, executing nothing.

### Stopping prompt injection & data theft
4. **Obfuscation-aware injection detection** (`detect/normalize.py`, `detect/injection.py`) — defeats base64, hex, URL-encoding, ROT13, homoglyphs, zero-width characters, letter-spacing and hidden HTML (comments, white/zero-size text, alt attributes) by scanning every normalised *view* of the input.
5. **Trained ML injection classifier** (`detect/classifier.py`) — a from-scratch, dependency-free logistic-regression model over hashed n-grams. Trains in seconds, runs in <0.4 ms, and **explains** each verdict by its top contributing n-grams. F1 0.9997 on held-out data.
6. **Cross-agent information-flow control / taint tracking** (`detect/taint.py`) — labels every tool output (confidential / secret / untrusted) and traces distinctive fragments. Catches **data laundering**: agent A reads customer data, passes it to agent B, B emails it out — and reports the full A→B→sink path.
7. **Reversible PII vault** (`detect/pii.py`) — agents only ever see opaque handles like `⟦pii:email:7f3a2c⟧`. Real values are released *only* when a call goes to the matching data subject. A hijacked agent leaks meaningless tokens.
8. **Canary honeytokens** (`detect/canary.py`) — plants fake credentials where only a misbehaving agent would find them. Zero false positives: if a canary appears in an outbound call, data is being exfiltrated, full stop → instant quarantine.
9. **Deception / honeypot routing** (`gateway.py::_honeypot`) — a quarantined agent is silently re-routed to fake tools that return plausible but worthless data. The attacker thinks they succeeded while you watch and contain.
10. **Spotlighting of untrusted content** — tool output from untrusted sources is wrapped in explicit `⟪UNTRUSTED CONTENT⟫` markers so downstream reasoning treats it as data, not instructions.

### Behaviour & runaway control
11. **Per-agent behavioural model** (`detect/behavior.py`) — a Markov model of each agent's normal tool-to-tool transitions; flags statistically surprising actions (e.g. a support bot suddenly reading secrets).
12. **Intent binding** — a session declares its purpose ("resolve ticket #4821"); actions that don't fit that purpose are held for a human, even if no rule forbids them outright.
13. **Risk budget** — every action spends risk points scaled by amount/destination; bursts of risky activity need human sign-off.
14. **Circuit breaker** — detects loops (identical calls), call floods and spend explosions, and trips with a cooldown.

### Governance & humans-in-the-loop
15. **Two-person approval quorum** (`approvals.py`) — high-value actions need N *distinct* humans; an agent can never approve its own action and nobody can vote twice.
16. **Natural-language → policy compiler** (`policy/nl.py`) — type "Require two approvals for refunds over $500" and get a validated, linted rule. Deterministic, explainable, no LLM required.
17. **Policy back-testing / time machine** (`policy/backtest.py`) — before shipping a rule change, replay your entire recorded history under the candidate policy and see exactly which past calls it would newly block or release.
18. **Progressive enforcement** — every rule can roll out in `observe` → `warn` → `enforce` mode, so you can measure impact before turning it on.

### Assurance & trust
19. **Tamper-evident flight recorder** (`audit/ledger.py`) — an append-only, hash-chained ledger with Merkle inclusion proofs and signed checkpoints. Editing any past record is detected and localised to the exact record.
20. **Trust Score + signed attestation + embeddable badge** (`trust.py`) — one explainable 0–100 score across five pillars, HMAC-signed, with an inline-SVG "Secured by WARDEN" badge (light/dark) you can embed — a verifiable safety signal for AI agents.

### Plus (because 20 wasn't enough)
- **Red-team swarm** (`redteam/attacks.py`) — 32 adversarial scenarios across 9 categories, each run in an isolated sandbox.
- **Ablation study** (`redteam/ablation.py`) — quantifies exactly how much each defence layer contributes.
- **Policy linter** (`policy/lint.py`) — catches conflicts, duplicates, dead rules and sensitive-tool coverage gaps.
- **Policy recommender** (`policy/recommend.py`) — suggests new guardrails from your incident history.
- **Compliance pack** (`simulate.py::compliance_pack`) — turns the live audit trail into NIST-mapped control evidence.
- **Three ways to connect** — a 2-line Python SDK, a REST API, and a native **MCP** endpoint, so any agent framework fits behind WARDEN.
- **Live dashboard** — a hand-built, dependency-free UI with a real-time SSE feed, dark mode, and one page per capability.
