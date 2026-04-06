from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import docker
from docker.errors import NotFound

from orchestrator.config import OrchestratorConfig
from orchestrator.models.runtime import ContainerHandle, ExecResult, WorkspaceView

logger = logging.getLogger(__name__)


class DockerManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config
        self._client = docker.DockerClient(base_url=config.docker_host)

    async def create_container(
        self,
        *,
        image: str,
        name: str,
        workspace_view: WorkspaceView,
        role: str,
        env: dict[str, str] | None = None,
        command: list[str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> ContainerHandle:
        environment = {
            "HOST_UID": str(self._config.host_uid),
            "HOST_GID": str(self._config.host_gid),
            "AUTOGEN_GIT_USER_NAME": self._config.git_user_name,
            "AUTOGEN_GIT_USER_EMAIL": self._config.git_user_email,
            **(env or {}),
        }
        volumes: dict[str, dict[str, str]] = {
            str(workspace_view.backing_root): {"bind": "/workspace", "mode": "rw"},
        }
        if self._config.ssh_dir:
            volumes[str(self._config.ssh_dir)] = {"bind": "/run/host-ssh", "mode": "ro"}
            environment["SSH_SOURCE_DIR"] = "/run/host-ssh"

        container = await asyncio.to_thread(
            self._client.containers.run,
            image,
            command or ["sleep", "infinity"],
            detach=True,
            name=name,
            tty=True,
            stdin_open=True,
            working_dir="/workspace",
            environment=environment,
            volumes=volumes,
            labels=labels or {},
            auto_remove=False,
        )
        logger.info(
            "container_created",
            extra={
                "container_id": container.id,
                "container_name": name,
                "image": image,
                "role": role,
                "workspace_id": workspace_view.workspace_id,
            },
        )
        return ContainerHandle(
            container_id=container.id,
            name=name,
            image=image,
            role=role,
            workspace_id=workspace_view.workspace_id,
        )

    async def exec(
        self,
        *,
        container_id: str,
        cmd: list[str] | str,
        cwd: str = "/workspace",
        timeout_seconds: int = 120,
    ) -> ExecResult:
        container = await asyncio.to_thread(self._client.containers.get, container_id)
        logger.info(
            "container_exec_start",
            extra={"container_id": container_id, "cmd": cmd, "cwd": cwd},
        )

        def _run_exec() -> ExecResult:
            exit_code, output = container.exec_run(
                cmd,
                workdir=cwd,
                demux=True,
                tty=False,
                user="autogen",
                environment={"HOME": "/home/autogen"},
                stdout=True,
                stderr=True,
            )
            stdout_bytes, stderr_bytes = output
            return ExecResult(
                cmd=cmd if isinstance(cmd, list) else [cmd],
                cwd=cwd,
                exit_code=exit_code,
                stdout=(stdout_bytes or b"").decode("utf-8", errors="replace"),
                stderr=(stderr_bytes or b"").decode("utf-8", errors="replace"),
            )

        result = await asyncio.wait_for(asyncio.to_thread(_run_exec), timeout=timeout_seconds)
        logger.info(
            "container_exec_done",
            extra={
                "container_id": container_id,
                "cmd": cmd,
                "cwd": cwd,
                "exit_code": result.exit_code,
            },
        )
        return result

    async def remove_container(self, container_id: str, force: bool = True) -> None:
        try:
            container = await asyncio.to_thread(self._client.containers.get, container_id)
        except NotFound:
            return
        await asyncio.to_thread(container.remove, force=force)
        logger.info("container_removed", extra={"container_id": container_id, "force": force})
