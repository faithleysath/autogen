from __future__ import annotations

from orchestrator.models.runtime import VISIBLE_ROOT, WorkspaceView
from orchestrator.services.artifact_service import ArtifactService


def test_artifact_round_trip(tmp_path):
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="planning",
    )
    service = ArtifactService()
    service.write_artifact(
        workspace,
        "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
        {"kind": "execution_contract", "run_id": "run-1"},
        "# Execution Contract\n",
    )

    document = service.read_artifact(
        workspace,
        "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
    )
    assert document.meta["kind"] == "execution_contract"
    assert "Execution Contract" in document.body
