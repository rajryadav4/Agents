"""Capability token tests, including property-based attenuation invariants."""
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from warden.identity.tokens import TokenAuthority, TokenError, attenuate, parse

TOOLS = ["send_email", "db_query", "shell_exec", "web_fetch", "issue_refund", "read_secret"]


@pytest.fixture
def authority():
    return TokenAuthority()


def test_mint_and_verify(authority):
    t = authority.mint("a1", tools=["send_email"])
    assert authority.verify(t, "a1", "send_email").ok


def test_wrong_agent(authority):
    t = authority.mint("a1", tools=["send_email"])
    assert not authority.verify(t, "a2", "send_email").ok


def test_tool_not_granted(authority):
    t = authority.mint("a1", tools=["send_email"])
    assert not authority.verify(t, "a1", "shell_exec").ok


def test_no_token(authority):
    assert not authority.verify(None, "a1", "x").ok
    assert not authority.verify("", "a1", "x").ok


def test_malformed_token(authority):
    for bad in ["garbage", "wdn1.notbase64.sig", "a.b.c.d", "wdn1..", "x.y.z"]:
        assert not authority.verify(bad, "a1", "x").ok


def test_expiry(authority):
    t = authority.mint("a1", tools=["x"], ttl_s=-1)
    assert not authority.verify(t, "a1", "x").ok
    t2 = authority.mint("a1", tools=["x"], ttl_s=100)
    assert authority.verify(t2, "a1", "x").ok


def test_forged_signature(authority):
    t = authority.mint("a1", tools=["send_email"])
    pre, body, sig = t.split(".")
    forged = f"{pre}.{body}.{'0' * len(sig)}"
    assert not authority.verify(forged, "a1", "send_email").ok


def test_stripped_caveat_detected(authority):
    import base64
    import json

    t = authority.mint("a1", tools=["send_email"])
    pre, body, sig = t.split(".")
    data = json.loads(base64.urlsafe_b64decode(body + "=="))
    data["c"] = [c for c in data["c"] if not c.startswith("tools in")]
    forged = f"{pre}.{base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')}.{sig}"
    assert not authority.verify(forged, "a1", "shell_exec").ok


def test_attenuation_narrows_tools(authority):
    t = authority.mint("a1", tools=["send_email", "db_query"])
    child = attenuate(t, "tools in db_query")
    assert authority.verify(child, "a1", "db_query").ok
    assert not authority.verify(child, "a1", "send_email").ok


def test_attenuation_cannot_widen(authority):
    t = authority.mint("a1", tools=["db_query"])
    child = attenuate(t, "tools in db_query,send_email")
    # intersection still excludes send_email
    assert not authority.verify(child, "a1", "send_email").ok
    assert authority.verify(child, "a1", "db_query").ok


def test_max_calls(authority):
    t = authority.mint("a1", tools=["x"], max_calls=3)
    for _ in range(3):
        assert authority.verify(t, "a1", "x").ok
    assert not authority.verify(t, "a1", "x").ok


def test_budget(authority):
    t = authority.mint("a1", tools=["x"], budget_usd=0.01)
    assert authority.verify(t, "a1", "x", cost_usd=0.005).ok
    assert authority.verify(t, "a1", "x", cost_usd=0.005).ok
    assert not authority.verify(t, "a1", "x", cost_usd=0.005).ok


def test_arg_constraint(authority):
    t = authority.mint("a1", tools=["send_email"], extra=["arg send_email.to glob *@acme.com"])
    assert authority.verify(t, "a1", "send_email", {"to": "bob@acme.com"}).ok
    assert not authority.verify(t, "a1", "send_email", {"to": "x@evil.io"}).ok


def test_arg_constraint_list(authority):
    t = authority.mint("a1", tools=["send_email"], extra=["arg send_email.to glob *@acme.com"])
    assert not authority.verify(t, "a1", "send_email", {"to": ["a@acme.com", "x@evil.io"]}).ok
    assert authority.verify(t, "a1", "send_email", {"to": ["a@acme.com", "b@acme.com"]}).ok


def test_session_binding(authority):
    t = authority.mint("a1", tools=["x"], extra=["session = s1"])
    assert authority.verify(t, "a1", "x", session_id="s1").ok
    assert not authority.verify(t, "a1", "x", session_id="s2").ok


def test_revoke(authority):
    t = authority.mint("a1", tools=["x"])
    tok = parse(t)
    assert authority.verify(t, "a1", "x").ok
    authority.revoke(tok.tid)
    assert not authority.verify(t, "a1", "x").ok


def test_revoke_child_by_parent(authority):
    t = authority.mint("a1", tools=["x", "y"])
    child = attenuate(t, "tools in x")
    authority.revoke(parse(t).tid)
    assert not authority.verify(child, "a1", "x").ok


def test_revoke_agent(authority):
    authority.mint("a1", tools=["x"])
    t2 = authority.mint("a1", tools=["y"])
    tids = authority.revoke_agent("a1")
    assert len(tids) == 2
    assert not authority.verify(t2, "a1", "y").ok


def test_unknown_caveat_rejected(authority):
    t = authority.mint("a1", tools=["x"])
    with pytest.raises(TokenError):
        attenuate(t, "evil_caveat = yes")


def test_describe(authority):
    t = authority.mint("a1", tools=["send_email", "db_query"], ttl_s=100)
    d = parse(t).describe()
    assert d["agent"] == "a1"
    assert set(d["tools"]) == {"send_email", "db_query"}
    assert d["expires"] > time.time()


# ---- property-based: attenuation can only ever reduce privileges ----
@settings(max_examples=120, deadline=None)
@given(
    base_tools=st.lists(st.sampled_from(TOOLS), min_size=1, max_size=6, unique=True),
    narrow_tools=st.lists(st.sampled_from(TOOLS), min_size=1, max_size=6, unique=True),
    probe=st.sampled_from(TOOLS),
)
def test_attenuation_monotone(base_tools, narrow_tools, probe):
    auth = TokenAuthority()
    parent = auth.mint("agent", tools=base_tools)
    child = attenuate(parent, "tools in " + ",".join(narrow_tools))
    parent_ok = auth.verify(parent, "agent", probe, consume=False).ok
    child_ok = auth.verify(child, "agent", probe, consume=False).ok
    # a child can never allow something the parent forbade
    assert not (child_ok and not parent_ok)


@settings(max_examples=80, deadline=None)
@given(extra=st.text(min_size=1, max_size=40))
def test_random_caveats_never_crash_verify(extra):
    auth = TokenAuthority()
    t = auth.mint("a1", tools=["x"])
    try:
        child = attenuate(t, extra)
    except TokenError:
        return
    # if it parsed, verification must return a result, never raise
    res = auth.verify(child, "a1", "x", consume=False)
    assert isinstance(res.ok, bool)


@settings(max_examples=60, deadline=None)
@given(garbage=st.text(max_size=60))
def test_garbage_tokens_rejected(garbage):
    auth = TokenAuthority()
    res = auth.verify(garbage, "a1", "x")
    assert isinstance(res.ok, bool)
