from __future__ import annotations

from typing import Any

from orchestrator.app import OrchestratorApp
from orchestrator.graph.nodes.common import (
    cleanup_workspace_and_container,
    now_utc,
    stage_workspace_id,
    visible_relpath,
)
from orchestrator.models.state import OrchestrationState
from orchestrator.models.usage import usage_summary_delta


async def prepare_stage_workspace(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    stage_plan = state["planned_stages"][state["stage_index"]]
    stage_no = stage_plan["stage_no"]
    workspace_id = stage_workspace_id(stage_no)
    workspace = app.workspace_manager.create_workspace(
        run_id=state["run_id"],
        workspace_id=workspace_id,
        workspace_kind="stage-dev",
    )
    container = await app.docker_manager.create_container(
        image=app.config.dev_image,
        name=app.container_name(state["run_id"], workspace_id),
        workspace_view=workspace,
        role="stage-dev",
        labels={
            "autogen.run_id": state["run_id"],
            "autogen.workspace_id": workspace_id,
            "autogen.workspace_kind": "stage-dev",
            "autogen.role": "stage-dev",
            "autogen.stage": str(stage_no),
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
    active_workspaces["stage_dev"] = workspace_id
    active_containers["stage_dev"] = container.container_id
    return {
        "stage_no": stage_no,
        "attempt_no": 0,
        "current_stage_plan": stage_plan,
        "current_stage_gate_path": None,
        "current_gate_decision": None,
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "run_status": "DEVELOPING",
        "event_log": [{"event": "prepare_stage_workspace", "stage_no": stage_no, "at": now_utc()}],
    }


async def run_developer(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"]["stage_dev"],
    )
    result = await app.role_runner.run_developer(
        state=state,
        workspace=workspace,
        container_id=state["active_containers"]["stage_dev"],
    )
    return {
        "usage_summary": usage_summary_delta("developer", result.get("usage")),
        "run_status": "DEVELOPING",
        "event_log": [{"event": "run_developer", "summary": result["summary"], "at": now_utc()}],
    }


async def run_stage_gate(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    attempt_no = state["attempt_no"] + 1
    workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"]["stage_dev"],
    )
    working_state = dict(state)
    working_state["attempt_no"] = attempt_no
    is_final_stage = state["stage_index"] == len(state["planned_stages"]) - 1
    result = await app.role_runner.run_stage_gate(
        state=working_state,
        workspace=workspace,
        container_id=state["active_containers"]["stage_dev"],
        is_final_stage=is_final_stage,
    )
    return {
        "attempt_no": attempt_no,
        "current_gate_decision": result["decision"],
        "current_stage_gate_path": result["gate_path"],
        "usage_summary": usage_summary_delta("stage_gate", result.get("usage")),
        "event_log": [{"event": "run_stage_gate", "decision": result["decision"], "at": now_utc()}],
    }


async def publish_stage_gate_result(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"]["stage_dev"],
    )
    decision = state["current_gate_decision"]
    gate_rel = visible_relpath(state["current_stage_gate_path"])
    if decision == "FAIL":
        message = f"gate(stage-{state['stage_no']:03d}): fail attempt-{state['attempt_no']:03d}"
        commit_sha = await app.git_service.commit_paths(
            container_id=state["active_containers"]["stage_dev"],
            workspace=workspace,
            message=message,
            paths=[gate_rel],
        )
        if commit_sha:
            await app.git_service.push(
                container_id=state["active_containers"]["stage_dev"],
                workspace=workspace,
                repo_url=state["repo_url"],
                run_branch=state["run_branch"],
            )
        return {
            "event_log": [{"event": "publish_stage_gate_result", "decision": decision, "at": now_utc()}],
        }

    verb = "pass" if decision == "NEXT_STAGE" else "complete"
    message = f"gate(stage-{state['stage_no']:03d}): {verb} attempt-{state['attempt_no']:03d}"
    commit_sha = await app.git_service.commit_all(
        container_id=state["active_containers"]["stage_dev"],
        workspace=workspace,
        message=message,
    )
    if commit_sha:
        await app.git_service.push(
            container_id=state["active_containers"]["stage_dev"],
            workspace=workspace,
            repo_url=state["repo_url"],
            run_branch=state["run_branch"],
        )

    stage_workspace_id_value = state["active_workspaces"].get("stage_dev")
    stage_container_id = state["active_containers"].get("stage_dev")
    await cleanup_workspace_and_container(
        app,
        run_id=state["run_id"],
        workspace_id=stage_workspace_id_value,
        container_id=stage_container_id,
    )
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    active_workspaces.pop("stage_dev", None)
    active_containers.pop("stage_dev", None)

    updates: dict[str, Any] = {
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "event_log": [{"event": "publish_stage_gate_result", "decision": decision, "at": now_utc()}],
    }
    if decision == "NEXT_STAGE":
        next_stage_index = state["stage_index"] + 1
        updates["stage_index"] = next_stage_index
        updates["current_stage_plan"] = state["planned_stages"][next_stage_index]
    return updates
