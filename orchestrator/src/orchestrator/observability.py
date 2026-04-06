from __future__ import annotations

from contextlib import asynccontextmanager, nullcontext
from typing import Any, AsyncIterator

from langsmith import Client, trace, tracing_context
from langsmith.run_helpers import get_current_run_tree

from orchestrator.config import OrchestratorConfig


def build_langsmith_client(config: OrchestratorConfig) -> Client | None:
    if not config.langsmith_tracing or not config.langsmith_api_key:
        return None
    return Client(
        api_key=config.langsmith_api_key,
        workspace_id=config.langsmith_workspace_id,
    )


def tracing_context_for_run(
    *,
    config: OrchestratorConfig,
    client: Client | None,
    thread_id: str,
    repo_url: str | None = None,
):
    if client is None:
        return nullcontext()
    metadata = {"thread_id": thread_id}
    if repo_url:
        metadata["repo_url"] = repo_url
    return tracing_context(
        enabled=True,
        client=client,
        project_name=config.langsmith_project,
        tags=["autogen", "orchestrator"],
        metadata=metadata,
    )


@asynccontextmanager
async def traced_block(
    *,
    enabled: bool,
    name: str,
    run_type: str,
    inputs: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    client: Client | None = None,
) -> AsyncIterator[Any]:
    if not enabled:
        yield None
        return
    async with trace(
        name=name,
        run_type=run_type,
        inputs=inputs,
        metadata=metadata,
        tags=tags,
        client=client,
    ) as run_tree:
        yield run_tree


def current_langsmith_ids() -> dict[str, str]:
    run = get_current_run_tree()
    if run is None:
        return {}
    payload = {"langsmith_run_id": str(run.id)}
    trace_id = getattr(run, "trace_id", None)
    if trace_id is not None:
        payload["langsmith_trace_id"] = str(trace_id)
    return payload


def summarize_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": state.get("run_id"),
        "run_status": state.get("run_status"),
        "cycle_no": state.get("cycle_no"),
        "stage_no": state.get("stage_no"),
        "attempt_no": state.get("attempt_no"),
        "release_no": state.get("release_no"),
        "current_gate_decision": state.get("current_gate_decision"),
        "release_decision": state.get("release_decision"),
    }
