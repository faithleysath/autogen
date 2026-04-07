from __future__ import annotations

import pytest

from orchestrator.models.runtime import VISIBLE_ROOT, WorkspaceView
from orchestrator.policy.role_policy import build_role_policy
from orchestrator.tools.base import ToolContext
from orchestrator.tools.task_tools import TaskToolset


class FakeBackgroundTaskManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_command_task(
        self,
        *,
        workspace: WorkspaceView,
        container_id: str,
        role: str,
        description: str,
        argv: list[str],
        cwd: str,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "workspace_id": workspace.workspace_id,
                "container_id": container_id,
                "role": role,
                "description": description,
                "argv": list(argv),
                "cwd": cwd,
            }
        )
        return {"task_id": "task-1", "status": "running"}


def _make_context(tmp_path, *, role: str) -> ToolContext:
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="stage-dev",
    )
    policy_kwargs = {"role": role, "run_id": "run-1", "cycle_no": 1}
    if role == "stage_gate":
        policy_kwargs.update({"stage_no": 1, "attempt_no": 1})
    if role in {"compliance", "qa", "e2e", "release_gate"}:
        policy_kwargs.update({"release_no": 1})
    policy = build_role_policy(**policy_kwargs)
    return ToolContext(
        role=role,
        workspace=workspace,
        container_id="container-1",
        policy=policy,
    )


@pytest.mark.anyio
async def test_task_create_allows_e2e_background_tasks_without_code_write(tmp_path):
    context = _make_context(tmp_path, role="e2e")
    background_tasks = FakeBackgroundTaskManager()
    tool = TaskToolset(context, background_tasks)

    result = await tool.task_create(
        {
            "description": "start preview server",
            "argv": ["npm", "run", "dev"],
            "cwd": "/workspace",
        }
    )

    assert result == {"task_id": "task-1", "status": "running"}
    assert background_tasks.calls == [
        {
            "workspace_id": "ws",
            "container_id": "container-1",
            "role": "e2e",
            "description": "start preview server",
            "argv": ["npm", "run", "dev"],
            "cwd": "/workspace",
        }
    ]
    assert not context.policy.allow_code_write
    assert context.policy.allow_background_tasks


@pytest.mark.anyio
async def test_task_create_allows_e2e_absolute_bun_path(tmp_path):
    context = _make_context(tmp_path, role="e2e")
    background_tasks = FakeBackgroundTaskManager()
    tool = TaskToolset(context, background_tasks)

    result = await tool.task_create(
        {
            "description": "start preview server",
            "argv": ["/opt/bun/bin/bun", "run", "preview"],
            "cwd": "/workspace",
        }
    )

    assert result == {"task_id": "task-1", "status": "running"}
    assert background_tasks.calls[0]["argv"] == ["/opt/bun/bin/bun", "run", "preview"]


@pytest.mark.anyio
async def test_task_create_rejects_roles_without_background_task_permission(tmp_path):
    context = _make_context(tmp_path, role="architect")
    tool = TaskToolset(context, FakeBackgroundTaskManager())

    with pytest.raises(PermissionError, match="architect cannot create background tasks"):
        await tool.task_create(
            {
                "description": "start preview server",
                "argv": ["npm", "run", "dev"],
                "cwd": "/workspace",
            }
        )
