"""Hash-chained ledger: integrity, tamper detection, Merkle proofs, checkpoints."""
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from warden.audit.ledger import Ledger, merkle_proof, merkle_root, verify_proof


@pytest.fixture
def ledger():
    lg = Ledger(":memory:")
    yield lg
    lg.close()


def test_append_and_length(ledger):
    for i in range(10):
        ledger.append("call", {"i": i}, "a1")
    assert len(ledger) == 10


def test_sequence_monotonic(ledger):
    seqs = [ledger.append("x", {"i": i})["seq"] for i in range(20)]
    assert seqs == list(range(1, 21))


def test_verify_clean(ledger):
    for i in range(50):
        ledger.append("call", {"i": i})
    v = ledger.verify()
    assert v["ok"] and v["count"] == 50 and v["broken_at"] is None


def test_tamper_detected(ledger):
    for i in range(30):
        ledger.append("call", {"i": i})
    ledger._tamper(15, {"i": "HACKED"})
    v = ledger.verify()
    assert not v["ok"] and v["broken_at"] == 15


def test_tamper_first_record(ledger):
    for i in range(10):
        ledger.append("call", {"i": i})
    ledger._tamper(1, {"i": "x"})
    assert ledger.verify()["broken_at"] == 1


def test_tamper_last_record(ledger):
    for i in range(10):
        ledger.append("call", {"i": i})
    ledger._tamper(10, {"i": "x"})
    assert ledger.verify()["broken_at"] == 10


def test_filter_by_kind(ledger):
    ledger.append("call", {})
    ledger.append("policy", {})
    ledger.append("call", {})
    assert len(ledger.records(kind="call")) == 2


def test_filter_by_agent(ledger):
    ledger.append("call", {}, "a1")
    ledger.append("call", {}, "a2")
    assert len(ledger.records(agent_id="a1")) == 1


def test_merkle_root_empty():
    assert merkle_root([]) == "0" * 64


def test_merkle_root_stable():
    leaves = [f"{i:064x}" for i in range(7)]
    assert merkle_root(leaves) == merkle_root(list(leaves))


def test_merkle_proof_valid(ledger):
    for i in range(16):
        ledger.append("call", {"i": i})
    for seq in range(1, 17):
        p = ledger.proof(seq)
        assert verify_proof(p["leaf"], p["path"], p["root"])


def test_merkle_proof_wrong_leaf_fails(ledger):
    for i in range(8):
        ledger.append("call", {"i": i})
    p = ledger.proof(3)
    assert not verify_proof("deadbeef" * 8, p["path"], p["root"])


def test_checkpoint_sign_verify(ledger):
    for i in range(12):
        ledger.append("call", {"i": i})
    cp = ledger.checkpoint()
    assert ledger.verify_checkpoint(cp)


def test_checkpoint_detects_tamper(ledger):
    for i in range(12):
        ledger.append("call", {"i": i})
    cp = ledger.checkpoint()
    ledger._tamper(5, {"x": 1})
    assert not ledger.verify_checkpoint(cp)


def test_checkpoint_forged_signature(ledger):
    for i in range(5):
        ledger.append("call", {"i": i})
    cp = ledger.checkpoint()
    cp["signature"] = "0" * 64
    assert not ledger.verify_checkpoint(cp)


@settings(max_examples=50, deadline=None)
@given(n=st.integers(min_value=1, max_value=40), idx=st.integers(min_value=0, max_value=39))
def test_merkle_proof_property(n, idx):
    leaves = [f"{i:064x}" for i in range(n)]
    i = idx % n
    path = merkle_proof(leaves, i)
    assert verify_proof(leaves[i], path, merkle_root(leaves))


@settings(max_examples=40, deadline=None)
@given(payloads=st.lists(st.dictionaries(st.text(max_size=8), st.integers()), min_size=1, max_size=30),
       tamper_at=st.integers(min_value=1, max_value=30))
def test_any_tamper_detected_property(payloads, tamper_at):
    lg = Ledger(":memory:")
    try:
        for p in payloads:
            lg.append("call", p)
        assert lg.verify()["ok"]
        t = ((tamper_at - 1) % len(payloads)) + 1
        lg._tamper(t, {"tampered": True, "_x": 999})
        v = lg.verify()
        # tamper is detected unless the "new" content hashes identically (astronomically unlikely)
        assert not v["ok"] or payloads[t - 1] == {"tampered": True, "_x": 999}
    finally:
        lg.close()
