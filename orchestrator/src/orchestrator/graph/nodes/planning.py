from __future__ import annotations

from typing import Any

from orchestrator.app import OrchestratorApp
from orchestrator.graph.nodes.common import (
    artifact_root,
    cleanup_workspace_and_container,
    normalize_stages,
    now_utc,
    planning_workspace_id,
    visible_relpath,
)
from orchestrator.models.state import OrchestrationState
from orchestrator.models.usage import usage_summary_delta


async def prepare_planning_workspace(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    run_id = state["run_id"]
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
    await app.git_service.clone_repo(
        container_id=container.container_id,
        workspace=workspace,
        repo_url=state["repo_url"],
    )
    await app.git_service.checkout_branch(
        container_id=container.container_id,
        workspace=workspace,
        branch=state["run_branch"],
    )
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    active_workspaces["planning"] = workspace_id
    active_containers["planning"] = container.container_id
    return {
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "run_status": "PLANNING",
        "event_log": [{"event": "prepare_planning_workspace", "cycle": cycle_no, "at": now_utc()}],
    }


async def run_architect(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(run_id=state["run_id"], workspace_id=state["active_workspaces"]["planning"])
    result = await app.role_runner.run_architect(
        state=state,
        workspace=workspace,
        container_id=state["active_containers"]["planning"],
    )
    return {
        "execution_contract_path": result["execution_contract_path"],
        "plan_path": result["plan_path"],
        "e2e_plan_path": result["e2e_plan_path"],
        "usage_summary": usage_summary_delta("architect", result.get("usage")),
        "run_status": "PLANNING",
        "event_log": [{"event": "run_architect", "summary": result["summary"], "at": now_utc()}],
    }


async def publish_planning_artifacts(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(run_id=state["run_id"], workspace_id=state["active_workspaces"]["planning"])
    planning_rel = visible_relpath(f"{artifact_root(state['run_id'])}/10-planning/cycle-{state['cycle_no']:03d}")
    commit_sha = await app.git_service.commit_paths(
        container_id=state["active_containers"]["planning"],
        workspace=workspace,
        message=f"plan(cycle-{state['cycle_no']:03d}): freeze execution contract and plans",
        paths=[planning_rel],
    )
    if commit_sha:
        await app.git_service.push(
            container_id=state["active_containers"]["planning"],
            workspace=workspace,
            repo_url=state["repo_url"],
            run_branch=state["run_branch"],
        )
    return {
        "event_log": [{"event": "publish_planning_artifacts", "commit": commit_sha, "at": now_utc()}],
    }


async def load_stage_plan(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(run_id=state["run_id"], workspace_id=state["active_workspaces"]["planning"])
    plan_doc = app.artifact_service.read_artifact(workspace, state["plan_path"])
    raw_stages = plan_doc.meta.get("stages", [])
    if not raw_stages:
        raise RuntimeError("architecture-plan.md must include a non-empty frontmatter stages array")
    planned_stages = normalize_stages(raw_stages, state["next_stage_no"])

    planning_workspace_id_value = state["active_workspaces"].get("planning")
    planning_container_id = state["active_containers"].get("planning")
    await cleanup_workspace_and_container(
        app,
        run_id=state["run_id"],
        workspace_id=planning_workspace_id_value,
        container_id=planning_container_id,
    )
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    active_workspaces.pop("planning", None)
    active_containers.pop("planning", None)
    return {
        "planned_stages": planned_stages,
        "current_stage_plan": planned_stages[0],
        "stage_index": 0,
        "next_stage_no": state["next_stage_no"] + len(planned_stages),
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "run_status": "PLANNING",
        "event_log": [{"event": "load_stage_plan", "stage_count": len(planned_stages), "at": now_utc()}],
    }
