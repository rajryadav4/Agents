"""Python SDK: put any agent behind WARDEN in two lines.

In-process::

    agent = WardenClient.local(warden, "support-bot", token)
    agent.call("send_email", to=..., subject=..., body=...)

Over HTTP (agent runs elsewhere)::

    agent = WardenClient.remote("http://localhost:8000", "support-bot", token)

Wrap your existing tool functions so every call is governed::

    send_email = agent.guard("send_email")
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from warden.core.models import CallResult, ToolCall


class WardenBlocked(Exception):
    """Raised (when ``raise_on_block=True``) if WARDEN blocks a call."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        super().__init__(response.get("decision", {}).get("explanation", "blocked by WARDEN"))


class PendingApproval(Exception):
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.approval_id = response.get("decision", {}).get("approval_id")
        super().__init__(f"waiting for human approval ({self.approval_id})")


class WardenClient:
    def __init__(self, agent_id: str, token: str, transport: Callable[[str, str, dict | None], dict],
                 session_id: str | None = None, raise_on_block: bool = False) -> None:
        self.agent_id = agent_id
        self.token = token
        self.session_id = session_id
        self.raise_on_block = raise_on_block
        self._t = transport

    # ------------------------------------------------------------ factories
    @classmethod
    def local(cls, warden, agent_id: str, token: str, **kw: Any) -> "WardenClient":
        def transport(method: str, path: str, body: dict | None) -> dict:
            if path == "/v1/call":
                call = ToolCall(agent_id=body["agent_id"], tool=body["tool"], args=body.get("args") or {},
                                session_id=body.get("session_id"), token=body.get("token"))
                res: CallResult = warden.call(call)
                return res.to_dict(reveal_deception=False)
            if path == "/v1/sessions":
                return {"session_id": warden.open_session(body["agent_id"], body.get("intent", ""))}
            if path.startswith("/v1/approvals/"):
                return warden.approvals.get(path.rsplit("/", 1)[1]).to_dict()
            raise ValueError(path)

        return cls(agent_id, token, transport, **kw)

    @classmethod
    def remote(cls, base_url: str, agent_id: str, token: str, timeout: float = 10.0, **kw: Any) -> "WardenClient":
        base = base_url.rstrip("/")

        def transport(method: str, path: str, body: dict | None) -> dict:
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return json.loads(exc.read() or b"{}")

        return cls(agent_id, token, transport, **kw)

    # ------------------------------------------------------------------ API
    def session(self, intent: str) -> "WardenClient":
        """Declare what this agent is about to do. Actions outside the intent get flagged."""
        sid = self._t("POST", "/v1/sessions", {"agent_id": self.agent_id, "intent": intent})["session_id"]
        return WardenClient(self.agent_id, self.token, self._t, session_id=sid, raise_on_block=self.raise_on_block)

    def call(self, tool: str, **args: Any) -> dict[str, Any]:
        resp = self._t("POST", "/v1/call", {"agent_id": self.agent_id, "tool": tool, "args": args,
                                            "session_id": self.session_id, "token": self.token})
        if self.raise_on_block:
            if resp.get("status") == "pending_approval":
                raise PendingApproval(resp)
            if resp.get("status") == "blocked":
                raise WardenBlocked(resp)
        return resp

    def wait_for_approval(self, approval_id: str, timeout: float = 300, poll: float = 1.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            req = self._t("GET", f"/v1/approvals/{approval_id}", None)
            if req.get("status") != "pending":
                return req
            time.sleep(poll)
        raise TimeoutError(f"approval {approval_id} still pending after {timeout}s")

    def guard(self, tool: str) -> Callable[..., Any]:
        """Return a function that calls ``tool`` through WARDEN and returns its result (or raises)."""

        def wrapped(**args: Any) -> Any:
            resp = self.call(tool, **args)
            if resp.get("status") == "pending_approval":
                raise PendingApproval(resp)
            if resp.get("status") not in ("executed",):
                raise WardenBlocked(resp)
            return resp.get("result")

        wrapped.__name__ = tool
        return wrapped
