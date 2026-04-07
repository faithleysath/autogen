from __future__ import annotations

from types import SimpleNamespace

import pytest

from orchestrator.models.runtime import ExecResult, VISIBLE_ROOT, WorkspaceView
from orchestrator.policy.role_policy import build_role_policy
from orchestrator.tools.base import ToolContext
from orchestrator.tools.bash_tool import BashToolset


class FakeDockerManager:
    def __init__(self, mutator=None):
        self._mutator = mutator
        self.calls: list[tuple[list[str], str]] = []

    async def exec(self, *, container_id: str, cmd: list[str], cwd: str, timeout_seconds: int) -> ExecResult:
        del container_id, timeout_seconds
        self.calls.append((cmd, cwd))
        if self._mutator is not None:
            self._mutator(cmd, cwd)
        return ExecResult(cmd=cmd, cwd=cwd, exit_code=0, stdout="", stderr="")


class FakeGitService:
    def __init__(self, changed_paths_responses: list[list[str]]):
        self._responses = list(changed_paths_responses)
        self.reverted_paths: list[str] = []

    async def changed_paths(self, *, container_id: str, workspace) -> list[str]:
        del container_id, workspace
        if not self._responses:
            return []
        return self._responses.pop(0)

    async def best_effort_revert_paths(self, *, container_id: str, workspace, paths: list[str]) -> None:
        del container_id, workspace
        self.reverted_paths.extend(paths)


def _make_context(tmp_path, *, role: str) -> ToolContext:
    workspace = WorkspaceView(
        workspace_id="ws",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="stage-dev",
    )
    policy_kwargs = {"role": role, "run_id": "run-1", "cycle_no": 1}
    if role == "stage_gate":
        policy_kwargs.update({"stage_no": 1, "attempt_no": 1})
    if role in {"compliance", "qa", "e2e", "release_gate"}:
        policy_kwargs.update({"release_no": 1})
    policy = build_role_policy(**policy_kwargs)
    return ToolContext(
        role=role,
        workspace=workspace,
        container_id="container-1",
        policy=policy,
    )


@pytest.mark.anyio
async def test_run_command_rejects_paths_outside_workspace(tmp_path):
    context = _make_context(tmp_path, role="stage_gate")
    tool = BashToolset(
        context,
        FakeDockerManager(),
        FakeGitService([[], []]),
        SimpleNamespace(review_timeout_seconds=60),
    )

    with pytest.raises(PermissionError):
        await tool.run_command(
            {
                "argv": ["sed", "/etc/hosts"],
                "cwd": "/workspace",
                "timeout_seconds": 10,
            }
        )


@pytest.mark.anyio
async def test_run_command_restores_disallowed_workspace_mutations(tmp_path):
    context = _make_context(tmp_path, role="stage_gate")
    target = tmp_path / "src" / "app.ts"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("before\n", encoding="utf-8")

    def mutator(cmd: list[str], cwd: str) -> None:
        del cmd, cwd
        target.write_text("after\n", encoding="utf-8")

    tool = BashToolset(
        context,
        FakeDockerManager(mutator=mutator),
        FakeGitService([["src/app.ts"], ["src/app.ts"]]),
        SimpleNamespace(review_timeout_seconds=60),
    )

    with pytest.raises(PermissionError):
        await tool.run_command(
            {
                "argv": ["pytest"],
                "cwd": "/workspace",
                "timeout_seconds": 10,
            }
        )

    assert target.read_text(encoding="utf-8") == "before\n"


@pytest.mark.anyio
async def test_run_command_allows_date_for_read_only_time_lookup(tmp_path):
    context = _make_context(tmp_path, role="architect")
    docker = FakeDockerManager()
    tool = BashToolset(
        context,
        docker,
        FakeGitService([[], []]),
        SimpleNamespace(review_timeout_seconds=60),
    )

    result = await tool.run_command(
        {
            "argv": ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"],
            "cwd": "/workspace",
            "timeout_seconds": 10,
        }
    )

    assert result["argv"] == ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"]
    assert docker.calls == [(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], "/workspace")]


@pytest.mark.anyio
async def test_e2e_run_command_allows_external_playwright_cache_inspection(tmp_path):
    context = _make_context(tmp_path, role="e2e")
    docker = FakeDockerManager()
    tool = BashToolset(
        context,
        docker,
        FakeGitService([[], []]),
        SimpleNamespace(review_timeout_seconds=60),
    )

    await tool.run_command(
        {
            "argv": ["ls", "/ms-playwright"],
            "cwd": "/workspace",
            "timeout_seconds": 10,
        }
    )

    assert docker.calls == [(["ls", "/ms-playwright"], "/workspace")]


@pytest.mark.anyio
async def test_stage_gate_run_command_tolerates_and_cleans_tsbuildinfo_side_effects(tmp_path):
    context = _make_context(tmp_path, role="stage_gate")

    def mutator(cmd: list[str], cwd: str) -> None:
        del cmd, cwd
        target = tmp_path / "tsconfig.tsbuildinfo"
        target.write_text("{}", encoding="utf-8")

    git = FakeGitService([[], ["tsconfig.tsbuildinfo"]])
    tool = BashToolset(
        context,
        FakeDockerManager(mutator=mutator),
        git,
        SimpleNamespace(review_timeout_seconds=60),
    )

    result = await tool.run_command(
        {
            "argv": ["bun", "run", "build"],
            "cwd": "/workspace",
            "timeout_seconds": 10,
        }
    )

    assert result["exit_code"] == 0
    assert git.reverted_paths == ["tsconfig.tsbuildinfo"]


@pytest.mark.anyio
async def test_e2e_run_command_tolerates_node_modules_side_effects(tmp_path):
    context = _make_context(tmp_path, role="e2e")

    def mutator(cmd: list[str], cwd: str) -> None:
        del cmd, cwd
        target = tmp_path / "node_modules" / ".bin" / "vite"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("vite", encoding="utf-8")

    git = FakeGitService([[], ["node_modules/.bin/vite"]])
    tool = BashToolset(
        context,
        FakeDockerManager(mutator=mutator),
        git,
        SimpleNamespace(review_timeout_seconds=60),
    )

    result = await tool.run_command(
        {
            "argv": ["bun", "install"],
            "cwd": "/workspace",
            "timeout_seconds": 10,
        }
    )

    assert result["exit_code"] == 0
    assert git.reverted_paths == []
