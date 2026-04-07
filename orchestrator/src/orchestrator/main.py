from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from orchestrator.app import OrchestratorApp, build_app
from orchestrator.graph.builder import build_graph
from orchestrator.graph.nodes.common import now_utc
from orchestrator.logging_utils import setup_logging
from orchestrator.models.state import make_initial_state
from orchestrator.observability import traced_block, tracing_context_for_run

logger = logging.getLogger(__name__)


def _read_prd(args: argparse.Namespace) -> str:
    if args.prd_file:
        return Path(args.prd_file).read_text(encoding="utf-8")
    if args.prd_text:
        return args.prd_text
    raise ValueError("either --prd-file or --prd-text is required")


async def _run(args: argparse.Namespace, app: OrchestratorApp) -> int:
    graph = build_graph(app)
    prd_markdown = _read_prd(args)
    initial_state = make_initial_state(args.repo_url, prd_markdown)
    config = {"configurable": {"thread_id": args.thread_id}}
    logger.info("run_started", extra={"repo_url": args.repo_url})
    tracing_cm = tracing_context_for_run(
        config=app.config,
        client=app.langsmith_client,
        thread_id=args.thread_id,
        repo_url=args.repo_url,
    )
    with tracing_cm:
        try:
            async with traced_block(
                enabled=app.langsmith_client is not None,
                name="orchestrator.run",
                run_type="chain",
                inputs={
                    "repo_url": args.repo_url,
                    "thread_id": args.thread_id,
                    "prd_char_count": len(prd_markdown),
                },
                metadata={
                    "command": "run",
                    "thread_id": args.thread_id,
                },
                tags=["autogen", "orchestrator", "run"],
                client=app.langsmith_client,
            ) as root_trace:
                result = await graph.ainvoke(initial_state, config=config)
                if root_trace is not None:
                    root_trace.end(
                        outputs={
                            "run_id": result.get("run_id"),
                            "run_status": result.get("run_status"),
                            "release_decision": result.get("release_decision"),
                        }
                    )
        except Exception as exc:
            failure_state = await _handle_run_failure(
                graph=graph,
                app=app,
                config=config,
                error=exc,
            )
            logger.exception("run_failed", extra={"repo_url": args.repo_url, "thread_id": args.thread_id})
            print(
                json.dumps(
                    {
                        "thread_id": args.thread_id,
                        "run_id": failure_state.get("run_id"),
                        "run_branch": failure_state.get("run_branch"),
                        "run_status": "FAILED",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1
    logger.info(
        "run_completed",
        extra={
            "repo_url": args.repo_url,
            "run_id": result.get("run_id"),
            "run_status": result.get("run_status"),
            "release_decision": result.get("release_decision"),
        },
    )
    print(
        json.dumps(
            {
                "thread_id": args.thread_id,
                "run_id": result.get("run_id"),
                "run_branch": result.get("run_branch"),
                "run_status": result.get("run_status"),
                "release_decision": result.get("release_decision"),
                "release_decision_path": result.get("release_decision_path"),
                "usage_summary": result.get("usage_summary"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _snapshot_values(snapshot: Any) -> dict[str, Any]:
    return dict(getattr(snapshot, "values", None) or {})


async def _select_resume_snapshot(graph, config: dict[str, object]):
    latest_snapshot = await graph.aget_state(config)
    latest_state = _snapshot_values(latest_snapshot)
    thread_id = config["configurable"]["thread_id"]
    if not latest_state:
        raise RuntimeError(f"no checkpoint state found for thread_id={thread_id}")
    if getattr(latest_snapshot, "next", ()):
        return latest_snapshot
    if latest_state.get("run_status") == "FAILED":
        async for snapshot in graph.aget_state_history(config):
            if getattr(snapshot, "next", ()):
                return snapshot
        raise RuntimeError(f"thread_id={thread_id} is FAILED but no resumable checkpoint was found")
    raise RuntimeError(
        f"thread_id={thread_id} is not resumable from status {latest_state.get('run_status')!r}"
    )


def _runtime_container_spec(
    app: OrchestratorApp,
    state: dict[str, Any],
    *,
    workspace_id: str,
    key: str,
) -> dict[str, Any]:
    if key == "planning":
        return {
            "image": app.config.dev_image,
            "role": "planning",
            "labels": {
                "autogen.run_id": state["run_id"],
                "autogen.workspace_id": workspace_id,
                "autogen.workspace_kind": "planning",
                "autogen.role": "planning",
                "autogen.cycle": str(state["cycle_no"]),
            },
        }
    if key == "stage_dev":
        return {
            "image": app.config.dev_image,
            "role": "stage-dev",
            "labels": {
                "autogen.run_id": state["run_id"],
                "autogen.workspace_id": workspace_id,
                "autogen.workspace_kind": "stage-dev",
                "autogen.role": "stage-dev",
                "autogen.stage": str(state["stage_no"]),
            },
        }
    if key == "publisher":
        return {
            "image": app.config.dev_image,
            "role": "release-publisher",
            "labels": {
                "autogen.run_id": state["run_id"],
                "autogen.workspace_id": workspace_id,
                "autogen.workspace_kind": "release-publisher",
                "autogen.role": "release-publisher",
                "autogen.release": str(state["release_no"]),
            },
        }
    if key in {"compliance", "qa", "e2e"}:
        return {
            "image": app.config.e2e_image if key == "e2e" else app.config.dev_image,
            "role": key,
            "labels": {
                "autogen.run_id": state["run_id"],
                "autogen.workspace_id": workspace_id,
                "autogen.workspace_kind": f"release-{key}",
                "autogen.role": key,
                "autogen.release": str(state["release_no"]),
            },
        }
    raise RuntimeError(f"cannot restore unknown runtime workspace key: {key}")


async def _restore_active_containers(
    app: OrchestratorApp,
    state: dict[str, Any],
) -> dict[str, str]:
    restored: dict[str, str] = {}
    created_container_ids: list[str] = []
    active_workspaces = dict(state.get("active_workspaces", {}))
    active_containers = dict(state.get("active_containers", {}))
    try:
        for key, workspace_id in active_workspaces.items():
            prior_container_id = active_containers.get(key)
            if prior_container_id:
                resolved_container_id = await app.docker_manager.resolve_container_id(prior_container_id)
                if resolved_container_id:
                    restored[key] = resolved_container_id
                    continue

            container_name = app.container_name(state["run_id"], workspace_id)
            existing_container_id = await app.docker_manager.resolve_container_id(container_name)
            if existing_container_id:
                restored[key] = existing_container_id
                continue

            workspace = app.workspace_manager.load_workspace(
                run_id=state["run_id"],
                workspace_id=workspace_id,
            )
            spec = _runtime_container_spec(app, state, workspace_id=workspace_id, key=key)
            container = await app.docker_manager.create_container(
                image=spec["image"],
                name=container_name,
                workspace_view=workspace,
                role=spec["role"],
                labels=spec["labels"],
            )
            created_container_ids.append(container.container_id)
            restored[key] = container.container_id
    except Exception:
        for container_id in created_container_ids:
            try:
                await app.docker_manager.remove_container(container_id)
            except Exception:
                logger.exception(
                    "failed_to_cleanup_container_after_restore_error",
                    extra={"container_id": container_id},
                )
        raise
    return restored


async def _resume(args: argparse.Namespace, app: OrchestratorApp) -> int:
    graph = build_graph(app)
    base_config = {"configurable": {"thread_id": args.thread_id}}
    resume_snapshot = await _select_resume_snapshot(graph, base_config)
    resume_state = _snapshot_values(resume_snapshot)
    restored_containers = await _restore_active_containers(app, resume_state)
    resume_config = await graph.aupdate_state(
        resume_snapshot.config,
        {
            "active_containers": restored_containers,
            "event_log": [
                {
                    "event": "resume_run",
                    "checkpoint_id": resume_snapshot.config["configurable"].get("checkpoint_id"),
                    "at": now_utc(),
                }
            ],
        },
    )
    tracing_cm = tracing_context_for_run(
        config=app.config,
        client=app.langsmith_client,
        thread_id=args.thread_id,
        repo_url=resume_state.get("repo_url"),
    )
    logger.info(
        "resume_started",
        extra={
            "thread_id": args.thread_id,
            "run_id": resume_state.get("run_id"),
            "resume_checkpoint_id": resume_snapshot.config["configurable"].get("checkpoint_id"),
            "pending_nodes": list(getattr(resume_snapshot, "next", ())),
        },
    )
    with tracing_cm:
        try:
            async with traced_block(
                enabled=app.langsmith_client is not None,
                name="orchestrator.resume",
                run_type="chain",
                inputs={
                    "thread_id": args.thread_id,
                    "run_id": resume_state.get("run_id"),
                    "resume_checkpoint_id": resume_snapshot.config["configurable"].get("checkpoint_id"),
                },
                metadata={
                    "command": "resume",
                    "thread_id": args.thread_id,
                },
                tags=["autogen", "orchestrator", "resume"],
                client=app.langsmith_client,
            ) as root_trace:
                result = await graph.ainvoke(None, config=resume_config)
                if root_trace is not None:
                    root_trace.end(
                        outputs={
                            "run_id": result.get("run_id"),
                            "run_status": result.get("run_status"),
                            "release_decision": result.get("release_decision"),
                        }
                    )
        except Exception as exc:
            failure_state = await _handle_run_failure(
                graph=graph,
                app=app,
                config=resume_config,
                error=exc,
            )
            logger.exception("resume_failed", extra={"thread_id": args.thread_id})
            print(
                json.dumps(
                    {
                        "thread_id": args.thread_id,
                        "run_id": failure_state.get("run_id"),
                        "run_branch": failure_state.get("run_branch"),
                        "run_status": "FAILED",
                        "error": str(exc),
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 1
    logger.info(
        "resume_completed",
        extra={
            "thread_id": args.thread_id,
            "run_id": result.get("run_id"),
            "run_status": result.get("run_status"),
            "release_decision": result.get("release_decision"),
        },
    )
    print(
        json.dumps(
            {
                "thread_id": args.thread_id,
                "run_id": result.get("run_id"),
                "run_branch": result.get("run_branch"),
                "run_status": result.get("run_status"),
                "release_decision": result.get("release_decision"),
                "release_decision_path": result.get("release_decision_path"),
                "usage_summary": result.get("usage_summary"),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


async def _handle_run_failure(
    *,
    graph,
    app: OrchestratorApp,
    config: dict[str, object],
    error: Exception,
) -> dict[str, object]:
    try:
        snapshot = await graph.aget_state(config)
        state = dict(snapshot.values or {})
    except Exception:
        logger.exception("failed_to_read_state_after_run_error")
        return {}

    for container_id in dict(state.get("active_containers", {})).values():
        try:
            await app.docker_manager.remove_container(container_id)
        except Exception:
            logger.exception("failed_to_remove_container_after_run_error", extra={"container_id": container_id})

    updates = {
        "active_containers": {},
        "run_status": "FAILED",
        "last_error": {
            "type": type(error).__name__,
            "message": str(error),
            "at": now_utc(),
        },
        "event_log": [
            {
                "event": "end_failure",
                "error_type": type(error).__name__,
                "at": now_utc(),
            }
        ],
    }
    try:
        await graph.aupdate_state(config, updates, as_node="end_failure")
    except Exception:
        logger.exception("failed_to_persist_failure_state")

    state.update(updates)
    return state


def _draw_graph_ascii(args: argparse.Namespace, app: OrchestratorApp) -> int:
    graph = build_graph(app)
    print(graph.get_graph().draw_ascii())
    logger.info("graph_ascii_rendered")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Autogen v1 orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Execute one orchestrator run")
    run_parser.add_argument("--repo-url", required=True, help="Git repository URL")
    run_parser.add_argument("--prd-file", help="Path to a markdown PRD file")
    run_parser.add_argument("--prd-text", help="Inline PRD markdown")
    run_parser.add_argument("--thread-id", required=True, help="LangGraph thread id for checkpointing")

    resume_parser = subparsers.add_parser(
        "resume",
        help="Resume a failed or interrupted orchestrator run from its latest resumable checkpoint",
    )
    resume_parser.add_argument("--thread-id", required=True, help="LangGraph thread id for checkpointing")

    graph_parser = subparsers.add_parser("graph", help="Render the orchestrator graph")
    graph_parser.add_argument(
        "--thread-id",
        default="graph-preview",
        help="Thread id used only for logging and checkpointer context",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = build_app()
    setup_logging(
        logs_dir=app.config.logs_dir,
        thread_id=args.thread_id,
        command=args.command,
    )
    try:
        if args.command == "run":
            return asyncio.run(_run(args, app))
        if args.command == "resume":
            return asyncio.run(_resume(args, app))
        if args.command == "graph":
            return _draw_graph_ascii(args, app)
        parser.print_help(sys.stderr)
        return 1
    finally:
        if app.langsmith_client is not None:
            app.langsmith_client.flush()
