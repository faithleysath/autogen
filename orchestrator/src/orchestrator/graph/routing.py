from __future__ import annotations

from typing import Literal

from orchestrator.models.state import OrchestrationState


def route_stage_outcome(
    state: OrchestrationState,
) -> Literal["run_developer", "prepare_stage_workspace", "freeze_release_candidate"]:
    decision = state["current_gate_decision"]
    if decision == "FAIL":
        return "run_developer"
    if decision == "NEXT_STAGE":
        return "prepare_stage_workspace"
    if decision == "COMPLETE_ALL_STAGES":
        return "freeze_release_candidate"
    raise ValueError(f"unknown stage gate decision: {decision}")


def route_release_outcome(
    state: OrchestrationState,
) -> Literal["cleanup_run_resources", "reset_for_replan"]:
    decision = state["release_decision"]
    if decision == "PASS":
        return "cleanup_run_resources"
    if decision == "REWORK":
        return "reset_for_replan"
    raise ValueError(f"unknown release decision: {decision}")
