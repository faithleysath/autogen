from __future__ import annotations

from typing import Any

from orchestrator.app import OrchestratorApp
from orchestrator.graph.nodes.common import artifact_root, new_run_id, now_utc, planning_workspace_id
from orchestrator.models.state import OrchestrationState


async def initialize_run(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    run_id = state.get("run_id") or new_run_id()
    cycle_no = state["cycle_no"]
    workspace_id = planning_workspace_id(cycle_no)
    workspace = app.workspace_manager.create_workspace(
        run_id=run_id,
        workspace_id=workspace_id,
        workspace_kind="planning",
    )
    container = await app.docker_manager.create_container(
        image=app.config.dev_image,
        name=app.container_name(run_id, workspace_id),
        workspace_view=workspace,
        role="planning",
        labels={
            "autogen.run_id": run_id,
            "autogen.workspace_id": workspace_id,
            "autogen.workspace_kind": "planning",
            "autogen.role": "planning",
            "autogen.cycle": str(cycle_no),
        },
    )
    base_branch = await app.git_service.get_default_branch(
        container_id=container.container_id,
        repo_url=state["repo_url"],
    )
    await app.git_service.clone_repo(
        container_id=container.container_id,
        workspace=workspace,
        repo_url=state["repo_url"],
    )
    await app.git_service.ensure_autogen_trackable(
        container_id=container.container_id,
        workspace=workspace,
    )
    run_branch = f"autogen/{run_id}"
    await app.git_service.create_run_branch(
        container_id=container.container_id,
        workspace=workspace,
        base_branch=base_branch,
        run_branch=run_branch,
    )

    run_root = artifact_root(run_id)
    prd_path = f"{run_root}/00-input/prd.md"
    run_json_path = f"{run_root}/00-input/run.json"
    app.artifact_service.write_artifact(
        workspace,
        prd_path,
        {
            "kind": "prd_input",
            "run_id": run_id,
            "role": "system",
            "created_at": now_utc(),
            "run_branch": run_branch,
            "base_branch": base_branch,
        },
        state["prd_markdown"],
    )
    app.artifact_service.write_json(
        workspace,
        run_json_path,
        {
            "run_id": run_id,
            "repo_url": state["repo_url"],
            "base_branch": base_branch,
            "run_branch": run_branch,
            "created_at": now_utc(),
        },
    )

    await app.git_service.commit_all(
        container_id=container.container_id,
        workspace=workspace,
        message="run(init): capture PRD and create run branch",
    )
    await app.git_service.push(
        container_id=container.container_id,
        workspace=workspace,
        repo_url=state["repo_url"],
        run_branch=run_branch,
        set_upstream=True,
    )

    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    active_workspaces["planning"] = workspace_id
    active_containers["planning"] = container.container_id
    return {
        "run_id": run_id,
        "base_branch": base_branch,
        "run_branch": run_branch,
        "artifact_root_path": run_root,
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "run_status": "INITIALIZED",
        "event_log": [{"event": "initialize_run", "run_id": run_id, "at": now_utc()}],
    }
