from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.models.runtime import WorkspaceView
from orchestrator.tools.base import ToolContext, ToolSpec


def _line_slice(content: str, start_line: int | None, end_line: int | None) -> str:
    lines = content.splitlines()
    start = 1 if start_line is None else max(1, start_line)
    end = len(lines) if end_line is None else min(len(lines), end_line)
    numbered = [f"{idx}: {line}" for idx, line in enumerate(lines[start - 1 : end], start=start)]
    return "\n".join(numbered)


class FileToolset:
    def __init__(self, context: ToolContext) -> None:
        self._context = context

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="list_files",
                description="List files under a workspace path. Use this to explore the repository tree.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_entries": {"type": "integer"},
                    },
                    "required": ["path", "max_entries"],
                    "additionalProperties": False,
                },
                handler=self.list_files,
            ),
            ToolSpec(
                name="read_file",
                description="Read a UTF-8 text file from the workspace. Returns line-numbered output.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start_line": {"type": "integer"},
                        "end_line": {"type": "integer"},
                    },
                    "required": ["path", "start_line", "end_line"],
                    "additionalProperties": False,
                },
                handler=self.read_file,
            ),
            ToolSpec(
                name="search_text",
                description="Search for literal text across files within the workspace.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "query": {"type": "string"},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["path", "query", "max_results"],
                    "additionalProperties": False,
                },
                handler=self.search_text,
            ),
            ToolSpec(
                name="write_file",
                description="Write a UTF-8 text file to an allowed workspace path, creating parent directories when needed.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                handler=self.write_file,
            ),
            ToolSpec(
                name="replace_in_file",
                description="Replace text within an allowed workspace file. Use after reading the current file content.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old": {"type": "string"},
                        "new": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old", "new", "replace_all"],
                    "additionalProperties": False,
                },
                handler=self.replace_in_file,
            ),
        ]

    async def list_files(self, args: dict[str, Any]) -> dict[str, Any]:
        visible_path = args["path"]
        max_entries = max(1, min(int(args["max_entries"]), 500))
        backing_path = self._context.workspace.to_backing_path(visible_path)
        if not backing_path.exists():
            raise FileNotFoundError(visible_path)
        entries: list[str] = []
        if backing_path.is_file():
            entries.append(visible_path)
        else:
            for path in sorted(backing_path.rglob("*")):
                if len(entries) >= max_entries:
                    break
                entries.append(self._context.workspace.to_visible_path(path))
        return {"entries": entries, "truncated": len(entries) >= max_entries}

    async def read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        visible_path = args["path"]
        backing_path = self._context.workspace.to_backing_path(visible_path)
        content = backing_path.read_text(encoding="utf-8")
        excerpt = _line_slice(content, args["start_line"], args["end_line"])
        return {"path": visible_path, "content": excerpt}

    async def search_text(self, args: dict[str, Any]) -> dict[str, Any]:
        root = self._context.workspace.to_backing_path(args["path"])
        query = args["query"]
        max_results = max(1, min(int(args["max_results"]), 200))
        results: list[dict[str, Any]] = []
        for path in sorted(root.rglob("*")):
            if len(results) >= max_results:
                break
            if not path.is_file():
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                if query not in line:
                    continue
                results.append(
                    {
                        "path": self._context.workspace.to_visible_path(path),
                        "line": line_no,
                        "content": line,
                    }
                )
                if len(results) >= max_results:
                    break
        return {"results": results, "truncated": len(results) >= max_results}

    async def write_file(self, args: dict[str, Any]) -> dict[str, Any]:
        visible_path = args["path"]
        self._context.policy.assert_writable(visible_path)
        backing_path = self._context.workspace.to_backing_path(visible_path)
        backing_path.parent.mkdir(parents=True, exist_ok=True)
        backing_path.write_text(args["content"], encoding="utf-8")
        return {"path": visible_path, "bytes_written": len(args["content"].encode("utf-8"))}

    async def replace_in_file(self, args: dict[str, Any]) -> dict[str, Any]:
        visible_path = args["path"]
        self._context.policy.assert_writable(visible_path)
        backing_path = self._context.workspace.to_backing_path(visible_path)
        content = backing_path.read_text(encoding="utf-8")
        old = args["old"]
        new = args["new"]
        if old not in content:
            raise ValueError(f"text not found in {visible_path}")
        if args["replace_all"]:
            updated = content.replace(old, new)
            replacements = content.count(old)
        else:
            updated = content.replace(old, new, 1)
            replacements = 1
        backing_path.write_text(updated, encoding="utf-8")
        return {"path": visible_path, "replacements": replacements}
