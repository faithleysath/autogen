from __future__ import annotations

import logging

from langgraph.graph import END, START, StateGraph

from orchestrator.app import OrchestratorApp
from orchestrator.graph.nodes.cleanup import cleanup_run_resources, end_failure, end_success
from orchestrator.graph.nodes.initialize import initialize_run
from orchestrator.graph.nodes.planning import (
    load_stage_plan,
    prepare_planning_workspace,
    publish_planning_artifacts,
    run_architect,
)
from orchestrator.graph.nodes.release_loop import (
    freeze_release_candidate,
    join_review_results,
    prepare_review_workspaces,
    publish_release_decision,
    publish_review_reports,
    reset_for_replan,
    run_compliance_review,
    run_e2e_review,
    run_qa_review,
    run_release_gate,
)
from orchestrator.graph.nodes.stage_loop import (
    prepare_stage_workspace,
    publish_stage_gate_result,
    run_developer,
    run_stage_gate,
)
from orchestrator.graph.routing import route_release_outcome, route_stage_outcome
from orchestrator.models.state import OrchestrationState
from orchestrator.observability import summarize_state, traced_block
from orchestrator.persistence.sqlite import build_checkpointer

logger = logging.getLogger(__name__)


def _bind(node_fn, app: OrchestratorApp):
    async def _node(state: OrchestrationState):
        logger.info(
            "graph_node_start",
            extra={
                "node": node_fn.__name__,
                "run_id": state.get("run_id"),
                "cycle_no": state.get("cycle_no"),
                "stage_no": state.get("stage_no"),
                "release_no": state.get("release_no"),
            },
        )
        async with traced_block(
            enabled=app.langsmith_client is not None,
            name=f"graph.{node_fn.__name__}",
            run_type="chain",
            inputs={"state": summarize_state(state)},
            metadata={
                "node": node_fn.__name__,
                "run_id": state.get("run_id"),
                "cycle_no": state.get("cycle_no"),
                "stage_no": state.get("stage_no"),
                "release_no": state.get("release_no"),
            },
            tags=["autogen", "orchestrator", "graph-node"],
            client=app.langsmith_client,
        ) as node_trace:
            try:
                updates = await node_fn(state, app)
            except Exception:
                logger.exception(
                    "graph_node_failed",
                    extra={
                        "node": node_fn.__name__,
                        "run_id": state.get("run_id"),
                        "cycle_no": state.get("cycle_no"),
                        "stage_no": state.get("stage_no"),
                        "release_no": state.get("release_no"),
                    },
                )
                raise
            if node_trace is not None:
                node_trace.end(outputs={"updates": sorted(updates.keys())})
            logger.info(
                "graph_node_done",
                extra={
                    "node": node_fn.__name__,
                    "run_id": state.get("run_id"),
                    "update_keys": sorted(updates.keys()),
                },
            )
            return updates

    return _node


def build_graph(app: OrchestratorApp):
    builder = StateGraph(OrchestrationState)
    builder.add_node("initialize_run", _bind(initialize_run, app))
    builder.add_node("prepare_planning_workspace", _bind(prepare_planning_workspace, app))
    builder.add_node("run_architect", _bind(run_architect, app))
    builder.add_node("publish_planning_artifacts", _bind(publish_planning_artifacts, app))
    builder.add_node("load_stage_plan", _bind(load_stage_plan, app))
    builder.add_node("prepare_stage_workspace", _bind(prepare_stage_workspace, app))
    builder.add_node("run_developer", _bind(run_developer, app))
    builder.add_node("run_stage_gate", _bind(run_stage_gate, app))
    builder.add_node("publish_stage_gate_result", _bind(publish_stage_gate_result, app))
    builder.add_node("freeze_release_candidate", _bind(freeze_release_candidate, app))
    builder.add_node("prepare_review_workspaces", _bind(prepare_review_workspaces, app))
    builder.add_node("run_compliance_review", _bind(run_compliance_review, app))
    builder.add_node("run_qa_review", _bind(run_qa_review, app))
    builder.add_node("run_e2e_review", _bind(run_e2e_review, app))
    builder.add_node("join_review_results", _bind(join_review_results, app))
    builder.add_node("publish_review_reports", _bind(publish_review_reports, app))
    builder.add_node("run_release_gate", _bind(run_release_gate, app))
    builder.add_node("publish_release_decision", _bind(publish_release_decision, app))
    builder.add_node("reset_for_replan", _bind(reset_for_replan, app))
    builder.add_node("cleanup_run_resources", _bind(cleanup_run_resources, app))
    builder.add_node("end_success", _bind(end_success, app))
    builder.add_node("end_failure", _bind(end_failure, app))

    builder.add_edge(START, "initialize_run")
    builder.add_edge("initialize_run", "run_architect")
    builder.add_edge("prepare_planning_workspace", "run_architect")
    builder.add_edge("run_architect", "publish_planning_artifacts")
    builder.add_edge("publish_planning_artifacts", "load_stage_plan")
    builder.add_edge("load_stage_plan", "prepare_stage_workspace")
    builder.add_edge("prepare_stage_workspace", "run_developer")
    builder.add_edge("run_developer", "run_stage_gate")
    builder.add_edge("run_stage_gate", "publish_stage_gate_result")
    builder.add_conditional_edges("publish_stage_gate_result", route_stage_outcome)

    builder.add_edge("freeze_release_candidate", "prepare_review_workspaces")
    # Run release reviews serially to avoid bursting providers with strict rate limits.
    builder.add_edge("prepare_review_workspaces", "run_compliance_review")
    builder.add_edge("run_compliance_review", "run_qa_review")
    builder.add_edge("run_qa_review", "run_e2e_review")
    builder.add_edge("run_e2e_review", "join_review_results")
    builder.add_edge("join_review_results", "publish_review_reports")
    builder.add_edge("publish_review_reports", "run_release_gate")
    builder.add_edge("run_release_gate", "publish_release_decision")
    builder.add_conditional_edges("publish_release_decision", route_release_outcome)
    builder.add_edge("reset_for_replan", "prepare_planning_workspace")
    builder.add_edge("cleanup_run_resources", "end_success")
    builder.add_edge("end_success", END)
    builder.add_edge("end_failure", END)

    return builder.compile(checkpointer=build_checkpointer(app.config.sqlite_path))
