from __future__ import annotations

import secrets
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docker.errors import NotFound

from orchestrator.models.runtime import ExecResult, WorkspaceView
from orchestrator.services.docker_manager import DockerManager


DEFAULT_CONTAINER_TASK_ROOT = "/autogen-state/tasks"


def _now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class BackgroundTaskRecord:
    task_id: str
    run_id: str
    workspace_id: str
    container_id: str
    role: str
    description: str
    argv: list[str]
    cwd: str
    pid: int
    host_log_path: Path
    host_status_path: Path
    container_log_path: str
    container_status_path: str
    created_at: str
    stop_requested: bool = False


class BackgroundTaskManager:
    def __init__(
        self,
        *,
        docker_manager: DockerManager,
        tasks_root: Path,
        container_task_root: str = DEFAULT_CONTAINER_TASK_ROOT,
    ) -> None:
        self._docker = docker_manager
        self._tasks_root = tasks_root
        self._container_task_root = container_task_root.rstrip("/")
        self._records: dict[str, BackgroundTaskRecord] = {}
        self._tasks_root.mkdir(parents=True, exist_ok=True)

    async def create_command_task(
        self,
        *,
        workspace: WorkspaceView,
        container_id: str,
        role: str,
        description: str,
        argv: list[str],
        cwd: str,
    ) -> dict[str, Any]:
        task_id = f"task-{secrets.token_hex(4)}"
        host_root = self._tasks_root / workspace.run_id / workspace.workspace_id
        host_root.mkdir(parents=True, exist_ok=True)
        host_log_path = host_root / f"{task_id}.log"
        host_status_path = host_root / f"{task_id}.status"
        host_log_path.write_text("", encoding="utf-8")
        if host_status_path.exists():
            host_status_path.unlink()

        container_root = f"{self._container_task_root}/{workspace.run_id}/{workspace.workspace_id}"
        container_log_path = f"{container_root}/{task_id}.log"
        container_status_path = f"{container_root}/{task_id}.status"
        script = self._build_start_script(
            argv=argv,
            cwd=cwd,
            container_root=container_root,
            container_log_path=container_log_path,
            container_status_path=container_status_path,
        )
        result = await self._docker.exec(
            container_id=container_id,
            cmd=["sh", "-lc", script],
            cwd="/workspace",
            timeout_seconds=30,
        )
        result.raise_for_error("start background task")
        pid_text = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        pid = int(pid_text)
        record = BackgroundTaskRecord(
            task_id=task_id,
            run_id=workspace.run_id,
            workspace_id=workspace.workspace_id,
            container_id=container_id,
            role=role,
            description=description,
            argv=list(argv),
            cwd=cwd,
            pid=pid,
            host_log_path=host_log_path,
            host_status_path=host_status_path,
            container_log_path=container_log_path,
            container_status_path=container_status_path,
            created_at=_now_utc(),
        )
        self._records[task_id] = record
        return await self.get_task(task_id=task_id)

    async def get_task(self, *, task_id: str) -> dict[str, Any]:
        record = self._require_task(task_id)
        status, exit_code = await self._inspect_status(record)
        return self._serialize_record(record, status=status, exit_code=exit_code)

    async def list_tasks(
        self,
        *,
        run_id: str,
        workspace_id: str,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for task_id, record in sorted(self._records.items()):
            if record.run_id != run_id or record.workspace_id != workspace_id:
                continue
            status, exit_code = await self._inspect_status(record)
            results.append(self._serialize_record(record, status=status, exit_code=exit_code))
        return results

    def read_output(self, *, task_id: str, max_bytes: int = 12000) -> dict[str, Any]:
        record = self._require_task(task_id)
        if record.host_log_path.exists():
            content = record.host_log_path.read_text(encoding="utf-8", errors="replace")
        else:
            content = ""
        if len(content) > max_bytes:
            content = content[-max_bytes:]
        return {
            "task_id": task_id,
            "output": content,
            "truncated": record.host_log_path.exists() and record.host_log_path.stat().st_size > max_bytes,
        }

    async def stop_task(self, *, task_id: str) -> dict[str, Any]:
        record = self._require_task(task_id)
        record.stop_requested = True
        try:
            await self._docker.exec(
                container_id=record.container_id,
                cmd=["sh", "-lc", self._build_stop_script(record.pid)],
                cwd="/workspace",
                timeout_seconds=15,
            )
        except NotFound:
            pass
        if not record.host_status_path.exists():
            record.host_status_path.write_text("-15\n", encoding="utf-8")
        return await self.get_task(task_id=task_id)

    def owns_task(self, *, task_id: str, run_id: str, workspace_id: str) -> bool:
        record = self._records.get(task_id)
        if record is None:
            return False
        return record.run_id == run_id and record.workspace_id == workspace_id

    def _require_task(self, task_id: str) -> BackgroundTaskRecord:
        record = self._records.get(task_id)
        if record is None:
            raise ValueError(f"unknown task_id: {task_id}")
        return record

    def _serialize_record(
        self,
        record: BackgroundTaskRecord,
        *,
        status: str,
        exit_code: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": record.task_id,
            "status": status,
            "description": record.description,
            "role": record.role,
            "argv": list(record.argv),
            "cwd": record.cwd,
            "pid": record.pid,
            "created_at": record.created_at,
        }
        if exit_code is not None:
            payload["exit_code"] = exit_code
        return payload

    async def _inspect_status(self, record: BackgroundTaskRecord) -> tuple[str, int | None]:
        if record.host_status_path.exists():
            try:
                exit_code = int(record.host_status_path.read_text(encoding="utf-8").strip())
            except ValueError:
                exit_code = None
            if record.stop_requested:
                return "stopped", -15
            if exit_code == 0:
                return "completed", exit_code
            return "failed", exit_code

        if record.stop_requested:
            return "stopped", -15

        alive = await self._is_process_alive(record)
        if alive:
            return "running", None
        return "lost", None

    async def _is_process_alive(self, record: BackgroundTaskRecord) -> bool:
        try:
            result = await self._docker.exec(
                container_id=record.container_id,
                cmd=["sh", "-lc", f"kill -0 {record.pid} >/dev/null 2>&1"],
                cwd="/workspace",
                timeout_seconds=10,
            )
        except NotFound:
            return False
        return result.exit_code == 0

    def _build_start_script(
        self,
        *,
        argv: list[str],
        cwd: str,
        container_root: str,
        container_log_path: str,
        container_status_path: str,
    ) -> str:
        return (
            f"mkdir -p {shlex.quote(container_root)} && "
            f"rm -f {shlex.quote(container_status_path)} && "
            f"( cd {shlex.quote(cwd)} && {shlex.join(argv)}; "
            f"printf '%s\\n' \"$?\" > {shlex.quote(container_status_path)} ) "
            f"> {shlex.quote(container_log_path)} 2>&1 & "
            "echo $!"
        )

    def _build_stop_script(self, pid: int) -> str:
        return (
            f"pgid=$(ps -o pgid= -p {pid} 2>/dev/null | tr -d ' '); "
            'if [ -n "$pgid" ]; then kill -TERM -- "-$pgid" >/dev/null 2>&1 || true; fi; '
            f"kill -TERM {pid} >/dev/null 2>&1 || true"
        )
