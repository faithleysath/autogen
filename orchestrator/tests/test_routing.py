from __future__ import annotations

from orchestrator.graph.nodes.common import normalize_stages
from orchestrator.graph.routing import route_release_outcome, route_stage_outcome


def test_route_stage_outcome():
    assert route_stage_outcome({"current_gate_decision": "FAIL"}) == "run_developer"
    assert route_stage_outcome({"current_gate_decision": "NEXT_STAGE"}) == "prepare_stage_workspace"
    assert (
        route_stage_outcome({"current_gate_decision": "COMPLETE_ALL_STAGES"})
        == "freeze_release_candidate"
    )


def test_route_release_outcome():
    assert route_release_outcome({"release_decision": "PASS"}) == "cleanup_run_resources"
    assert route_release_outcome({"release_decision": "REWORK"}) == "reset_for_replan"


def test_normalize_stages_continues_numbering():
    stages = normalize_stages(
        [{"goal": "one", "inputs": ["a"], "exit_criteria": ["b"]}],
        next_stage_no=3,
    )
    assert stages[0]["stage_no"] == 3
    assert stages[0]["stage_id"] == "stage-003"
