"""Behavioural controls: anomaly model, intent binding, risk budget, circuit breaker."""
import pytest

from warden.detect.behavior import (BehaviorModel, CircuitBreaker, RiskBudget, as_list, classify_intent,
                                    intent_check)


def test_behavior_warmup():
    m = BehaviorModel(warmup=5)
    for _ in range(3):
        m.observe("a", "x")
    assert not m.score("a", "x")["warm"]


def test_behavior_novel_tool_flagged():
    m = BehaviorModel(warmup=3)
    for _ in range(6):
        m.observe("a", "db_query")
        m.observe("a", "send_email")
    s = m.score("a", "shell_exec")
    assert s["novel_tool"] and s["score"] >= 0.8


def test_behavior_normal_not_flagged():
    m = BehaviorModel(warmup=3)
    for _ in range(10):
        m.observe("a", "db_query")
        m.observe("a", "send_email")
    s = m.score("a", "send_email")
    assert s["score"] < 0.9


@pytest.mark.parametrize("intent,tool,ok", [
    ("Resolve support ticket", "send_email", True),
    ("Summarize AI news", "web_fetch", True),
    ("Summarize AI news", "issue_refund", False),
    ("Reply to shipping question", "shell_exec", False),
    ("Reconcile the ledger", "issue_refund", True),
    ("Deploy the new server", "shell_exec", True),
])
def test_intent_check(intent, tool, ok):
    result = intent_check(intent, tool)
    assert (result is None) == ok, (intent, tool, result)


def test_classify_intent():
    assert "support" in classify_intent("resolve the customer ticket")
    assert "ops" in classify_intent("restart the server and check logs")


def test_intent_unknown_tool_passes():
    assert intent_check("anything", "some_unknown_tool") is None


# ---- risk budget ----
def test_risk_budget_accumulates():
    b = RiskBudget(limit=50)
    b.charge("s1", 30)
    assert not b.would_exceed("s1", 20)
    b.charge("s1", 20)
    assert b.would_exceed("s1", 1)


def test_risk_budget_per_key():
    b = RiskBudget(limit=50)
    b.charge("s1", 50)
    assert not b.would_exceed("s2", 40)


def test_risk_cost_scales_with_amount():
    low = RiskBudget.cost(5, {"amount": 10}, False)
    high = RiskBudget.cost(5, {"amount": 100000}, False)
    assert high > low


def test_risk_cost_external_premium():
    assert RiskBudget.cost(5, {}, True) > RiskBudget.cost(5, {}, False)


def test_risk_reset():
    b = RiskBudget(limit=10)
    b.charge("s", 10)
    b.reset("s")
    assert not b.would_exceed("s", 5)


# ---- circuit breaker ----
def test_breaker_identical_loop():
    cb = CircuitBreaker(max_identical=3)
    for _ in range(3):
        assert cb.check("a", "x", {"q": 1}) is None
        cb.record("a", "x", {"q": 1})
    assert cb.check("a", "x", {"q": 1}) is not None


def test_breaker_different_args_ok():
    cb = CircuitBreaker(max_identical=3)
    for i in range(10):
        assert cb.check("a", "x", {"q": i}) is None
        cb.record("a", "x", {"q": i})


def test_breaker_rate_limit():
    cb = CircuitBreaker(max_rate_per_min=5)
    tripped = False
    for i in range(10):
        if cb.check("a", "x", {"i": i}) is not None:
            tripped = True
            break
        cb.record("a", "x", {"i": i})
    assert tripped


def test_breaker_spend_limit():
    cb = CircuitBreaker(max_spend_usd_per_hour=1.0)
    assert cb.check("a", "x", {}, cost_usd=0.5) is None
    cb.record("a", "x", {}, cost_usd=0.5)
    assert cb.check("a", "x", {}, cost_usd=2.0) is not None


def test_breaker_cooldown_then_reset():
    cb = CircuitBreaker(max_identical=2)
    for _ in range(2):
        cb.record("a", "x", {})
    assert cb.check("a", "x", {}) is not None  # tripped
    assert cb.check("a", "y", {}) is not None  # still open for agent
    cb.reset("a")
    assert cb.check("a", "y", {}) is None


def test_breaker_state():
    cb = CircuitBreaker(max_identical=2)
    for _ in range(2):
        cb.record("a", "x", {})
    cb.check("a", "x", {})
    st = cb.state("a")
    assert st["open"]


@pytest.mark.parametrize("value,expected", [
    (None, []),
    ("a,b,c", ["a", "b", "c"]),
    (["x", "y"], ["x", "y"]),
    ("single", ["single"]),
    ("a; b c", ["a", "b", "c"]),
])
def test_as_list(value, expected):
    assert as_list(value) == expected
