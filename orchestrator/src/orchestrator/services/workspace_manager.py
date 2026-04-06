from __future__ import annotations

import json
import logging
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.models.runtime import RunDirs, VISIBLE_ROOT, WorkspaceView, remove_path, write_json

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, config: OrchestratorConfig) -> None:
        self._config = config

    def create_run_dirs(self, run_id: str) -> RunDirs:
        run_root = self._config.workspace_root / "runs" / run_id
        metadata_root = run_root / "metadata"
        workspaces_root = run_root / "workspaces"
        logs_root = run_root / "logs"
        for path in (
            self._config.workspace_root,
            self._config.state_dir,
            run_root,
            metadata_root,
            workspaces_root,
            logs_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        logger.info("run_dirs_ready", extra={"run_id": run_id, "run_root": str(run_root)})
        return RunDirs(
            run_root=run_root,
            metadata_root=metadata_root,
            workspaces_root=workspaces_root,
            logs_root=logs_root,
        )

    def create_workspace(
        self,
        *,
        run_id: str,
        workspace_id: str,
        workspace_kind: str,
    ) -> WorkspaceView:
        run_dirs = self.create_run_dirs(run_id)
        backing_root = run_dirs.workspaces_root / workspace_id
        remove_path(backing_root)
        backing_root.mkdir(parents=True, exist_ok=True)

        metadata_path = run_dirs.metadata_root / f"{workspace_id}.json"
        write_json(
            metadata_path,
            {
                "run_id": run_id,
                "workspace_id": workspace_id,
                "workspace_kind": workspace_kind,
                "backing_root": str(backing_root),
                "visible_root": str(VISIBLE_ROOT),
            },
        )
        logger.info(
            "workspace_created",
            extra={
                "run_id": run_id,
                "workspace_id": workspace_id,
                "workspace_kind": workspace_kind,
                "backing_root": str(backing_root),
            },
        )

        return WorkspaceView(
            workspace_id=workspace_id,
            visible_root=VISIBLE_ROOT,
            backing_root=backing_root,
            run_id=run_id,
            workspace_kind=workspace_kind,
        )

    def remove_workspace(self, run_id: str, workspace_id: str) -> None:
        run_dirs = self.create_run_dirs(run_id)
        remove_path(run_dirs.workspaces_root / workspace_id)
        metadata_path = run_dirs.metadata_root / f"{workspace_id}.json"
        if metadata_path.exists():
            metadata_path.unlink()
        logger.info("workspace_removed", extra={"run_id": run_id, "workspace_id": workspace_id})

    def list_workspaces(self, run_id: str) -> list[WorkspaceView]:
        run_dirs = self.create_run_dirs(run_id)
        workspaces: list[WorkspaceView] = []
        for metadata_path in sorted(run_dirs.metadata_root.glob("*.json")):
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            workspaces.append(
                WorkspaceView(
                    workspace_id=metadata_path.stem,
                    visible_root=VISIBLE_ROOT,
                    backing_root=Path(payload["backing_root"]),
                    run_id=run_id,
                    workspace_kind=payload["workspace_kind"],
                )
            )
        return workspaces

    def load_workspace(self, run_id: str, workspace_id: str) -> WorkspaceView:
        run_dirs = self.create_run_dirs(run_id)
        metadata_path = run_dirs.metadata_root / f"{workspace_id}.json"
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        return WorkspaceView(
            workspace_id=workspace_id,
            visible_root=VISIBLE_ROOT,
            backing_root=Path(payload["backing_root"]),
            run_id=run_id,
            workspace_kind=payload["workspace_kind"],
        )
