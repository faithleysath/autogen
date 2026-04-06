from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict

from orchestrator.models.usage import merge_usage_summaries


def merge_dicts(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if left:
        merged.update(left)
    if right:
        merged.update(right)
    return merged


class OrchestrationState(TypedDict, total=False):
    repo_url: str
    prd_markdown: str
    run_id: str
    base_branch: str
    run_branch: str
    run_status: str
    cycle_no: int
    next_stage_no: int
    stage_no: int
    stage_index: int
    attempt_no: int
    release_no: int
    planned_stages: list[dict[str, Any]]
    current_stage_plan: dict[str, Any] | None
    candidate_code_sha: str | None
    active_workspaces: dict[str, str]
    active_containers: dict[str, str]
    execution_contract_path: str | None
    plan_path: str | None
    e2e_plan_path: str | None
    current_stage_gate_path: str | None
    current_gate_decision: str | None
    review_results: Annotated[dict[str, dict[str, Any]], merge_dicts]
    release_decision: str | None
    release_decision_path: str | None
    rework_summary_path: str | None
    artifact_root_path: str
    usage_summary: Annotated[dict[str, Any], merge_usage_summaries]
    last_error: dict[str, Any] | None
    event_log: Annotated[list[dict[str, Any]], operator.add]


def make_initial_state(repo_url: str, prd_markdown: str) -> OrchestrationState:
    return {
        "repo_url": repo_url,
        "prd_markdown": prd_markdown,
        "run_status": "NEW",
        "cycle_no": 1,
        "next_stage_no": 1,
        "stage_no": 0,
        "stage_index": 0,
        "attempt_no": 0,
        "release_no": 0,
        "planned_stages": [],
        "current_stage_plan": None,
        "candidate_code_sha": None,
        "active_workspaces": {},
        "active_containers": {},
        "execution_contract_path": None,
        "plan_path": None,
        "e2e_plan_path": None,
        "current_stage_gate_path": None,
        "current_gate_decision": None,
        "review_results": {},
        "release_decision": None,
        "release_decision_path": None,
        "rework_summary_path": None,
        "artifact_root_path": "",
        "usage_summary": {},
        "last_error": None,
        "event_log": [],
    }
