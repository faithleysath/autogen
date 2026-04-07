from __future__ import annotations

import asyncio
import time

import pytest

from orchestrator.models.runtime import ExecResult, VISIBLE_ROOT, WorkspaceView
from orchestrator.services.background_tasks import BackgroundTaskManager


class LocalDockerManager:
    def __init__(self, workspace_root, tasks_root):
        self._workspace_root = workspace_root
        self._tasks_root = tasks_root

    def _translate(self, value: str) -> str:
        return (
            value.replace("/workspace", self._workspace_root.as_posix())
            .replace("/autogen-state/tasks", self._tasks_root.as_posix())
        )

    async def exec(self, *, container_id: str, cmd: list[str], cwd: str, timeout_seconds: int) -> ExecResult:
        del container_id
        translated_cmd = [self._translate(part) for part in cmd]
        translated_cwd = self._translate(cwd)
        process = await asyncio.create_subprocess_exec(
            *translated_cmd,
            cwd=translated_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        return ExecResult(
            cmd=translated_cmd,
            cwd=translated_cwd,
            exit_code=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


class RecordingDockerManager:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def exec(self, *, container_id: str, cmd: list[str], cwd: str, timeout_seconds: int) -> ExecResult:
        del container_id, cwd, timeout_seconds
        self.calls.append(cmd)
        if cmd[:2] == ["sh", "-c"] and "echo $!" in cmd[2]:
            return ExecResult(cmd=cmd, cwd="/workspace", exit_code=0, stdout="123\n", stderr="")
        return ExecResult(cmd=cmd, cwd="/workspace", exit_code=0, stdout="", stderr="")


def _workspace(tmp_path) -> WorkspaceView:
    return WorkspaceView(
        workspace_id="stage-001-dev",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="stage-dev",
    )


@pytest.mark.anyio
async def test_background_task_manager_runs_and_reads_output(tmp_path):
    manager = BackgroundTaskManager(
        docker_manager=LocalDockerManager(tmp_path, tmp_path / "_state" / "tasks"),
        tasks_root=tmp_path / "_state" / "tasks",
    )
    workspace = _workspace(tmp_path)

    task = await manager.create_command_task(
        workspace=workspace,
        container_id="container-1",
        role="developer",
        description="emit output",
        argv=[
            "python3",
            "-c",
            "import time; print('start', flush=True); time.sleep(0.2); print('done', flush=True)",
        ],
        cwd="/workspace",
    )

    deadline = time.monotonic() + 5
    status = task["status"]
    while status == "running" and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        task = await manager.get_task(task_id=task["task_id"])
        status = task["status"]

    assert task["status"] == "completed"
    output = manager.read_output(task_id=task["task_id"])
    assert "start" in output["output"]
    assert "done" in output["output"]

    tasks = await manager.list_tasks(run_id="run-1", workspace_id="stage-001-dev")
    assert len(tasks) == 1
    assert tasks[0]["task_id"] == task["task_id"]


@pytest.mark.anyio
async def test_background_task_manager_stops_running_task(tmp_path):
    manager = BackgroundTaskManager(
        docker_manager=LocalDockerManager(tmp_path, tmp_path / "_state" / "tasks"),
        tasks_root=tmp_path / "_state" / "tasks",
    )
    workspace = _workspace(tmp_path)

    task = await manager.create_command_task(
        workspace=workspace,
        container_id="container-1",
        role="developer",
        description="long sleep",
        argv=[
            "python3",
            "-c",
            "import time; print('sleeping', flush=True); time.sleep(5)",
        ],
        cwd="/workspace",
    )

    stopped = await manager.stop_task(task_id=task["task_id"])
    assert stopped["status"] == "stopped"
    assert stopped["exit_code"] == -15


@pytest.mark.anyio
async def test_background_task_manager_uses_non_login_shell_for_command_tasks(tmp_path):
    docker = RecordingDockerManager()
    manager = BackgroundTaskManager(
        docker_manager=docker,
        tasks_root=tmp_path / "_state" / "tasks",
    )

    await manager.create_command_task(
        workspace=_workspace(tmp_path),
        container_id="container-1",
        role="e2e",
        description="start preview server",
        argv=["bun", "run", "preview"],
        cwd="/workspace",
    )

    assert docker.calls[0][:2] == ["sh", "-c"]
