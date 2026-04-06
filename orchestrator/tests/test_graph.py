from __future__ import annotations

from orchestrator.app import build_app
from orchestrator.graph.builder import build_graph


def test_graph_builds_with_sqlite_checkpointer(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTOGEN_WORKSPACE_ROOT", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AUTOGEN_SQLITE_PATH", str(tmp_path / "state" / "orchestrator.sqlite"))
    app = build_app()
    graph = build_graph(app)
    assert graph is not None
