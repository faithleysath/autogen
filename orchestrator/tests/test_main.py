from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.main import (
    _handle_run_failure,
    _restore_active_containers,
    _select_resume_snapshot,
)


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


@pytest.mark.anyio
async def test_select_resume_snapshot_uses_prior_checkpoint_after_failed_terminal_state():
    resumable = SimpleNamespace(
        values={"run_status": "REVIEWING", "run_id": "run-1"},
        next=("run_compliance_review",),
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "cp-resume"}},
    )
    failed = SimpleNamespace(
        values={"run_status": "FAILED", "run_id": "run-1"},
        next=(),
        config={"configurable": {"thread_id": "thread-1", "checkpoint_id": "cp-failed"}},
    )

    class FakeGraph:
        async def aget_state(self, config):
            del config
            return failed

        async def aget_state_history(self, config):
            del config
            for item in (failed, resumable):
                yield item

    snapshot = await _select_resume_snapshot(
        FakeGraph(),
        {"configurable": {"thread_id": "thread-1"}},
    )

    assert snapshot is resumable


@pytest.mark.anyio
async def test_restore_active_containers_reuses_existing_and_recreates_missing(monkeypatch):
    created: list[tuple[str, str, str]] = []

    class FakeDockerManager:
        async def resolve_container_id(self, name_or_id: str) -> str | None:
            if name_or_id == "existing-container":
                return "existing-container"
            return None

        async def create_container(self, *, image, name, workspace_view, role, labels):
            del labels
            created.append((image, name, role))
            return SimpleNamespace(container_id=f"new-{workspace_view.workspace_id}")

    class FakeWorkspaceManager:
        def load_workspace(self, *, run_id: str, workspace_id: str):
            return SimpleNamespace(run_id=run_id, workspace_id=workspace_id)

    app = SimpleNamespace(
        config=SimpleNamespace(dev_image="dev-image", e2e_image="e2e-image"),
        docker_manager=FakeDockerManager(),
        workspace_manager=FakeWorkspaceManager(),
        container_name=lambda run_id, workspace_id: f"autogen-{run_id}-{workspace_id}",
    )

    restored = await _restore_active_containers(
        app,
        {
            "run_id": "run-1",
            "cycle_no": 1,
            "stage_no": 2,
            "release_no": 3,
            "active_workspaces": {
                "planning": "cycle-001-planning",
                "e2e": "release-003-e2e",
            },
            "active_containers": {
                "planning": "existing-container",
                "e2e": "missing-container",
            },
        },
    )

    assert restored == {
        "planning": "existing-container",
        "e2e": "new-release-003-e2e",
    }
    assert created == [
        (
            "e2e-image",
            "autogen-run-1-release-003-e2e",
            "e2e",
        )
    ]
