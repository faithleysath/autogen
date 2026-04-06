from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


VISIBLE_ROOT = PurePosixPath("/workspace")


@dataclass(slots=True)
class WorkspaceView:
    workspace_id: str
    visible_root: PurePosixPath
    backing_root: Path
    run_id: str
    workspace_kind: str

    def ensure_within_workspace(self, visible_path: str) -> PurePosixPath:
        candidate = PurePosixPath(visible_path)
        if not candidate.is_absolute():
            raise ValueError(f"path must be absolute: {visible_path}")
        try:
            candidate.relative_to(self.visible_root)
        except ValueError as exc:
            raise ValueError(f"path must stay within {self.visible_root}: {visible_path}") from exc
        normalized = self.visible_root.joinpath(candidate.relative_to(self.visible_root))
        for part in normalized.parts:
            if part == "..":
                raise ValueError(f"path traversal is not allowed: {visible_path}")
        return normalized

    def to_backing_path(self, visible_path: str) -> Path:
        normalized = self.ensure_within_workspace(visible_path)
        relative = normalized.relative_to(self.visible_root)
        resolved = (self.backing_root / relative.as_posix()).resolve()
        try:
            resolved.relative_to(self.backing_root.resolve())
        except ValueError as exc:
            raise ValueError(f"path resolves outside workspace: {visible_path}") from exc
        return resolved

    def to_visible_path(self, backing_path: Path) -> str:
        resolved = backing_path.resolve()
        try:
            relative = resolved.relative_to(self.backing_root.resolve())
        except ValueError as exc:
            raise ValueError(f"path is outside workspace backing root: {backing_path}") from exc
        return str(self.visible_root / PurePosixPath(relative.as_posix()))

    def to_repo_relative_path(self, visible_path: str) -> str:
        normalized = self.ensure_within_workspace(visible_path)
        return normalized.relative_to(self.visible_root).as_posix()


@dataclass(slots=True)
class RunDirs:
    run_root: Path
    metadata_root: Path
    workspaces_root: Path
    logs_root: Path


@dataclass(slots=True)
class ContainerHandle:
    container_id: str
    name: str
    image: str
    role: str
    workspace_id: str


@dataclass(slots=True)
class ExecResult:
    cmd: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str

    def raise_for_error(self, prefix: str) -> None:
        if self.exit_code != 0:
            raise RuntimeError(
                f"{prefix} failed with exit code {self.exit_code}\nSTDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"
            )


@dataclass(slots=True)
class StagePlan:
    stage_no: int
    stage_id: str
    goal: str
    inputs: list[str]
    exit_criteria: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_no": self.stage_no,
            "stage_id": self.stage_id,
            "goal": self.goal,
            "inputs": self.inputs,
            "exit_criteria": self.exit_criteria,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StagePlan":
        return cls(
            stage_no=int(payload["stage_no"]),
            stage_id=str(payload["stage_id"]),
            goal=str(payload["goal"]),
            inputs=[str(item) for item in payload.get("inputs", [])],
            exit_criteria=[str(item) for item in payload.get("exit_criteria", [])],
        )


@dataclass(slots=True)
class ReviewArtifact:
    role: str
    workspace_id: str
    report_path: str
    verdict: str
    candidate_code_sha: str
    published_commit_sha: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "workspace_id": self.workspace_id,
            "report_path": self.report_path,
            "verdict": self.verdict,
            "candidate_code_sha": self.candidate_code_sha,
            "published_commit_sha": self.published_commit_sha,
        }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def remove_path(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
