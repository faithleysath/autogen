from __future__ import annotations

import pytest

from orchestrator.models.runtime import VISIBLE_ROOT, WorkspaceView


def test_workspace_path_mapping(tmp_path):
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="stage-dev",
    )
    backing = workspace.to_backing_path("/workspace/src/app.ts")
    assert backing == tmp_path / "src" / "app.ts"
    assert workspace.to_visible_path(backing) == "/workspace/src/app.ts"


def test_workspace_rejects_escape(tmp_path):
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="stage-dev",
    )
    with pytest.raises(ValueError):
        workspace.to_backing_path("/tmp/outside.txt")
