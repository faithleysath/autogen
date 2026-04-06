from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import Any

from orchestrator.app import OrchestratorApp
from orchestrator.models.runtime import StagePlan


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{timestamp}-{secrets.token_hex(3)}"


def artifact_root(run_id: str) -> str:
    return f"/workspace/.autogen/runs/{run_id}"


def planning_workspace_id(cycle_no: int) -> str:
    return f"cycle-{cycle_no:03d}-planning"


def stage_workspace_id(stage_no: int) -> str:
    return f"stage-{stage_no:03d}-dev"


def review_workspace_id(release_no: int, role: str) -> str:
    return f"release-{release_no:03d}-{role}"


def publisher_workspace_id(release_no: int) -> str:
    return f"release-{release_no:03d}-publisher"


def visible_relpath(visible_path: str) -> str:
    return visible_path.removeprefix("/workspace/")


async def cleanup_workspace_and_container(
    app: OrchestratorApp,
    *,
    run_id: str,
    workspace_id: str | None,
    container_id: str | None,
) -> None:
    if container_id:
        await app.docker_manager.remove_container(container_id)
    if workspace_id:
        app.workspace_manager.remove_workspace(run_id, workspace_id)


def normalize_stages(raw_stages: list[dict[str, Any]], next_stage_no: int) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for offset, raw in enumerate(raw_stages):
        stage_no = next_stage_no + offset
        stage = StagePlan(
            stage_no=stage_no,
            stage_id=f"stage-{stage_no:03d}",
            goal=str(raw["goal"]),
            inputs=[str(item) for item in raw.get("inputs", [])],
            exit_criteria=[str(item) for item in raw.get("exit_criteria", [])],
        )
        normalized.append(stage.to_dict())
    return normalized
