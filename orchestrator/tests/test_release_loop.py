from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.graph.nodes.release_loop import join_review_results, reset_for_replan


@pytest.mark.anyio
async def test_join_review_results_rejects_candidate_sha_mismatch():
    state = {
        "candidate_code_sha": "abc",
        "review_results": {
            "compliance": {"candidate_code_sha": "abc"},
            "qa": {"candidate_code_sha": "def"},
            "e2e": {"candidate_code_sha": "abc"},
        },
    }
    with pytest.raises(RuntimeError, match="candidate_code_sha"):
        await join_review_results(state, app=None)


@pytest.mark.anyio
async def test_reset_for_replan_clears_stale_release_and_gate_state():
    removed_containers: list[str] = []
    removed_workspaces: list[tuple[str, str]] = []

    class FakeDockerManager:
        async def remove_container(self, container_id: str, force: bool = True) -> None:
            del force
            removed_containers.append(container_id)

    class FakeWorkspaceManager:
        def remove_workspace(self, run_id: str, workspace_id: str) -> None:
            removed_workspaces.append((run_id, workspace_id))

    app = SimpleNamespace(
        docker_manager=FakeDockerManager(),
        workspace_manager=FakeWorkspaceManager(),
    )
    state = {
        "run_id": "run-1",
        "cycle_no": 1,
        "active_workspaces": {"publisher": "release-001-publisher"},
        "active_containers": {"publisher": "container-1"},
        "current_stage_gate_path": "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md",
        "current_gate_decision": "FAIL",
        "candidate_code_sha": "abc",
        "review_results": {"compliance": {"candidate_code_sha": "abc"}},
        "release_decision": "REWORK",
        "release_decision_path": "/workspace/.autogen/runs/run-1/40-release/release-001/decision.md",
    }

    updates = await reset_for_replan(state, app)

    assert removed_containers == ["container-1"]
    assert removed_workspaces == [("run-1", "release-001-publisher")]
    assert updates["current_stage_gate_path"] is None
    assert updates["current_gate_decision"] is None
    assert updates["candidate_code_sha"] is None
    assert updates["review_results"] == {}
    assert updates["release_decision"] is None
    assert updates["release_decision_path"] is None
