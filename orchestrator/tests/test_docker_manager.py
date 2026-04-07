from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.services.docker_manager import DockerManager, _EXEC_ENV


class FakeContainer:
    def __init__(self) -> None:
        self.exec_calls: list[dict[str, object]] = []

    def exec_run(
        self,
        cmd,
        *,
        workdir,
        demux,
        tty,
        user,
        environment,
        stdout,
        stderr,
    ):
        self.exec_calls.append(
            {
                "cmd": cmd,
                "workdir": workdir,
                "demux": demux,
                "tty": tty,
                "user": user,
                "environment": environment,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        return 0, (b"ok\n", b"")


class FakeContainers:
    def __init__(self, container: FakeContainer) -> None:
        self._container = container
        self.requested_ids: list[str] = []

    def get(self, container_id: str) -> FakeContainer:
        self.requested_ids.append(container_id)
        return self._container


class FakeClient:
    def __init__(self, container: FakeContainer) -> None:
        self.containers = FakeContainers(container)


@pytest.mark.anyio
async def test_exec_preserves_bun_and_playwright_environment(monkeypatch):
    container = FakeContainer()
    fake_client = FakeClient(container)

    monkeypatch.setattr(
        "orchestrator.services.docker_manager.docker.DockerClient",
        lambda base_url: fake_client,
    )

    manager = DockerManager(
        SimpleNamespace(
            docker_host="unix:///var/run/docker.sock",
            container_start_timeout_seconds=60,
        )
    )

    result = await manager.exec(
        container_id="container-1",
        cmd=["sh", "-lc", "echo ok"],
        cwd="/workspace",
        timeout_seconds=30,
    )

    assert result.stdout == "ok\n"
    assert fake_client.containers.requested_ids == ["container-1"]
    assert container.exec_calls[0]["environment"] == _EXEC_ENV
    assert container.exec_calls[0]["user"] == "autogen"
    assert _EXEC_ENV["BUN_INSTALL_CACHE_DIR"] == "/autogen-state/tasks/tmp/bun-cache"
    assert _EXEC_ENV["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"
    assert _EXEC_ENV["TMPDIR"] == "/autogen-state/tasks/tmp"
