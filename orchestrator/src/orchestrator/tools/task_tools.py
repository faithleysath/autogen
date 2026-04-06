from __future__ import annotations

from typing import Any

from orchestrator.services.background_tasks import BackgroundTaskManager
from orchestrator.tools.base import ToolContext, ToolSpec
from orchestrator.tools.bash_tool import _iter_path_candidates, _resolve_visible_path, _validate_command


class TaskToolset:
    def __init__(self, context: ToolContext, background_tasks: BackgroundTaskManager) -> None:
        self._context = context
        self._background_tasks = background_tasks

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="task_create",
                description="Start a background command task in the current workspace container.",
                parameters={
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                        "cwd": {"type": "string"},
                    },
                    "required": ["description", "argv", "cwd"],
                    "additionalProperties": False,
                },
                handler=self.task_create,
            ),
            ToolSpec(
                name="task_get",
                description="Read metadata for one background task in the current workspace.",
                parameters={
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
                handler=self.task_get,
                read_only=True,
            ),
            ToolSpec(
                name="task_list",
                description="List background tasks created in the current workspace.",
                parameters={
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                handler=self.task_list,
                read_only=True,
            ),
            ToolSpec(
                name="task_output",
                description="Read recent output for one background task in the current workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string"},
                        "max_bytes": {"type": "integer"},
                    },
                    "required": ["task_id", "max_bytes"],
                    "additionalProperties": False,
                },
                handler=self.task_output,
                read_only=True,
            ),
            ToolSpec(
                name="task_stop",
                description="Stop a running background task in the current workspace.",
                parameters={
                    "type": "object",
                    "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"],
                    "additionalProperties": False,
                },
                handler=self.task_stop,
            ),
        ]

    async def task_create(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._context.policy.allow_command:
            raise PermissionError(f"{self._context.role} cannot run commands")
        if not self._context.policy.allow_code_write:
            raise PermissionError(f"{self._context.role} cannot create background tasks")
        argv = [str(item) for item in args["argv"]]
        _validate_command(argv)
        cwd = str(args["cwd"])
        self._context.workspace.ensure_within_workspace(cwd)
        for candidate in _iter_path_candidates(argv):
            self._context.workspace.ensure_within_workspace(_resolve_visible_path(candidate, cwd))
        return await self._background_tasks.create_command_task(
            workspace=self._context.workspace,
            container_id=self._context.container_id,
            role=self._context.role,
            description=str(args["description"]),
            argv=argv,
            cwd=cwd,
        )

    async def task_get(self, args: dict[str, Any]) -> dict[str, Any]:
        self._assert_task_visible(args["task_id"])
        return await self._background_tasks.get_task(task_id=args["task_id"])

    async def task_list(self, args: dict[str, Any]) -> dict[str, Any]:
        del args
        tasks = await self._background_tasks.list_tasks(
            run_id=self._context.workspace.run_id,
            workspace_id=self._context.workspace.workspace_id,
        )
        return {"tasks": tasks}

    async def task_output(self, args: dict[str, Any]) -> dict[str, Any]:
        self._assert_task_visible(args["task_id"])
        max_bytes = max(256, min(int(args["max_bytes"]), 24000))
        return self._background_tasks.read_output(task_id=args["task_id"], max_bytes=max_bytes)

    async def task_stop(self, args: dict[str, Any]) -> dict[str, Any]:
        self._assert_task_visible(args["task_id"])
        return await self._background_tasks.stop_task(task_id=args["task_id"])

    def _assert_task_visible(self, task_id: str) -> None:
        if not self._background_tasks.owns_task(
            task_id=task_id,
            run_id=self._context.workspace.run_id,
            workspace_id=self._context.workspace.workspace_id,
        ):
            raise PermissionError(f"{self._context.role} cannot access task {task_id}")
