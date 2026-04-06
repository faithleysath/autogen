from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from orchestrator.config import OrchestratorConfig
from orchestrator.models.runtime import remove_path
from orchestrator.services.docker_manager import DockerManager
from orchestrator.services.git_service import GitService
from orchestrator.tools.base import ToolContext, ToolSpec


READ_ONLY_GIT_SUBCOMMANDS = {
    "branch",
    "check-ignore",
    "diff",
    "grep",
    "log",
    "ls-files",
    "rev-parse",
    "show",
    "status",
}
ALLOWED_PRIMARY_COMMANDS = {
    "bun",
    "bunx",
    "date",
    "find",
    "head",
    "ls",
    "node",
    "npm",
    "npx",
    "playwright",
    "pnpm",
    "pwd",
    "py.test",
    "pytest",
    "python",
    "python3",
    "rg",
    "sed",
    "tail",
    "wc",
    "yarn",
    "git",
}
DISALLOWED_INLINE_FLAGS = {"-c", "-e", "--eval"}


@dataclass(frozen=True, slots=True)
class PathSnapshot:
    exists: bool
    is_symlink: bool
    content: bytes | None = None
    symlink_target: str | None = None


def _validate_command(argv: list[str]) -> None:
    if not argv:
        raise ValueError("argv must not be empty")
    primary = argv[0]
    if primary not in ALLOWED_PRIMARY_COMMANDS:
        raise PermissionError(f"command is not allowed: {primary}")
    if primary == "git":
        if len(argv) < 2 or argv[1] not in READ_ONLY_GIT_SUBCOMMANDS:
            raise PermissionError("git command is restricted to read-only subcommands")
    if primary in {"python", "python3", "node"} and any(flag in DISALLOWED_INLINE_FLAGS for flag in argv[1:]):
        raise PermissionError(f"{primary} inline evaluation flags are not allowed")


def _looks_like_path_token(token: str) -> bool:
    return token in {".", ".."} or token.startswith(("/", "./", "../"))


def _iter_path_candidates(argv: list[str]) -> list[str]:
    candidates: list[str] = []
    for token in argv[1:]:
        if _looks_like_path_token(token):
            candidates.append(token)
            continue
        if "=" not in token:
            continue
        _, value = token.split("=", 1)
        if _looks_like_path_token(value):
            candidates.append(value)
    return candidates


def _resolve_visible_path(candidate: str, cwd: str) -> str:
    candidate_path = PurePosixPath(candidate)
    if candidate_path.is_absolute():
        return str(candidate_path)
    return str(PurePosixPath(cwd).joinpath(candidate_path))


class BashToolset:
    def __init__(
        self,
        context: ToolContext,
        docker_manager: DockerManager,
        git_service: GitService,
        config: OrchestratorConfig,
    ) -> None:
        self._context = context
        self._docker = docker_manager
        self._git = git_service
        self._config = config

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="run_command",
                description=(
                    "Run an allowed non-shell command inside the role container. "
                    "Pass argv as an array of strings; shell metacharacters are not available. "
                    "Allowed programs are restricted, and git only supports read-only subcommands "
                    "such as status, diff, log, show, and rev-parse."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "cwd": {"type": "string"},
                        "timeout_seconds": {"type": "integer"},
                    },
                    "required": ["argv", "cwd", "timeout_seconds"],
                    "additionalProperties": False,
                },
                handler=self.run_command,
            )
        ]

    async def run_command(self, args: dict[str, Any]) -> dict[str, Any]:
        if not self._context.policy.allow_command:
            raise PermissionError(f"{self._context.role} cannot run commands")
        argv = [str(item) for item in args["argv"]]
        _validate_command(argv)
        cwd = str(args["cwd"])
        try:
            self._context.workspace.ensure_within_workspace(cwd)
        except ValueError as exc:
            raise PermissionError(str(exc)) from exc
        for candidate in _iter_path_candidates(argv):
            try:
                self._context.workspace.ensure_within_workspace(_resolve_visible_path(candidate, cwd))
            except ValueError as exc:
                raise PermissionError(str(exc)) from exc

        before_dirty: set[str] = set()
        before_snapshots: dict[str, PathSnapshot] = {}
        if not self._context.policy.allow_code_write:
            before_dirty, before_snapshots = await self._capture_protected_state()

        timeout_seconds = min(
            int(args["timeout_seconds"]),
            self._config.review_timeout_seconds,
        )
        result = await self._docker.exec(
            container_id=self._context.container_id,
            cmd=argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
        )
        if not self._context.policy.allow_code_write:
            violations = await self._restore_disallowed_side_effects(before_dirty, before_snapshots)
            if violations:
                changed = ", ".join(f"/workspace/{path}" for path in violations[:10])
                raise PermissionError(
                    f"{self._context.role} command modified disallowed paths: {changed}"
                )
        return {
            "argv": argv,
            "cwd": cwd,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-12000:],
        }

    def _repo_path_to_host_path(self, repo_path: str) -> Path:
        return self._context.workspace.backing_root / PurePosixPath(repo_path).as_posix()

    def _snapshot_path(self, repo_path: str) -> PathSnapshot:
        host_path = self._repo_path_to_host_path(repo_path)
        exists = host_path.exists() or host_path.is_symlink()
        if not exists:
            return PathSnapshot(exists=False, is_symlink=False)
        if host_path.is_symlink():
            return PathSnapshot(
                exists=True,
                is_symlink=True,
                symlink_target=os.readlink(host_path),
            )
        return PathSnapshot(
            exists=True,
            is_symlink=False,
            content=host_path.read_bytes(),
        )

    def _restore_snapshot(self, repo_path: str, snapshot: PathSnapshot) -> None:
        host_path = self._repo_path_to_host_path(repo_path)
        if not snapshot.exists:
            remove_path(host_path)
            return
        host_path.parent.mkdir(parents=True, exist_ok=True)
        remove_path(host_path)
        if snapshot.is_symlink:
            assert snapshot.symlink_target is not None
            host_path.symlink_to(snapshot.symlink_target)
            return
        host_path.write_bytes(snapshot.content or b"")

    async def _capture_protected_state(self) -> tuple[set[str], dict[str, PathSnapshot]]:
        changed_paths = set(
            await self._git.changed_paths(
                container_id=self._context.container_id,
                workspace=self._context.workspace,
            )
        )
        snapshots: dict[str, PathSnapshot] = {}
        for repo_path in changed_paths:
            visible_path = f"/workspace/{repo_path}"
            if self._context.policy.can_write(visible_path):
                continue
            snapshots[repo_path] = self._snapshot_path(repo_path)
        return changed_paths, snapshots

    async def _restore_disallowed_side_effects(
        self,
        before_dirty: set[str],
        before_snapshots: dict[str, PathSnapshot],
    ) -> list[str]:
        after_dirty = set(
            await self._git.changed_paths(
                container_id=self._context.container_id,
                workspace=self._context.workspace,
            )
        )
        violations: set[str] = set()

        for repo_path, snapshot in before_snapshots.items():
            if self._snapshot_path(repo_path) != snapshot:
                violations.add(repo_path)
                self._restore_snapshot(repo_path, snapshot)

        new_disallowed = sorted(
            repo_path
            for repo_path in (after_dirty - before_dirty)
            if not self._context.policy.can_write(f"/workspace/{repo_path}")
        )
        if new_disallowed:
            violations.update(new_disallowed)
            await self._git.best_effort_revert_paths(
                container_id=self._context.container_id,
                workspace=self._context.workspace,
                paths=new_disallowed,
            )

        return sorted(violations)
