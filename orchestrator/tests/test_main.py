from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.main import _handle_run_failure


@pytest.mark.anyio
async def test_handle_run_failure_removes_containers_and_persists_failure_state():
    removed: list[str] = []
    updates_called: list[tuple[dict[str, object], str | None]] = []

    class FakeGraph:
        async def aget_state(self, config):
            del config
            return SimpleNamespace(
                values={
                    "run_id": "run-1",
                    "run_branch": "autogen/run-1",
                    "active_containers": {"stage_dev": "container-1"},
                    "active_workspaces": {"stage_dev": "stage-001-dev"},
                }
            )

        async def aupdate_state(self, config, values, as_node=None):
            del config
            updates_called.append((values, as_node))

    class FakeDockerManager:
        async def remove_container(self, container_id: str, force: bool = True) -> None:
            del force
            removed.append(container_id)

    app = SimpleNamespace(docker_manager=FakeDockerManager())
    result = await _handle_run_failure(
        graph=FakeGraph(),
        app=app,
        config={"configurable": {"thread_id": "thread-1"}},
        error=RuntimeError("boom"),
    )

    assert removed == ["container-1"]
    assert result["run_status"] == "FAILED"
    assert result["active_containers"] == {}
    assert result["active_workspaces"] == {"stage_dev": "stage-001-dev"}
    assert updates_called
    assert updates_called[0][1] == "end_failure"
