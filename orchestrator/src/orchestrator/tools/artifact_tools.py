from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from orchestrator.services.artifact_service import ArtifactService
from orchestrator.tools.base import ToolContext, ToolSpec


class ArtifactToolset:
    def __init__(self, context: ToolContext, artifact_service: ArtifactService) -> None:
        self._context = context
        self._artifact_service = artifact_service

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="write_markdown_artifact",
                description="Write a markdown artifact with YAML frontmatter to an allowed workspace path.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "frontmatter": {"type": "object", "additionalProperties": True},
                        "body": {"type": "string"},
                    },
                    "required": ["path", "frontmatter", "body"],
                    "additionalProperties": False,
                },
                handler=self.write_markdown_artifact,
                strict=False,
            ),
            ToolSpec(
                name="read_markdown_artifact",
                description="Read a markdown artifact and return its parsed frontmatter and body.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                handler=self.read_markdown_artifact,
                read_only=True,
            ),
        ]

    async def write_markdown_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        visible_path = args["path"]
        self._context.policy.assert_writable(visible_path)
        frontmatter, body = self._coerce_write_payload(visible_path, args)
        frontmatter.setdefault("run_id", self._context.workspace.run_id)
        frontmatter.setdefault("role", self._context.role)
        frontmatter.setdefault("created_at", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self._artifact_service.write_artifact(
            self._context.workspace,
            visible_path,
            frontmatter,
            body,
        )
        return {"path": visible_path}

    async def read_markdown_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        artifact = self._artifact_service.read_artifact(self._context.workspace, args["path"])
        return {
            "path": args["path"],
            "frontmatter": artifact.meta,
            "body": artifact.body,
        }

    def _coerce_write_payload(
        self,
        visible_path: str,
        args: dict[str, Any],
    ) -> tuple[dict[str, Any], str]:
        if "frontmatter" in args and "body" in args:
            return dict(args["frontmatter"]), str(args["body"])

        raw_body = args.get("body")
        if not isinstance(raw_body, str):
            raise ValueError(
                "write_markdown_artifact requires frontmatter/body fields or a string body payload"
            )

        stripped = raw_body.lstrip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(raw_body)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                frontmatter = payload.get("frontmatter")
                body = payload.get("body")
                if isinstance(frontmatter, dict) and isinstance(body, str):
                    return dict(frontmatter), body

        artifact = self._artifact_service.parse_markdown_frontmatter(visible_path, raw_body)
        return dict(artifact.meta), artifact.body
