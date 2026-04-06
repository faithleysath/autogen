from __future__ import annotations

import pytest

from orchestrator.models.runtime import VISIBLE_ROOT, WorkspaceView
from orchestrator.policy.role_policy import build_role_policy
from orchestrator.tools.base import ToolContext
from orchestrator.tools.file_tools import FileToolset


def _make_toolset(tmp_path) -> FileToolset:
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="planning",
    )
    context = ToolContext(
        role="architect",
        workspace=workspace,
        container_id="container-1",
        policy=build_role_policy(role="architect", run_id="run-1", cycle_no=1),
    )
    return FileToolset(context, include_write_tools=True)


@pytest.mark.anyio
async def test_list_files_hides_internal_dirs_during_repo_survey(tmp_path):
    toolset = _make_toolset(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".autogen" / "runs").mkdir(parents=True)
    (tmp_path / ".autogen" / "runs" / "note.md").write_text("internal\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("export const app = true;\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("node_modules\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Sample\n", encoding="utf-8")

    result = await toolset.list_files({"path": "/workspace", "max_entries": 50})

    assert "/workspace/README.md" in result["entries"]
    assert "/workspace/.gitignore" in result["entries"]
    assert "/workspace/src" in result["entries"]
    assert "/workspace/src/app.ts" in result["entries"]
    assert not any(path.startswith("/workspace/.git/") for path in result["entries"])
    assert not any(path.startswith("/workspace/.autogen/") for path in result["entries"])


@pytest.mark.anyio
async def test_list_files_allows_explicit_internal_paths(tmp_path):
    toolset = _make_toolset(tmp_path)
    internal_root = tmp_path / ".autogen" / "runs" / "run-1" / "00-input"
    internal_root.mkdir(parents=True)
    (internal_root / "prd.md").write_text("# PRD\n", encoding="utf-8")

    result = await toolset.list_files({"path": "/workspace/.autogen", "max_entries": 50})

    assert "/workspace/.autogen/runs" in result["entries"]
    assert "/workspace/.autogen/runs/run-1/00-input/prd.md" in result["entries"]


@pytest.mark.anyio
async def test_search_text_skips_internal_dirs_unless_explicitly_targeted(tmp_path):
    toolset = _make_toolset(tmp_path)
    (tmp_path / ".autogen" / "runs").mkdir(parents=True)
    (tmp_path / ".autogen" / "runs" / "plan.md").write_text("todo marker\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("todo marker\n", encoding="utf-8")

    survey_result = await toolset.search_text(
        {"path": "/workspace", "query": "todo marker", "max_results": 20}
    )
    explicit_result = await toolset.search_text(
        {"path": "/workspace/.autogen", "query": "todo marker", "max_results": 20}
    )

    assert survey_result["results"] == [
        {
            "path": "/workspace/README.md",
            "line": 1,
            "content": "todo marker",
        }
    ]
    assert explicit_result["results"] == [
        {
            "path": "/workspace/.autogen/runs/plan.md",
            "line": 1,
            "content": "todo marker",
        }
    ]
