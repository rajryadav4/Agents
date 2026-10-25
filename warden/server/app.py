"""WARDEN HTTP control plane: REST + Server-Sent Events + MCP endpoint + dashboard."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from warden import trust
from warden.approvals import ApprovalError
from warden.core.models import ToolCall
from warden.demo import scenarios
from warden.demo.world import AcmeWorld, build_world
from warden.policy import nl
from warden.policy.backtest import backtest
from warden.policy.engine import Policy, Rule
from warden.policy.lint import lint, summary
from warden.policy.recommend import coverage, recommend
from warden.redteam.ablation import run_ablation
from warden.redteam.attacks import run_campaign
from warden.simulate import blast_radius, compliance_pack

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_ATTEST_KEY = os.environ.get("WARDEN_ATTEST_KEY", "warden-demo-key").encode()


def create_app(world: AcmeWorld | None = None) -> FastAPI:
    world = world or build_world()
    w = world.warden
    app = FastAPI(title="WARDEN", version="1.0.0", description="Control plane & firewall for AI agents.")
    app.state.world = world

    def token_for(agent_id: str, authorization: str | None) -> str | None:
        if authorization and authorization.lower().startswith("bearer "):
            return authorization[7:].strip()
        return world.tokens.get(agent_id)

    # ----------------------------------------------------------- data plane
    @app.post("/v1/call")
    async def call(body: dict[str, Any], authorization: str | None = Header(default=None)) -> JSONResponse:
        if "agent_id" not in body or "tool" not in body:
            raise HTTPException(422, "agent_id and tool are required")
        tc = ToolCall(agent_id=body["agent_id"], tool=body["tool"], args=body.get("args") or {},
                      session_id=body.get("session_id"), token=body.get("token") or token_for(body["agent_id"], authorization))
        res = await asyncio.to_thread(w.call, tc)
        status = 200 if res.status.value in ("executed", "deceived") else 202 if res.status.value == "pending_approval" else 403
        return JSONResponse(res.to_dict(reveal_deception=False), status_code=status)

    @app.post("/v1/sessions")
    async def open_session(body: dict[str, Any]) -> dict[str, Any]:
        return {"session_id": w.open_session(body["agent_id"], body.get("intent", ""))}

    # ----------------------------------------------------------- read models
    @app.get("/v1/stats")
    async def stats() -> dict[str, Any]:
        return w.stats()

    @app.get("/v1/agents")
    async def agents() -> dict[str, Any]:
        out = []
        for a in w.agents.all():
            d = a.to_dict()
            d["counts"] = dict(w.agent_counts.get(a.id, {}))
            d["breaker"] = w.breaker.state(a.id)
            out.append(d)
        return {"agents": out}

    @app.get("/v1/agents/{agent_id}/blast-radius")
    async def blast(agent_id: str) -> dict[str, Any]:
        try:
            return blast_radius(w, agent_id, world.tokens.get(agent_id))
        except KeyError:
            raise HTTPException(404, f"unknown agent {agent_id}")

    @app.post("/v1/agents/{agent_id}/kill")
    async def kill(agent_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not w.agents.get(agent_id):
            raise HTTPException(404, "unknown agent")
        killed = w.kill_agent(agent_id, (body or {}).get("reason", "manual kill switch"), (body or {}).get("actor", "dashboard"))
        return {"killed": killed}

    @app.post("/v1/agents/{agent_id}/restore")
    async def restore(agent_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            w.restore_agent(agent_id, (body or {}).get("actor", "dashboard"))
        except (KeyError, PermissionError) as exc:
            raise HTTPException(400, str(exc))
        return {"ok": True}

    @app.get("/v1/tools")
    async def tools() -> dict[str, Any]:
        return {"tools": [t.describe() for t in w.tools.all()]}

    @app.get("/v1/events")
    async def events(limit: int = 200, kind: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
        recs = w.ledger.records(kind=kind, agent_id=agent_id, limit=limit, newest_first=True)
        return {"events": recs}

    @app.get("/v1/incidents")
    async def incidents() -> dict[str, Any]:
        return {"incidents": [i.to_dict() for i in w.incidents.all()]}

    # ----------------------------------------------------------- approvals
    @app.get("/v1/approvals")
    async def approvals(all: bool = False) -> dict[str, Any]:
        items = w.approvals.all() if all else w.approvals.pending()
        return {"approvals": [a.to_dict() for a in items]}

    @app.post("/v1/approvals/{approval_id}/decide")
    async def decide(approval_id: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(w.decide_approval, approval_id, body.get("approver", ""),
                                           bool(body.get("approve")), body.get("note", ""))
        except ApprovalError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/v1/approvals/{approval_id}")
    async def get_approval(approval_id: str) -> dict[str, Any]:
        try:
            return w.approvals.get(approval_id).to_dict()
        except ApprovalError:
            raise HTTPException(404, "unknown approval")

    # ----------------------------------------------------------- policy
    @app.get("/v1/policy")
    async def get_policy() -> dict[str, Any]:
        issues = lint(w.policy, known_tools=w.tools.names())
        return {"policy": w.policy.to_dict(), "yaml": w.policy.to_yaml(), "lint": issues, "lint_summary": summary(issues),
                "fingerprint": w.policy.fingerprint(), "coverage": coverage(w)}

    @app.post("/v1/policy/compile")
    async def compile_nl(body: dict[str, Any]) -> dict[str, Any]:
        c = nl.compile_rule(body.get("text", ""), known_tools=w.tools.names(), org_domain=w.org_domains[0])
        result = c.to_dict()
        if c.rule:
            result["lint"] = lint(Policy(rules=[Rule.from_dict(c.rule)]), known_tools=w.tools.names())
        return result

    @app.post("/v1/policy/lint")
    async def lint_policy(body: dict[str, Any]) -> dict[str, Any]:
        try:
            pol = Policy.from_yaml(body["yaml"]) if "yaml" in body else Policy.from_dict(body.get("policy"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        issues = lint(pol, known_tools=w.tools.names())
        return {"ok": summary(issues)["error"] == 0, "lint": issues, "summary": summary(issues)}

    @app.post("/v1/policy/backtest")
    async def backtest_policy(body: dict[str, Any]) -> dict[str, Any]:
        try:
            candidate = Policy.from_yaml(body["yaml"]) if "yaml" in body else Policy.from_dict(body["policy"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"could not parse candidate policy: {exc}")
        roles = {a.id: a.role for a in w.agents.all()}
        return backtest(w.ledger.records(kind="call"), w.policy, candidate, roles)

    @app.post("/v1/policy/apply")
    async def apply_policy(body: dict[str, Any]) -> dict[str, Any]:
        try:
            pol = Policy.from_yaml(body["yaml"]) if "yaml" in body else Policy.from_dict(body["policy"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"could not parse policy: {exc}")
        issues = lint(pol, known_tools=w.tools.names())
        if summary(issues)["error"]:
            raise HTTPException(400, "policy has lint errors; fix them before applying")
        w.set_policy(pol, actor=body.get("actor", "dashboard"))
        return {"ok": True, "fingerprint": pol.fingerprint(), "rules": len(pol.rules)}

    @app.get("/v1/policy/recommend")
    async def policy_recommend() -> dict[str, Any]:
        return {"suggestions": recommend(w)}

    # ----------------------------------------------------------- assurance
    @app.post("/v1/redteam/run")
    async def redteam() -> dict[str, Any]:
        rep = await asyncio.to_thread(run_campaign, w.policy, w.settings)
        w.redteam_reports.append(rep)
        w._publish({"type": "redteam", "summary": {k: rep[k] for k in ("blocked", "attacks", "weighted_block_rate")}})
        return rep

    @app.post("/v1/redteam/ablation")
    async def ablation() -> dict[str, Any]:
        return await asyncio.to_thread(run_ablation, w.policy)

    @app.get("/v1/trust")
    async def trust_score() -> dict[str, Any]:
        rep = await asyncio.to_thread(trust.compute, w)
        return trust.attest(rep, _ATTEST_KEY)

    @app.get("/v1/trust/badge.svg")
    async def trust_badge(dark: int = 0) -> Response:
        rep = await asyncio.to_thread(trust.compute, w)
        return Response(trust.badge_svg(rep, dark=bool(dark)), media_type="image/svg+xml",
                        headers={"Cache-Control": "max-age=300"})

    @app.get("/v1/compliance")
    async def compliance() -> dict[str, Any]:
        return compliance_pack(w)

    @app.get("/v1/ledger/verify")
    async def ledger_verify() -> dict[str, Any]:
        return w.ledger.verify()

    @app.post("/v1/ledger/checkpoint")
    async def ledger_checkpoint() -> dict[str, Any]:
        return w.ledger.checkpoint()

    @app.get("/v1/ledger/proof/{seq}")
    async def ledger_proof(seq: int) -> dict[str, Any]:
        try:
            return w.ledger.proof(seq)
        except KeyError:
            raise HTTPException(404, f"no ledger record {seq}")

    # ----------------------------------------------------------- demo
    @app.get("/v1/scenarios")
    async def list_scenarios() -> dict[str, Any]:
        return {"scenarios": [{"id": k} for k in scenarios.SCENARIOS]}

    @app.post("/v1/scenarios/{name}/run")
    async def run_scenario(name: str) -> dict[str, Any]:
        if name not in scenarios.SCENARIOS:
            raise HTTPException(404, f"unknown scenario {name}")
        res = await asyncio.to_thread(scenarios.run, world, name)
        return res.to_dict()

    @app.post("/v1/scenarios/run-all")
    async def run_all() -> dict[str, Any]:
        results = await asyncio.to_thread(scenarios.run_all, world)
        return {"results": [r.to_dict() for r in results], "passed": sum(r.passed for r in results), "total": len(results)}

    @app.post("/v1/demo/reset")
    async def reset() -> dict[str, Any]:
        new = build_world()
        app.state.world = new
        _rebind(app, new)
        return {"ok": True}

    # ----------------------------------------------------------- live stream
    @app.get("/v1/stream")
    async def stream(request: Request) -> StreamingResponse:
        queue = w.bus.subscribe_async()
        recent = list(w.bus.recent)[-30:]

        async def gen():
            try:
                yield f"event: hello\ndata: {json.dumps({'recent': recent})}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                w.bus.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ----------------------------------------------------------- MCP endpoint
    @app.post("/mcp")
    async def mcp(body: dict[str, Any], authorization: str | None = Header(default=None)) -> JSONResponse:
        return JSONResponse(await _mcp_handle(w, world, body, authorization))

    @app.get("/healthz")
    async def health() -> dict[str, Any]:
        return {"ok": True, "uptime_s": round(time.time() - w.started_at, 1), "calls": w.counters["calls"]}

    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> HTMLResponse:
        path = os.path.join(STATIC_DIR, "index.html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return HTMLResponse(fh.read())
        return HTMLResponse("<h1>WARDEN</h1><p>Dashboard not built.</p>")

    @app.get("/badge")
    async def badge_demo() -> PlainTextResponse:
        return PlainTextResponse('<img src="/v1/trust/badge.svg" alt="Secured by WARDEN">', media_type="text/plain")

    return app


def _rebind(app: FastAPI, world: AcmeWorld) -> None:
    # Rebuild routes closed over the old world by recreating the app's world refs.
    new = create_app(world)
    app.router.routes = new.router.routes
    app.state.world = world


# --------------------------------------------------------------------------- MCP
def _mcp_tools(w) -> list[dict[str, Any]]:
    out = []
    for t in w.tools.all():
        props = {}
        required = []
        for name, typ in (t.params or {}).items():
            optional = typ.endswith("?")
            props[name] = {"type": "string", "description": typ.rstrip("?")}
            if not optional:
                required.append(name)
        out.append({"name": t.name, "description": t.description,
                    "inputSchema": {"type": "object", "properties": props, "required": required}})
    return out


async def _mcp_handle(w, world, body: dict[str, Any], authorization: str | None) -> dict[str, Any]:
    mid = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    def ok(result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                   "serverInfo": {"name": "warden", "version": "1.0.0"}})
    if method in ("tools/list", "list_tools"):
        return ok({"tools": _mcp_tools(w)})
    if method in ("tools/call", "call_tool"):
        name = params.get("name")
        args = params.get("arguments") or {}
        agent_id = args.pop("__agent_id__", None) or params.get("agent_id") or "support-bot"
        token = None
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        token = token or world.tokens.get(agent_id)
        tc = ToolCall(agent_id=agent_id, tool=name, args=args, token=token)
        res = await asyncio.to_thread(w.call, tc)
        d = res.to_dict(reveal_deception=False)
        blocked = res.status.value not in ("executed", "deceived")
        text = (d["decision"]["explanation"] if blocked else json.dumps(d["result"], default=str))
        return ok({"content": [{"type": "text", "text": text}], "isError": blocked,
                   "_warden": {"verdict": d["decision"]["verdict"], "status": d["status"]}})
    return err(-32601, f"method not found: {method}")
