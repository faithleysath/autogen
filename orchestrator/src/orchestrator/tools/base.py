from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from orchestrator.models.runtime import WorkspaceView
from orchestrator.policy.role_policy import RolePolicy


ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
ReadOnlyEvaluator = Callable[[dict[str, Any]], bool]


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    strict: bool = True
    read_only: bool | ReadOnlyEvaluator = False

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "strict": self.strict,
            "parameters": self.parameters,
        }

    def invocation_is_read_only(self, args: dict[str, Any]) -> bool:
        if callable(self.read_only):
            return bool(self.read_only(args))
        return bool(self.read_only)


@dataclass(slots=True)
class ToolContext:
    role: str
    workspace: WorkspaceView
    container_id: str
    policy: RolePolicy
