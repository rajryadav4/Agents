"""Tool registry: what tools exist and how risky / sensitive each one is."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# Information-flow labels, ordered from least to most sensitive.
CONFIDENTIALITY_LEVELS = ["public", "internal", "confidential", "secret"]


def conf_rank(label: str) -> int:
    return CONFIDENTIALITY_LEVELS.index(label) if label in CONFIDENTIALITY_LEVELS else 0


@dataclass
class ToolSpec:
    """Describes a tool an agent can call through WARDEN.

    category        read | write | external | exec | payment | comms | agent
    trust           integrity of the tool's *output*: trusted | untrusted (web, inbound email...)
    confidentiality label of the tool's *output*: public | internal | confidential | secret
    sink            where the tool's *arguments* go: None | internal | external
    dest_args       args naming a destination (recipient, url, command) — checked for tainted origins
    content_args    args carrying content that leaves through this tool
    risk            base risk points charged against the session's risk budget
    cost_usd        estimated spend per call (for spend circuit breakers)
    honeypot        deceptive implementation used for quarantined agents
    """

    name: str
    fn: Callable[[dict[str, Any]], Any]
    description: str = ""
    category: str = "read"
    trust: str = "trusted"
    confidentiality: str = "public"
    sink: str | None = None
    dest_args: list[str] = field(default_factory=list)
    content_args: list[str] = field(default_factory=list)
    risk: int = 1
    cost_usd: float = 0.0
    honeypot: Callable[[dict[str, Any]], Any] | None = None
    params: dict[str, str] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "trust": self.trust,
            "confidentiality": self.confidentiality,
            "sink": self.sink,
            "dest_args": self.dest_args,
            "content_args": self.content_args,
            "risk": self.risk,
            "cost_usd": self.cost_usd,
            "params": self.params,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        self._tools[spec.name] = spec
        return spec

    def tool(self, name: str | None = None, **kwargs: Any) -> Callable:
        """Decorator form: ``@registry.tool(category="external", sink="external")``."""

        def wrap(fn: Callable[[dict[str, Any]], Any]) -> Callable:
            self.register(ToolSpec(name=name or fn.__name__, fn=fn, description=(fn.__doc__ or "").strip(), **kwargs))
            return fn

        return wrap

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)
