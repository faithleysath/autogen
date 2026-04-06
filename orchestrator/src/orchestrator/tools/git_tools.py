from __future__ import annotations

from typing import Any

from orchestrator.services.git_service import GitService
from orchestrator.tools.base import ToolContext, ToolSpec


class GitReadToolset:
    def __init__(self, context: ToolContext, git_service: GitService) -> None:
        self._context = context
        self._git = git_service

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="git_status",
                description="Read the current git status in short format.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=self.git_status,
                read_only=True,
            ),
            ToolSpec(
                name="git_diff",
                description="Read the current git diff against HEAD.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=self.git_diff,
                read_only=True,
            ),
            ToolSpec(
                name="git_head",
                description="Read the current HEAD commit SHA.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=self.git_head,
                read_only=True,
            ),
        ]

    async def git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        return {
            "status": await self._git.status_short(
                container_id=self._context.container_id,
                workspace=self._context.workspace,
            )
        }

    async def git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        return {
            "diff": await self._git.diff(
                container_id=self._context.container_id,
                workspace=self._context.workspace,
            )
        }

    async def git_head(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        return {
            "head": await self._git.current_head(
                container_id=self._context.container_id,
                workspace=self._context.workspace,
            )
        }
