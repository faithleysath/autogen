from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from contextlib import nullcontext
from pathlib import Path

from orchestrator.app import OrchestratorApp, build_app
from orchestrator.graph.builder import build_graph
from orchestrator.logging_utils import setup_logging
from orchestrator.models.state import make_initial_state
from orchestrator.observability import trace, traced_block, tracing_context_for_run

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
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


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
        if args.command == "graph":
            return _draw_graph_ascii(args, app)
        parser.print_help(sys.stderr)
        return 1
    finally:
        if app.langsmith_client is not None:
            app.langsmith_client.flush()
