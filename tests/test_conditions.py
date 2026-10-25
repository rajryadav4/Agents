"""Condition language: every operator, nesting, time windows, validation."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from warden.policy.conditions import OPERATORS, evaluate, match_op, match_spec, validate

OPS_TRUE = [
    ("eq", "send_email", "send_email"),
    ("eq", "SEND_EMAIL", "send_email"),
    ("ne", "a", "b"),
    ("in", "x", ["x", "y"]),
    ("not_in", "z", ["x", "y"]),
    ("glob", "send_email", "send_*"),
    ("not_glob", "x@evil.io", "*@acme.com"),
    ("regex", "rm -rf /", r"rm\s+-rf"),
    ("not_regex", "safe", r"danger"),
    ("gt", 600, 500),
    ("gte", 500, 500),
    ("lt", 100, 500),
    ("lte", 500, 500),
    ("contains", "hello world", "world"),
    ("not_contains", "hello", "bye"),
    ("starts_with", "hello", "hel"),
    ("ends_with", "hello", "llo"),
    ("len_gt", "abcdef", 3),
    ("len_lt", "ab", 5),
]
OPS_FALSE = [
    ("eq", "a", "b"),
    ("ne", "a", "a"),
    ("in", "z", ["x", "y"]),
    ("glob", "read_file", "send_*"),
    ("not_glob", "x@acme.com", "*@acme.com"),
    ("gt", 100, 500),
    ("lt", 600, 500),
    ("contains", "abc", "xyz"),
    ("starts_with", "hello", "xyz"),
    ("len_gt", "ab", 5),
]


@pytest.mark.parametrize("op,val,exp", OPS_TRUE)
def test_operators_true(op, val, exp):
    assert match_op(op, val, exp) is True


@pytest.mark.parametrize("op,val,exp", OPS_FALSE)
def test_operators_false(op, val, exp):
    assert match_op(op, val, exp) is False


def test_numeric_coercion():
    assert match_op("gt", "600", "500")
    assert match_op("gt", "$1,200", 500)


def test_list_positive_any():
    assert match_op("glob", ["a@acme.com", "b@evil.io"], "*@evil.io")


def test_list_negative_any_violation():
    # one external recipient should trigger not_glob
    assert match_op("not_glob", ["a@acme.com", "b@evil.io"], "*@acme.com")
    assert not match_op("not_glob", ["a@acme.com", "b@acme.com"], "*@acme.com")


def test_exists():
    assert match_op("exists", "something", True)
    from warden.policy.conditions import MISSING

    assert match_op("exists", MISSING, False)


def test_tool_condition():
    assert evaluate({"tool": "send_email"}, {"tool": "send_email"})
    assert not evaluate({"tool": "send_email"}, {"tool": "db_query"})


def test_tool_list():
    assert evaluate({"tool": ["send_email", "http_post"]}, {"tool": "http_post"})


def test_args_nested():
    ctx = {"tool": "issue_refund", "args": {"amount": 600}}
    assert evaluate({"tool": "issue_refund", "args": {"amount": {"gt": 500}}}, ctx)
    assert not evaluate({"tool": "issue_refund", "args": {"amount": {"gt": 700}}}, ctx)


def test_any_all_not():
    ctx = {"tool": "send_email", "args": {"to": "x@evil.io"}}
    assert evaluate({"any": [{"tool": "db_query"}, {"tool": "send_email"}]}, ctx)
    assert evaluate({"all": [{"tool": "send_email"}, {"args": {"to": {"not_glob": "*@acme.com"}}}]}, ctx)
    assert evaluate({"not": {"tool": "db_query"}}, ctx)
    assert not evaluate({"not": {"tool": "send_email"}}, ctx)


def test_time_window_outside():
    import datetime

    # 3am UTC
    ts = datetime.datetime(2026, 1, 1, 3, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert evaluate({"time": {"outside": "09:00-18:00"}}, {"now": ts})
    ts2 = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
    assert not evaluate({"time": {"outside": "09:00-18:00"}}, {"now": ts2})


def test_match_spec_forms():
    assert match_spec("send_*", "send_email")
    assert match_spec(["a", "b*"], "bob")
    assert match_spec({"eq": "x"}, "x")


def test_validate_catches_bad_regex():
    problems = validate({"args": {"cmd": {"regex": "([unclosed"}}})
    assert any("regex" in p for p in problems)


def test_validate_unknown_operator():
    problems = validate({"tool": {"frobnicate": "x"}})
    assert any("unknown operator" in p for p in problems)


def test_validate_bad_time():
    problems = validate({"time": {"outside": "notatime"}})
    assert problems


def test_unknown_condition_key_raises():
    with pytest.raises(Exception):
        evaluate({"nonsense": 1}, {"tool": "x"})


@settings(max_examples=100, deadline=None)
@given(a=st.integers(-10000, 10000), b=st.integers(-10000, 10000))
def test_gt_lt_consistency(a, b):
    if a != b:
        assert match_op("gt", a, b) != match_op("lt", a, b) or a == b
    assert match_op("gte", a, a)
    assert match_op("lte", a, a)


@settings(max_examples=80, deadline=None)
@given(s=st.text(max_size=20), sub=st.text(min_size=1, max_size=5))
def test_contains_matches_substring(s, sub):
    combined = s + sub
    assert match_op("contains", combined, sub)


@pytest.mark.parametrize("op", sorted(OPERATORS))
def test_all_operators_handle_missing(op):
    from warden.policy.conditions import MISSING

    # no operator should raise on a missing value
    try:
        r = match_op(op, MISSING, "x" if op not in ("gt", "gte", "lt", "lte", "len_gt", "len_lt") else 1)
        assert isinstance(r, bool)
    except Exception as e:  # pragma: no cover
        pytest.fail(f"{op} raised {e}")
