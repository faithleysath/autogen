from __future__ import annotations

from dataclasses import dataclass

from langsmith import Client

from orchestrator.config import OrchestratorConfig
from orchestrator.observability import build_langsmith_client
from orchestrator.services.artifact_service import ArtifactService
from orchestrator.services.background_tasks import BackgroundTaskManager
from orchestrator.services.docker_manager import DockerManager
from orchestrator.services.git_service import GitService
from orchestrator.services.lock_service import LockService
from orchestrator.services.role_runner import OpenAIRoleRunner, RoleRunnerProtocol
from orchestrator.services.workspace_manager import WorkspaceManager


@dataclass(slots=True)
class OrchestratorApp:
    config: OrchestratorConfig
    langsmith_client: Client | None
    workspace_manager: WorkspaceManager
    artifact_service: ArtifactService
    docker_manager: DockerManager
    lock_service: LockService
    git_service: GitService
    background_task_manager: BackgroundTaskManager
    role_runner: RoleRunnerProtocol

    def container_name(self, run_id: str, workspace_id: str) -> str:
        return f"autogen-{run_id}-{workspace_id}"


def build_app(
    *,
    config: OrchestratorConfig | None = None,
    role_runner: RoleRunnerProtocol | None = None,
) -> OrchestratorApp:
    app_config = config or OrchestratorConfig.from_env()
    langsmith_client = build_langsmith_client(app_config)
    workspace_manager = WorkspaceManager(app_config)
    artifact_service = ArtifactService()
    docker_manager = DockerManager(app_config)
    lock_service = LockService(app_config.sqlite_path)
    git_service = GitService(app_config, docker_manager, lock_service)
    background_task_manager = BackgroundTaskManager(
        docker_manager=docker_manager,
        tasks_root=app_config.tasks_dir,
    )
    resolved_role_runner = role_runner or OpenAIRoleRunner(
        app_config,
        langsmith_client,
        artifact_service,
        docker_manager,
        git_service,
        background_task_manager,
    )
    return OrchestratorApp(
        config=app_config,
        langsmith_client=langsmith_client,
        workspace_manager=workspace_manager,
        artifact_service=artifact_service,
        docker_manager=docker_manager,
        lock_service=lock_service,
        git_service=git_service,
        background_task_manager=background_task_manager,
        role_runner=resolved_role_runner,
    )
