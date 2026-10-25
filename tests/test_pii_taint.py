"""PII detection/vault and cross-agent taint tracking."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from warden.detect.pii import PIIVault, find_pii, iban_ok, luhn_ok
from warden.detect.taint import TaintTracker, atoms, shingles

VALID_CARDS = ["4111 1111 1111 1111", "5500 0000 0000 0004", "4111111111111111"]
INVALID_CARDS = ["1234 5678 9012 3456", "0000 0000 0000 0001"]


@pytest.mark.parametrize("card", VALID_CARDS)
def test_luhn_valid(card):
    assert luhn_ok(card)


@pytest.mark.parametrize("card", INVALID_CARDS)
def test_luhn_invalid(card):
    assert not luhn_ok(card)


def test_iban_valid():
    assert iban_ok("GB29 NWBK 6016 1331 9268 19")
    assert not iban_ok("GB00 XXXX 0000 0000 0000 00")


@pytest.mark.parametrize("text,kind", [
    ("contact me at john.doe@example.com", "email"),
    ("card 4111 1111 1111 1111", "credit_card"),
    ("ssn 123-45-6789", "ssn"),
    ("key AKIAIOSFODNN7EXAMPLE", "aws_key"),
    ("token sk_live_abcd1234efgh5678 ok", "api_key"),
])
def test_find_pii_kinds(text, kind):
    kinds = {m.kind for m in find_pii(text)}
    assert kind in kinds, (text, kinds)


def test_find_pii_none():
    assert find_pii("just some ordinary text with no secrets") == []


def test_vault_tokenize_detokenize():
    v = PIIVault()
    tok, kinds = v.tokenize_text("email me at alice@example.com")
    assert "alice@example.com" not in tok
    assert "email" in kinds
    assert v.detokenize(tok) == "email me at alice@example.com"


def test_vault_handle_stable():
    v = PIIVault()
    h1 = v.handle_for("email", "a@b.com")
    h2 = v.handle_for("email", "a@b.com")
    assert h1 == h2


def test_vault_gated_detokenize():
    v = PIIVault()
    tok, _ = v.tokenize_text("to alice@example.com and bob@example.com")
    # only release alice
    out = v.detokenize(tok, allow=lambda kind, real: real == "alice@example.com")
    assert "alice@example.com" in out
    assert "bob@example.com" not in out
    assert "redacted" in out


def test_vault_nested_structure():
    v = PIIVault()
    data = {"to": "a@b.com", "items": ["x@y.com", {"deep": "z@w.com"}]}
    tok, kinds = v.tokenize(data)
    assert len(kinds) == 3
    assert v.detokenize(tok) == data


# ---- taint ----
def test_taint_basic_flow():
    t = TaintTracker()
    t.label_output("customer email is secret@cust.com", "confidential", "a1", "db_query", "c1")
    hits = t.trace("here is secret@cust.com for you")
    assert hits and hits[0].label == "confidential"


def test_taint_no_false_hit():
    t = TaintTracker()
    t.label_output("alice@cust.com", "confidential", "a1", "db_query", "c1")
    assert t.trace("completely unrelated text") == []


def test_taint_shingle_match():
    t = TaintTracker()
    t.label_output("the quick brown fox jumps over the lazy dog today", "confidential", "a1", "db", "c1")
    hits = t.trace("I saw the quick brown fox jumps over the lazy dog today indeed")
    assert hits


def test_taint_cross_agent_path():
    t = TaintTracker()
    t.label_output("secret@cust.com data", "confidential", "support", "db_query", "c1")
    t.record_message("support", "research", "c2", "here is secret@cust.com data")
    path = t.path("support", "research")
    assert path == ["support", "research"]


def test_taint_allowlist():
    t = TaintTracker()
    t.domain_allowlist = {"acme.com"}
    t.label_output("internal@acme.com", "confidential", "a1", "db", "c1")
    # allowlisted atoms are not tracked
    assert t.trace("internal@acme.com") == []


def test_atoms_extracts_emails_urls():
    a = atoms("email x@y.com visit https://evil.io/path and key AKIAABCDEFGHIJKLMNOP")
    assert any("@" in x for x in a)
    assert any("evil.io" in x for x in a)


def test_shingles_short_text():
    assert shingles("too short") == set()
    assert len(shingles("one two three four five six seven")) > 0


@settings(max_examples=60, deadline=None)
@given(email=st.emails())
def test_vault_roundtrip_property(email):
    v = PIIVault()
    text = f"please contact {email} soon"
    tok, kinds = v.tokenize_text(text)
    if kinds:  # some generated emails may not match the conservative regex
        assert email not in tok
        assert v.detokenize(tok) == text


@settings(max_examples=50, deadline=None)
@given(data=st.text(min_size=30, max_size=200))
def test_taint_trace_consistent(data):
    t = TaintTracker()
    t.label_output(data, "confidential", "a1", "db", "c1")
    # tracing the same data always finds it (if it had any atoms/shingles)
    hits = t.trace(data)
    if atoms(data) or shingles(data):
        assert hits
