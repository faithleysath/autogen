from __future__ import annotations

import json

import pytest

from orchestrator.models.runtime import VISIBLE_ROOT, WorkspaceView
from orchestrator.policy.role_policy import build_role_policy
from orchestrator.services.artifact_service import ArtifactService
from orchestrator.tools.artifact_tools import ArtifactToolset
from orchestrator.tools.base import ToolContext


def _make_toolset(
    tmp_path,
    *,
    role: str = "architect",
    workspace_kind: str = "planning",
    stage_no: int | None = None,
    attempt_no: int | None = None,
) -> ArtifactToolset:
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind=workspace_kind,
    )
    context = ToolContext(
        role=role,
        workspace=workspace,
        container_id="container-1",
        policy=build_role_policy(
            role=role,
            run_id="run-1",
            cycle_no=1,
            stage_no=stage_no,
            attempt_no=attempt_no,
        ),
    )
    return ArtifactToolset(context, ArtifactService())


@pytest.mark.anyio
async def test_write_markdown_artifact_adds_default_control_frontmatter(tmp_path):
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="planning",
    )
    toolset = _make_toolset(tmp_path)

    result = await toolset.write_markdown_artifact(
        {
            "path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
            "frontmatter": {"kind": "execution_contract"},
            "body": "body\n",
        }
    )

    assert result["path"].endswith("execution-contract.md")
    artifact = ArtifactService().read_artifact(
        workspace,
        "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
    )
    assert artifact.meta["kind"] == "execution_contract"
    assert artifact.meta["run_id"] == "run-1"
    assert artifact.meta["role"] == "architect"
    assert artifact.meta["created_at"].endswith("Z")


@pytest.mark.anyio
async def test_write_markdown_artifact_accepts_full_markdown_body(tmp_path):
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="planning",
    )
    toolset = _make_toolset(tmp_path, role="stage_gate", stage_no=1, attempt_no=1)

    result = await toolset.write_markdown_artifact(
        {
            "path": "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md",
            "body": (
                "---\n"
                "kind: gate_decision\n"
                "stage_id: stage-001\n"
                "attempt_no: 1\n"
                "decision: NEXT_STAGE\n"
                "---\n\n"
                "Stage 1 passed.\n"
            ),
        }
    )

    assert result["path"].endswith("gate-decision.md")
    artifact = ArtifactService().read_artifact(
        workspace,
        "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md",
    )
    assert artifact.meta["kind"] == "gate_decision"
    assert artifact.meta["stage_id"] == "stage-001"
    assert artifact.meta["attempt_no"] == 1
    assert artifact.meta["decision"] == "NEXT_STAGE"
    assert artifact.meta["run_id"] == "run-1"
    assert artifact.meta["role"] == "stage_gate"
    assert artifact.body == "Stage 1 passed.\n"


@pytest.mark.anyio
async def test_write_markdown_artifact_accepts_json_wrapped_body(tmp_path):
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="planning",
    )
    toolset = _make_toolset(tmp_path, role="stage_gate", stage_no=1, attempt_no=1)

    result = await toolset.write_markdown_artifact(
        {
            "path": "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md",
            "body": json.dumps(
                {
                    "frontmatter": {
                        "kind": "gate_decision",
                        "stage_id": "stage-001",
                        "attempt_no": 1,
                        "decision": "NEXT_STAGE",
                    },
                    "body": "Stage 1 passed.\n",
                }
            ),
        }
    )

    assert result["path"].endswith("gate-decision.md")
    artifact = ArtifactService().read_artifact(
        workspace,
        "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md",
    )
    assert artifact.meta["kind"] == "gate_decision"
    assert artifact.meta["stage_id"] == "stage-001"
    assert artifact.meta["attempt_no"] == 1
    assert artifact.meta["decision"] == "NEXT_STAGE"
    assert artifact.meta["run_id"] == "run-1"
    assert artifact.meta["role"] == "stage_gate"
    assert artifact.body == "Stage 1 passed.\n"
