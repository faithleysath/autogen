from __future__ import annotations

from typing import Any

from orchestrator.app import OrchestratorApp
from orchestrator.graph.nodes.common import cleanup_workspace_and_container, now_utc
from orchestrator.models.state import OrchestrationState


async def cleanup_run_resources(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    for key in list(active_workspaces):
        await cleanup_workspace_and_container(
            app,
            run_id=state["run_id"],
            workspace_id=active_workspaces.get(key),
            container_id=active_containers.get(key),
        )
    return {
        "active_workspaces": {},
        "active_containers": {},
        "run_status": "PASSED" if state["release_decision"] == "PASS" else "FAILED",
        "event_log": [{"event": "cleanup_run_resources", "at": now_utc()}],
    }


async def end_success(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    del app
    return {
        "run_status": "PASSED",
        "event_log": [{"event": "end_success", "at": now_utc()}],
    }


async def end_failure(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    del app
    return {
        "run_status": "FAILED",
        "event_log": [{"event": "end_failure", "at": now_utc()}],
    }
