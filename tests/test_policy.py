"""Policy engine, YAML round-trip, linter, NL compiler, back-test."""
import pytest

from warden.core.models import Mode, ToolCall, Verdict, aggregate_verdict
from warden.policy import nl
from warden.policy.engine import Policy, Rule
from warden.policy.lint import lint, summary

YAML = """
name: test
version: 1
rules:
  - id: no-external-email
    description: block external email
    when: {tool: send_email, args: {to: {not_glob: "*@acme.com"}}}
    action: deny
    severity: high
  - id: big-refund
    when: {tool: issue_refund, args: {amount: {gt: 500}}}
    action: require_approval
    approvals: 2
"""


def test_load_yaml():
    p = Policy.from_yaml(YAML)
    assert len(p.rules) == 2
    assert p.rule("big-refund").approvals == 2


def test_yaml_roundtrip():
    p = Policy.from_yaml(YAML)
    p2 = Policy.from_yaml(p.to_yaml())
    assert p.fingerprint() == p2.fingerprint()


def test_evaluate_deny():
    p = Policy.from_yaml(YAML)
    f = p.evaluate(ToolCall("a", "send_email", {"to": "x@evil.io"}))
    assert aggregate_verdict(f) == Verdict.DENY


def test_evaluate_allow_internal():
    p = Policy.from_yaml(YAML)
    f = p.evaluate(ToolCall("a", "send_email", {"to": "x@acme.com"}))
    assert aggregate_verdict(f) == Verdict.ALLOW


def test_evaluate_approval():
    p = Policy.from_yaml(YAML)
    f = p.evaluate(ToolCall("a", "issue_refund", {"amount": 600}))
    assert aggregate_verdict(f) == Verdict.REQUIRE_APPROVAL


def test_observe_mode_allows():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"tool": "x"}, "action": "deny", "mode": "observe"})])
    f = p.evaluate(ToolCall("a", "x", {}))
    assert aggregate_verdict(f) == Verdict.ALLOW


def test_warn_mode_caps():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"tool": "x"}, "action": "deny", "mode": "warn"})])
    f = p.evaluate(ToolCall("a", "x", {}))
    assert aggregate_verdict(f) == Verdict.WARN


def test_unless():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"tool": "send_email"},
                                      "unless": {"args": {"to": {"glob": "*@acme.com"}}}, "action": "deny"})])
    assert aggregate_verdict(p.evaluate(ToolCall("a", "send_email", {"to": "x@evil.io"}))) == Verdict.DENY
    assert aggregate_verdict(p.evaluate(ToolCall("a", "send_email", {"to": "x@acme.com"}))) == Verdict.ALLOW


def test_upsert_remove():
    p = Policy()
    p.upsert(Rule.from_dict({"id": "r1", "when": {"tool": "x"}, "action": "deny"}))
    assert p.rule("r1")
    assert p.remove("r1")
    assert not p.rule("r1")


def test_set_mode():
    p = Policy.from_yaml(YAML)
    p.set_mode("big-refund", "observe")
    assert p.rule("big-refund").mode == "observe"


def test_disabled_rule_skipped():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"tool": "x"}, "action": "deny", "enabled": False})])
    assert aggregate_verdict(p.evaluate(ToolCall("a", "x", {}))) == Verdict.ALLOW


# ---- linter ----
def test_lint_clean():
    issues = lint(Policy.from_yaml(YAML))
    assert summary(issues)["error"] == 0


def test_lint_duplicate_id():
    p = Policy(rules=[Rule.from_dict({"id": "dup", "when": {"tool": "x"}, "action": "deny"}),
                      Rule.from_dict({"id": "dup", "when": {"tool": "y"}, "action": "deny"})])
    assert any(i["code"] == "duplicate-id" for i in lint(p))


def test_lint_bad_action():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"tool": "x"}, "action": "nuke"})])
    assert any(i["code"] == "bad-action" for i in lint(p))


def test_lint_empty_when():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {}, "action": "deny"})])
    assert any(i["code"] == "matches-everything" for i in lint(p))


def test_lint_conflict():
    p = Policy(rules=[Rule.from_dict({"id": "a", "when": {"tool": "x"}, "action": "deny"}),
                      Rule.from_dict({"id": "b", "when": {"tool": "x"}, "action": "warn"})])
    assert any(i["code"] == "conflict" for i in lint(p))


def test_lint_unknown_tool():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"tool": "nonexistent"}, "action": "deny"})])
    assert any(i["code"] == "unknown-tool" for i in lint(p, known_tools=["send_email"]))


def test_lint_bad_regex():
    p = Policy(rules=[Rule.from_dict({"id": "r", "when": {"args": {"cmd": {"regex": "([bad"}}}, "action": "deny"})])
    assert any(i["code"] == "bad-condition" for i in lint(p))


# ---- NL compiler ----
NL_CASES = [
    ("Never send email outside acme.com", "send_email", "deny"),
    ("Require two approvals for refunds over $500", "issue_refund", "require_approval"),
    ("Block shell commands containing 'rm -rf'", "shell_exec", "deny"),
    ("Quarantine any agent that posts to pastebin.com", None, "quarantine"),
    ("Warn when someone deletes files", "delete_file", "warn"),
    ("Only the research agent may fetch web pages", "web_fetch", "deny"),
]


@pytest.mark.parametrize("text,tool,action", NL_CASES)
def test_nl_compile(text, tool, action):
    c = nl.compile_rule(text, known_tools=["send_email", "issue_refund", "shell_exec", "http_post", "delete_file", "web_fetch"], org_domain="acme.com")
    assert c.rule is not None, text
    assert c.rule["action"] == action, (text, c.rule)
    if tool:
        toolspec = c.rule["when"].get("tool")
        assert tool == toolspec or (isinstance(toolspec, list) and tool in toolspec), (text, toolspec)


def test_nl_two_approvals():
    c = nl.compile_rule("Require two approvals for refunds over $500", org_domain="acme.com")
    assert c.rule["approvals"] == 2


def test_nl_generates_valid_rule():
    for text, _, _ in NL_CASES:
        c = nl.compile_rule(text, org_domain="acme.com")
        if c.rule:
            issues = lint(Policy(rules=[Rule.from_dict(c.rule)]))
            assert summary(issues)["error"] == 0, (text, issues)


def test_nl_refuses_empty():
    c = nl.compile_rule("hello how are you today", org_domain="acme.com")
    # vague text should produce low confidence or no rule
    assert c.rule is None or c.confidence < 0.6


# ---- backtest ----
def test_backtest(world):
    from warden.core.models import ToolCall as TC
    from warden.policy.backtest import backtest

    warden = world.warden
    for _ in range(5):
        warden.call(TC("support-bot", "kb_search", {"query": "x"}, token=world.tokens["support-bot"]))
    stricter = warden.policy.copy()
    stricter.upsert(Rule.from_dict({"id": "block-kb", "when": {"tool": "kb_search"}, "action": "deny"}))
    rep = backtest(warden.ledger.records(kind="call"), warden.policy, stricter,
                   {a.id: a.role for a in warden.agents.all()})
    assert rep["newly_blocked_or_flagged"] >= 1
    assert rep["calls_replayed"] >= 5
