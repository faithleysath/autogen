from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

from orchestrator.models.artifacts import ArtifactDocument
from orchestrator.models.runtime import WorkspaceView

logger = logging.getLogger(__name__)


FRONTMATTER_PATTERN = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class ArtifactService:
    def render_markdown_with_frontmatter(self, meta: dict[str, Any], body: str) -> str:
        frontmatter = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
        body = body.rstrip() + "\n"
        return f"---\n{frontmatter}\n---\n\n{body}"

    def parse_markdown_frontmatter(self, path: Path, content: str) -> ArtifactDocument:
        match = FRONTMATTER_PATTERN.match(content)
        if not match:
            raise ValueError(f"missing YAML frontmatter: {path}")
        meta_raw, body = match.groups()
        meta = yaml.safe_load(meta_raw) or {}
        return ArtifactDocument(path=path, meta=meta, body=body.lstrip("\n"))

    def write_artifact(
        self,
        workspace: WorkspaceView,
        visible_path: str,
        meta: dict[str, Any],
        body: str,
    ) -> Path:
        backing_path = workspace.to_backing_path(visible_path)
        backing_path.parent.mkdir(parents=True, exist_ok=True)
        backing_path.write_text(
            self.render_markdown_with_frontmatter(meta, body),
            encoding="utf-8",
        )
        logger.info(
            "artifact_written",
            extra={
                "workspace_id": workspace.workspace_id,
                "path": visible_path,
                "kind": meta.get("kind"),
            },
        )
        return backing_path

    def read_artifact(self, workspace: WorkspaceView, visible_path: str) -> ArtifactDocument:
        backing_path = workspace.to_backing_path(visible_path)
        logger.info(
            "artifact_read",
            extra={"workspace_id": workspace.workspace_id, "path": visible_path},
        )
        return self.parse_markdown_frontmatter(
            backing_path,
            backing_path.read_text(encoding="utf-8"),
        )

    def write_json(self, workspace: WorkspaceView, visible_path: str, payload: dict[str, Any]) -> Path:
        backing_path = workspace.to_backing_path(visible_path)
        backing_path.parent.mkdir(parents=True, exist_ok=True)
        backing_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info(
            "json_written",
            extra={"workspace_id": workspace.workspace_id, "path": visible_path},
        )
        return backing_path

    def read_json(self, workspace: WorkspaceView, visible_path: str) -> dict[str, Any]:
        backing_path = workspace.to_backing_path(visible_path)
        return json.loads(backing_path.read_text(encoding="utf-8"))

    def copy_file(
        self,
        src_workspace: WorkspaceView,
        src_visible_path: str,
        dst_workspace: WorkspaceView,
        dst_visible_path: str,
    ) -> None:
        src = src_workspace.to_backing_path(src_visible_path)
        dst = dst_workspace.to_backing_path(dst_visible_path)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        logger.info(
            "artifact_copied",
            extra={
                "src_workspace_id": src_workspace.workspace_id,
                "dst_workspace_id": dst_workspace.workspace_id,
                "src_path": src_visible_path,
                "dst_path": dst_visible_path,
            },
        )
