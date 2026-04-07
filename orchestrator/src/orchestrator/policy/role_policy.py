from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class RolePolicy:
    role: str
    writable_paths: tuple[str, ...]
    writable_prefixes: tuple[str, ...]
    allow_code_write: bool
    allow_command: bool
    allow_background_tasks: bool
    allowed_external_path_prefixes: tuple[str, ...] = ()

    def can_write(self, visible_path: str) -> bool:
        if self.allow_code_write:
            return not visible_path.startswith("/workspace/.git/") and not visible_path.startswith(
                "/workspace/.autogen/"
            )
        return visible_path in self.writable_paths or any(
            visible_path.startswith(prefix) for prefix in self.writable_prefixes
        )

    def assert_writable(self, visible_path: str) -> None:
        if not self.can_write(visible_path):
            raise PermissionError(f"{self.role} cannot write {visible_path}")

    def can_access_external_path(self, visible_path: str) -> bool:
        return any(
            visible_path == prefix or visible_path.startswith(f"{prefix.rstrip('/')}/")
            for prefix in self.allowed_external_path_prefixes
        )


def build_role_policy(
    *,
    role: str,
    run_id: str,
    cycle_no: int,
    stage_no: int | None = None,
    attempt_no: int | None = None,
    release_no: int | None = None,
) -> RolePolicy:
    run_root = f"/workspace/.autogen/runs/{run_id}"
    if role == "architect":
        return RolePolicy(
            role=role,
            writable_paths=(),
            writable_prefixes=(
                f"{run_root}/00-input/",
                f"{run_root}/10-planning/cycle-{cycle_no:03d}/",
            ),
            allowed_external_path_prefixes=(),
            allow_code_write=False,
            allow_command=True,
            allow_background_tasks=False,
        )
    if role == "developer":
        return RolePolicy(
            role=role,
            writable_paths=(),
            writable_prefixes=(),
            allowed_external_path_prefixes=(),
            allow_code_write=True,
            allow_command=True,
            allow_background_tasks=True,
        )
    if role == "stage_gate":
        assert stage_no is not None and attempt_no is not None
        return RolePolicy(
            role=role,
            writable_paths=(
                f"{run_root}/20-stages/stage-{stage_no:03d}/attempt-{attempt_no:03d}/gate-decision.md",
            ),
            writable_prefixes=(),
            allowed_external_path_prefixes=(),
            allow_code_write=False,
            allow_command=True,
            allow_background_tasks=True,
        )
    if role in {"compliance", "qa"}:
        assert release_no is not None
        return RolePolicy(
            role=role,
            writable_paths=(
                f"{run_root}/30-reviews/release-{release_no:03d}/{role}/report.md",
            ),
            writable_prefixes=(),
            allowed_external_path_prefixes=(),
            allow_code_write=False,
            allow_command=True,
            allow_background_tasks=False,
        )
    if role == "e2e":
        assert release_no is not None
        return RolePolicy(
            role=role,
            writable_paths=(
                f"{run_root}/30-reviews/release-{release_no:03d}/e2e/report.md",
            ),
            writable_prefixes=(
                f"{run_root}/30-reviews/release-{release_no:03d}/e2e/evidence/",
            ),
            allowed_external_path_prefixes=(
                "/ms-playwright",
                "/opt/bun/bin",
            ),
            allow_code_write=False,
            allow_command=True,
            allow_background_tasks=True,
        )
    if role == "release_gate":
        assert release_no is not None
        return RolePolicy(
            role=role,
            writable_paths=(
                f"{run_root}/40-release/release-{release_no:03d}/decision.md",
                f"{run_root}/50-rework/release-{release_no:03d}/rework-summary.md",
            ),
            writable_prefixes=(),
            allowed_external_path_prefixes=(),
            allow_code_write=False,
            allow_command=False,
            allow_background_tasks=False,
        )
    raise ValueError(f"unknown role: {role}")
