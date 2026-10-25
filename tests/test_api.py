"""HTTP API, SSE, and MCP endpoint tests via FastAPI TestClient."""
import pytest
from fastapi.testclient import TestClient

from warden.server.app import create_app


@pytest.fixture
def client():
    return TestClient(create_app())


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json()["ok"]


def test_index_served(client):
    assert client.get("/").status_code == 200


def test_stats(client):
    assert "verdicts" in client.get("/v1/stats").json()


def test_call_allow(client):
    r = client.post("/v1/call", json={"agent_id": "support-bot", "tool": "kb_search", "args": {"query": "refund"}})
    assert r.status_code == 200
    assert r.json()["status"] == "executed"


def test_call_blocked(client):
    r = client.post("/v1/call", json={"agent_id": "support-bot", "tool": "shell_exec", "args": {"cmd": "ls"}})
    assert r.status_code == 403
    assert r.json()["status"] == "blocked"


def test_call_held(client):
    r = client.post("/v1/call", json={"agent_id": "support-bot", "tool": "send_email",
                                      "args": {"to": "x@ext.com", "subject": "s", "body": "b"}})
    assert r.status_code == 202
    assert r.json()["status"] == "pending_approval"


def test_call_validation(client):
    assert client.post("/v1/call", json={"tool": "x"}).status_code == 422


def test_agents_list(client):
    agents = client.get("/v1/agents").json()["agents"]
    assert len(agents) >= 4
    assert all("status" in a for a in agents)


def test_kill_and_restore(client):
    assert client.post("/v1/agents/support-bot/kill", json={"reason": "t"}).status_code == 200
    # killed cannot be restored
    r = client.post("/v1/agents/support-bot/restore", json={})
    assert r.status_code == 400


def test_blast_radius_endpoint(client):
    b = client.get("/v1/agents/ops-bot/blast-radius").json()
    assert "assessment" in b and "reachable" in b


def test_tools_endpoint(client):
    assert len(client.get("/v1/tools").json()["tools"]) >= 10


def test_policy_endpoint(client):
    p = client.get("/v1/policy").json()
    assert "yaml" in p and "lint" in p and "coverage" in p


def test_policy_compile(client):
    r = client.post("/v1/policy/compile", json={"text": "never send email outside acme.com"}).json()
    assert r["rule"]["action"] == "deny"


def test_policy_lint(client):
    r = client.post("/v1/policy/lint", json={"yaml": "rules:\n  - id: r\n    when: {tool: x}\n    action: deny\n"}).json()
    assert "summary" in r


def test_policy_lint_bad_yaml(client):
    r = client.post("/v1/policy/lint", json={"yaml": ":::not yaml:::["}).json()
    assert r["ok"] is False


def test_policy_backtest(client):
    client.post("/v1/call", json={"agent_id": "support-bot", "tool": "kb_search", "args": {"query": "x"}})
    yaml = "rules:\n  - id: block-kb\n    when: {tool: kb_search}\n    action: deny\n"
    r = client.post("/v1/policy/backtest", json={"yaml": yaml}).json()
    assert r["calls_replayed"] >= 1


def test_policy_apply(client):
    yaml = "name: t\nrules:\n  - id: r1\n    description: d\n    when: {tool: send_email}\n    action: warn\n"
    r = client.post("/v1/policy/apply", json={"yaml": yaml}).json()
    assert r["ok"]


def test_policy_recommend(client):
    assert "suggestions" in client.get("/v1/policy/recommend").json()


def test_scenarios_list(client):
    assert len(client.get("/v1/scenarios").json()["scenarios"]) == 11


def test_run_scenario(client):
    r = client.post("/v1/scenarios/poisoned_webpage/run").json()
    assert r["passed"]


def test_run_all_scenarios(client):
    r = client.post("/v1/scenarios/run-all").json()
    assert r["passed"] == r["total"] == 11


def test_redteam_run(client):
    r = client.post("/v1/redteam/run").json()
    assert r["blocked"] == r["attacks"]


def test_trust_endpoint(client):
    r = client.get("/v1/trust").json()
    assert 0 <= r["score"] <= 100
    assert "attestation" in r


def test_trust_badge(client):
    r = client.get("/v1/trust/badge.svg")
    assert r.status_code == 200 and "svg" in r.headers["content-type"]
    assert b"Secured by WARDEN" in r.content


def test_compliance_endpoint(client):
    r = client.get("/v1/compliance").json()
    assert "controls" in r and r["ledger"]["ok"]


def test_ledger_verify_endpoint(client):
    assert client.get("/v1/ledger/verify").json()["ok"]


def test_ledger_checkpoint(client):
    client.post("/v1/call", json={"agent_id": "support-bot", "tool": "kb_search", "args": {"query": "x"}})
    cp = client.post("/v1/ledger/checkpoint").json()
    assert "root" in cp and "signature" in cp


def test_ledger_proof(client):
    client.post("/v1/call", json={"agent_id": "support-bot", "tool": "kb_search", "args": {"query": "x"}})
    p = client.get("/v1/ledger/proof/1").json()
    assert "path" in p and "root" in p


def test_incidents_endpoint(client):
    client.post("/v1/scenarios/canary_exfiltration/run")
    r = client.get("/v1/incidents").json()
    assert len(r["incidents"]) >= 1


def test_approvals_flow(client):
    client.post("/v1/call", json={"agent_id": "finance-bot", "tool": "issue_refund",
                                  "args": {"customer_id": "cus_1005", "amount": 2400, "reason": "x"}})
    pending = client.get("/v1/approvals").json()["approvals"]
    assert pending
    apr = pending[0]["id"]
    client.post(f"/v1/approvals/{apr}/decide", json={"approver": "a@acme.com", "approve": True})
    r = client.post(f"/v1/approvals/{apr}/decide", json={"approver": "b@acme.com", "approve": True}).json()
    assert r["status"] == "approved"


def test_approval_self_rejected(client):
    client.post("/v1/call", json={"agent_id": "finance-bot", "tool": "issue_refund",
                                  "args": {"customer_id": "cus_1005", "amount": 2400, "reason": "x"}})
    apr = client.get("/v1/approvals").json()["approvals"][0]["id"]
    r = client.post(f"/v1/approvals/{apr}/decide", json={"approver": "finance-bot", "approve": True})
    assert r.status_code == 400


# ---- MCP ----
def test_mcp_initialize(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}).json()
    assert r["result"]["serverInfo"]["name"] == "warden"


def test_mcp_tools_list(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    assert len(r["result"]["tools"]) >= 10


def test_mcp_call_allow(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "kb_search", "agent_id": "support-bot", "arguments": {"query": "refund"}}}).json()
    assert r["result"]["isError"] is False


def test_mcp_call_blocked(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": "shell_exec", "agent_id": "support-bot", "arguments": {"cmd": "ls"}}}).json()
    assert r["result"]["isError"] is True


def test_mcp_unknown_method(client):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "bogus"}).json()
    assert r["error"]["code"] == -32601


def test_sessions_endpoint(client):
    r = client.post("/v1/sessions", json={"agent_id": "support-bot", "intent": "help"}).json()
    assert r["session_id"].startswith("ses_")


def test_stream_sse(client):
    with client.stream("GET", "/v1/stream") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line:
                assert "hello" in line or "data" in line or line.startswith(":")
                break
