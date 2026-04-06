from __future__ import annotations

from typing import Any

from orchestrator.config import OrchestratorConfig
from orchestrator.services.docker_manager import DockerManager
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


class BashToolset:
    def __init__(
        self,
        context: ToolContext,
        docker_manager: DockerManager,
        config: OrchestratorConfig,
    ) -> None:
        self._context = context
        self._docker = docker_manager
        self._config = config

    def specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="run_command",
                description=(
                    "Run an allowed non-shell command inside the role container. "
                    "Pass argv as an array of strings; shell metacharacters are not available."
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
        self._context.workspace.ensure_within_workspace(cwd)
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
        return {
            "argv": argv,
            "cwd": cwd,
            "exit_code": result.exit_code,
            "stdout": result.stdout[-12000:],
            "stderr": result.stderr[-12000:],
        }
