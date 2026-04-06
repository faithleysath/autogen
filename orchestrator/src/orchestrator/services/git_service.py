from __future__ import annotations

import logging
from pathlib import Path

from orchestrator.config import OrchestratorConfig
from orchestrator.models.runtime import WorkspaceView
from orchestrator.services.docker_manager import DockerManager
from orchestrator.services.lock_service import LockService


AUTOGEN_IGNORE_BLOCK = """# autogen tracked artifacts
!.autogen/
!.autogen/runs/
!.autogen/runs/**
"""

logger = logging.getLogger(__name__)


class GitService:
    def __init__(
        self,
        config: OrchestratorConfig,
        docker_manager: DockerManager,
        lock_service: LockService,
    ) -> None:
        self._config = config
        self._docker = docker_manager
        self._locks = lock_service

    async def run_git(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        args: list[str],
        cwd: str = "/workspace",
    ) -> str:
        result = await self._docker.exec(
            container_id=container_id,
            cmd=["git", *args],
            cwd=cwd,
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        result.raise_for_error(f"git {' '.join(args)}")
        logger.info(
            "git_command",
            extra={
                "container_id": container_id,
                "workspace_id": workspace.workspace_id,
                "args": args,
                "cwd": cwd,
            },
        )
        return result.stdout.strip()

    async def configure_identity(self, *, container_id: str, workspace: WorkspaceView) -> None:
        await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["config", "user.name", self._config.git_user_name],
        )
        await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["config", "user.email", self._config.git_user_email],
        )

    async def get_default_branch(self, *, container_id: str, repo_url: str) -> str:
        result = await self._docker.exec(
            container_id=container_id,
            cmd=["git", "ls-remote", "--symref", repo_url, "HEAD"],
            cwd="/workspace",
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        result.raise_for_error("git ls-remote --symref")
        for line in result.stdout.splitlines():
            if not line.startswith("ref: "):
                continue
            head_ref = line.split()[1]
            logger.info("git_default_branch", extra={"repo_url": repo_url, "base_branch": head_ref})
            return head_ref.removeprefix("refs/heads/")
        raise RuntimeError(f"could not determine default branch for {repo_url}")

    async def clone_repo(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        repo_url: str,
    ) -> None:
        result = await self._docker.exec(
            container_id=container_id,
            cmd=["git", "clone", repo_url, "/workspace"],
            cwd="/",
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        result.raise_for_error("git clone")
        logger.info(
            "git_clone",
            extra={"repo_url": repo_url, "workspace_id": workspace.workspace_id, "container_id": container_id},
        )
        await self.configure_identity(container_id=container_id, workspace=workspace)

    async def checkout_branch(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        branch: str,
        create: bool = False,
        start_point: str | None = None,
    ) -> None:
        args = ["checkout"]
        if create:
            args.extend(["-b", branch])
            if start_point:
                args.append(start_point)
        else:
            args.append(branch)
        await self.run_git(container_id=container_id, workspace=workspace, args=args)

    async def checkout_detached(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        ref: str,
    ) -> None:
        await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["checkout", "--detach", ref],
        )

    async def fetch(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        remote: str = "origin",
    ) -> None:
        await self.run_git(container_id=container_id, workspace=workspace, args=["fetch", remote])

    async def create_run_branch(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        base_branch: str,
        run_branch: str,
    ) -> None:
        await self.checkout_branch(
            container_id=container_id,
            workspace=workspace,
            branch=base_branch,
        )
        await self.checkout_branch(
            container_id=container_id,
            workspace=workspace,
            branch=run_branch,
            create=True,
        )

    async def ensure_autogen_trackable(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
    ) -> None:
        result = await self._docker.exec(
            container_id=container_id,
            cmd=["git", "check-ignore", "-v", ".autogen/runs/.probe"],
            cwd="/workspace",
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        if result.exit_code != 0:
            return

        gitignore_path = workspace.to_backing_path("/workspace/.gitignore")
        existing = gitignore_path.read_text(encoding="utf-8") if gitignore_path.exists() else ""
        if AUTOGEN_IGNORE_BLOCK not in existing:
            if existing and not existing.endswith("\n"):
                existing += "\n"
            existing += "\n" + AUTOGEN_IGNORE_BLOCK
            gitignore_path.write_text(existing, encoding="utf-8")

        verification = await self._docker.exec(
            container_id=container_id,
            cmd=["git", "check-ignore", "-v", ".autogen/runs/.probe"],
            cwd="/workspace",
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        if verification.exit_code == 0:
            raise RuntimeError(
                ".autogen is still ignored after attempting to patch .gitignore. "
                "This likely comes from a parent or global ignore rule."
            )
        logger.info("autogen_trackable", extra={"workspace_id": workspace.workspace_id})

    async def current_head(self, *, container_id: str, workspace: WorkspaceView) -> str:
        return await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["rev-parse", "HEAD"],
        )

    async def status_short(self, *, container_id: str, workspace: WorkspaceView) -> str:
        return await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["status", "--short"],
        )

    async def diff(self, *, container_id: str, workspace: WorkspaceView) -> str:
        return await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["diff"],
        )

    async def pull_rebase(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        run_branch: str,
    ) -> None:
        await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["pull", "--rebase", "--autostash", "origin", run_branch],
        )
        logger.info(
            "git_pull_rebase",
            extra={"workspace_id": workspace.workspace_id, "run_branch": run_branch},
        )

    async def commit_paths(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        message: str,
        paths: list[str],
    ) -> str | None:
        add_args = ["add", "--", *paths]
        await self.run_git(container_id=container_id, workspace=workspace, args=add_args)
        diff_result = await self._docker.exec(
            container_id=container_id,
            cmd=["git", "diff", "--cached", "--name-only"],
            cwd="/workspace",
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        diff_result.raise_for_error("git diff --cached --name-only")
        if not diff_result.stdout.strip():
            return None
        await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["commit", "-m", message],
        )
        commit_sha = await self.current_head(container_id=container_id, workspace=workspace)
        logger.info(
            "git_commit_paths",
            extra={"workspace_id": workspace.workspace_id, "message": message, "paths": paths, "commit_sha": commit_sha},
        )
        return commit_sha

    async def commit_all(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        message: str,
    ) -> str | None:
        await self.run_git(container_id=container_id, workspace=workspace, args=["add", "-A"])
        diff_result = await self._docker.exec(
            container_id=container_id,
            cmd=["git", "diff", "--cached", "--name-only"],
            cwd="/workspace",
            timeout_seconds=self._config.cmd_timeout_seconds,
        )
        diff_result.raise_for_error("git diff --cached --name-only")
        if not diff_result.stdout.strip():
            return None
        await self.run_git(
            container_id=container_id,
            workspace=workspace,
            args=["commit", "-m", message],
        )
        commit_sha = await self.current_head(container_id=container_id, workspace=workspace)
        logger.info(
            "git_commit_all",
            extra={"workspace_id": workspace.workspace_id, "message": message, "commit_sha": commit_sha},
        )
        return commit_sha

    async def push(
        self,
        *,
        container_id: str,
        workspace: WorkspaceView,
        repo_url: str,
        run_branch: str,
        set_upstream: bool = False,
    ) -> None:
        async with self._locks.push_lock(
            repo_url=repo_url,
            run_branch=run_branch,
            timeout_seconds=self._config.push_lock_timeout_seconds,
        ):
            args = ["push"]
            if set_upstream:
                args.append("-u")
            args.extend(["origin", run_branch])
            await self.run_git(container_id=container_id, workspace=workspace, args=args)
            logger.info(
                "git_push",
                extra={"workspace_id": workspace.workspace_id, "run_branch": run_branch, "set_upstream": set_upstream},
            )
