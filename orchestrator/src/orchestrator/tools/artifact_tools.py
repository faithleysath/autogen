from __future__ import annotations

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
            ),
        ]

    async def write_markdown_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        visible_path = args["path"]
        self._context.policy.assert_writable(visible_path)
        self._artifact_service.write_artifact(
            self._context.workspace,
            visible_path,
            args["frontmatter"],
            args["body"],
        )
        return {"path": visible_path}

    async def read_markdown_artifact(self, args: dict[str, Any]) -> dict[str, Any]:
        artifact = self._artifact_service.read_artifact(self._context.workspace, args["path"])
        return {
            "path": args["path"],
            "frontmatter": artifact.meta,
            "body": artifact.body,
        }
