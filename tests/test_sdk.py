"""Python SDK (local + remote transports)."""
import pytest

from warden.sdk import PendingApproval, WardenBlocked, WardenClient


def test_local_call(world):
    c = WardenClient.local(world.warden, "support-bot", world.tokens["support-bot"])
    r = c.call("kb_search", query="refund")
    assert r["status"] == "executed"


def test_local_blocked(world):
    c = WardenClient.local(world.warden, "support-bot", world.tokens["support-bot"])
    r = c.call("shell_exec", cmd="ls")
    assert r["status"] == "blocked"


def test_session(world):
    c = WardenClient.local(world.warden, "support-bot", world.tokens["support-bot"]).session("Resolve ticket")
    assert c.session_id
    assert c.call("kb_search", query="x")["status"] == "executed"


def test_guard_returns_result(world):
    c = WardenClient.local(world.warden, "support-bot", world.tokens["support-bot"])
    kb = c.guard("kb_search")
    assert isinstance(kb(query="refund"), list)


def test_guard_raises_on_block(world):
    c = WardenClient.local(world.warden, "support-bot", world.tokens["support-bot"])
    shell = c.guard("shell_exec")
    with pytest.raises(WardenBlocked):
        shell(cmd="ls")


def test_raise_on_block_flag(world):
    c = WardenClient.local(world.warden, "support-bot", world.tokens["support-bot"], raise_on_block=True)
    with pytest.raises(WardenBlocked):
        c.call("shell_exec", cmd="ls")


def test_pending_approval_raised(world):
    c = WardenClient.local(world.warden, "finance-bot", world.tokens["finance-bot"], raise_on_block=True)
    with pytest.raises(PendingApproval):
        c.call("issue_refund", customer_id="cus_1005", amount=2400, reason="x")


def test_remote_transport_against_testclient():
    # drive the remote SDK through the FastAPI TestClient's transport
    from fastapi.testclient import TestClient

    from warden.server.app import create_app

    app = create_app()
    tc = TestClient(app)
    token = app.state.world.tokens["support-bot"]

    def transport(method, path, body):
        r = tc.request(method, path, json=body) if body is not None else tc.request(method, path)
        return r.json()

    c = WardenClient("support-bot", token, transport)
    assert c.call("kb_search", query="x")["status"] == "executed"
    assert c.call("shell_exec", cmd="ls")["status"] == "blocked"
