"""Agent registry: every agent has an identity, an owner, a role and a status."""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any


class AgentStatus:
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    KILLED = "killed"


@dataclass
class Agent:
    id: str
    name: str
    role: str = "general"
    owner: str = "unknown"
    description: str = ""
    parent: str | None = None  # delegating agent, if this is a sub-agent
    status: str = AgentStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    status_reason: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}
        self._lock = threading.RLock()

    def register(self, agent_id: str, name: str | None = None, role: str = "general", owner: str = "unknown",
                 description: str = "", parent: str | None = None, tags: list[str] | None = None) -> Agent:
        with self._lock:
            if parent is not None and parent not in self._agents:
                raise KeyError(f"parent agent '{parent}' is not registered")
            if parent is not None and self._agents[parent].status != AgentStatus.ACTIVE:
                raise PermissionError(f"parent agent '{parent}' is {self._agents[parent].status}; cannot delegate")
            agent = Agent(id=agent_id, name=name or agent_id, role=role, owner=owner, description=description,
                          parent=parent, tags=list(tags or []))
            self._agents[agent_id] = agent
            return agent

    def get(self, agent_id: str) -> Agent | None:
        with self._lock:
            return self._agents.get(agent_id)

    def all(self) -> list[Agent]:
        with self._lock:
            return list(self._agents.values())

    def ancestors(self, agent_id: str) -> list[str]:
        out: list[str] = []
        seen = {agent_id}
        with self._lock:
            cur = self._agents.get(agent_id)
            while cur and cur.parent and cur.parent not in seen:
                out.append(cur.parent)
                seen.add(cur.parent)
                cur = self._agents.get(cur.parent)
        return out

    def descendants(self, agent_id: str) -> list[str]:
        with self._lock:
            children: dict[str, list[str]] = {}
            for a in self._agents.values():
                if a.parent:
                    children.setdefault(a.parent, []).append(a.id)
        out: list[str] = []
        stack = list(children.get(agent_id, []))
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.append(cur)
            stack.extend(children.get(cur, []))
        return out

    def set_status(self, agent_id: str, status: str, reason: str = "") -> Agent:
        with self._lock:
            agent = self._agents[agent_id]
            agent.status = status
            agent.status_reason = reason
            return agent
