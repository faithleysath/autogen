from __future__ import annotations

from typing import Any

from orchestrator.app import OrchestratorApp
from orchestrator.graph.nodes.common import (
    cleanup_workspace_and_container,
    now_utc,
    publisher_workspace_id,
    review_workspace_id,
    visible_relpath,
)
from orchestrator.models.state import OrchestrationState


async def freeze_release_candidate(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    release_no = state["release_no"] + 1
    workspace_id = publisher_workspace_id(release_no)
    workspace = app.workspace_manager.create_workspace(
        run_id=state["run_id"],
        workspace_id=workspace_id,
        workspace_kind="release-publisher",
    )
    container = await app.docker_manager.create_container(
        image=app.config.dev_image,
        name=app.container_name(state["run_id"], workspace_id),
        workspace_view=workspace,
        role="release-publisher",
        labels={
            "autogen.run_id": state["run_id"],
            "autogen.workspace_id": workspace_id,
            "autogen.workspace_kind": "release-publisher",
            "autogen.role": "release-publisher",
            "autogen.release": str(release_no),
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
    candidate_sha = await app.git_service.current_head(
        container_id=container.container_id,
        workspace=workspace,
    )
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    active_workspaces["publisher"] = workspace_id
    active_containers["publisher"] = container.container_id
    return {
        "release_no": release_no,
        "candidate_code_sha": candidate_sha,
        "review_results": {},
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "run_status": "REVIEWING",
        "event_log": [{"event": "freeze_release_candidate", "candidate": candidate_sha, "at": now_utc()}],
    }


async def prepare_review_workspaces(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    for role, image in (
        ("compliance", app.config.dev_image),
        ("qa", app.config.dev_image),
        ("e2e", app.config.e2e_image),
    ):
        workspace_id = review_workspace_id(state["release_no"], role)
        workspace = app.workspace_manager.create_workspace(
            run_id=state["run_id"],
            workspace_id=workspace_id,
            workspace_kind=f"release-{role}",
        )
        container = await app.docker_manager.create_container(
            image=image,
            name=app.container_name(state["run_id"], workspace_id),
            workspace_view=workspace,
            role=role,
            labels={
                "autogen.run_id": state["run_id"],
                "autogen.workspace_id": workspace_id,
                "autogen.workspace_kind": f"release-{role}",
                "autogen.role": role,
                "autogen.release": str(state["release_no"]),
            },
        )
        await app.git_service.clone_repo(
            container_id=container.container_id,
            workspace=workspace,
            repo_url=state["repo_url"],
        )
        await app.git_service.checkout_detached(
            container_id=container.container_id,
            workspace=workspace,
            ref=state["candidate_code_sha"],
        )
        active_workspaces[role] = workspace_id
        active_containers[role] = container.container_id
    return {
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "event_log": [{"event": "prepare_review_workspaces", "release_no": state["release_no"], "at": now_utc()}],
    }


async def run_compliance_review(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    return await _run_review_role("compliance", state, app)


async def run_qa_review(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    return await _run_review_role("qa", state, app)


async def run_e2e_review(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    return await _run_review_role("e2e", state, app)


async def _run_review_role(role: str, state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"][role],
    )
    runner = getattr(app.role_runner, f"run_{role}")
    result = await runner(
        state=state,
        workspace=workspace,
        container_id=state["active_containers"][role],
    )
    return {
        "review_results": {
            role: {
                "workspace_id": state["active_workspaces"][role],
                "candidate_code_sha": state["candidate_code_sha"],
                "report_path": result["report_path"],
                "verdict": result["verdict"],
                "published_commit_sha": None,
            }
        },
        "event_log": [{"event": f"run_{role}_review", "verdict": result["verdict"], "at": now_utc()}],
    }


async def join_review_results(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    del app
    missing = {"compliance", "qa", "e2e"} - set(state["review_results"])
    if missing:
        raise RuntimeError(f"missing review results: {sorted(missing)}")
    return {
        "event_log": [{"event": "join_review_results", "at": now_utc()}],
    }


async def publish_review_reports(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    publisher_workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"]["publisher"],
    )
    publisher_container_id = state["active_containers"]["publisher"]
    updated_results = {role: dict(data) for role, data in state["review_results"].items()}
    for role in ("compliance", "qa", "e2e"):
        review_workspace = app.workspace_manager.load_workspace(
            run_id=state["run_id"],
            workspace_id=state["active_workspaces"][role],
        )
        report_path = updated_results[role]["report_path"]
        app.artifact_service.copy_file(
            review_workspace,
            report_path,
            publisher_workspace,
            report_path,
        )
        await app.git_service.pull_rebase(
            container_id=publisher_container_id,
            workspace=publisher_workspace,
            run_branch=state["run_branch"],
        )
        commit_sha = await app.git_service.commit_paths(
            container_id=publisher_container_id,
            workspace=publisher_workspace,
            message=f"review(release-{state['release_no']:03d}/{role}): add report",
            paths=[visible_relpath(report_path)],
        )
        if commit_sha:
            await app.git_service.push(
                container_id=publisher_container_id,
                workspace=publisher_workspace,
                repo_url=state["repo_url"],
                run_branch=state["run_branch"],
            )
        updated_results[role]["published_commit_sha"] = commit_sha

    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    for role in ("compliance", "qa", "e2e"):
        await cleanup_workspace_and_container(
            app,
            run_id=state["run_id"],
            workspace_id=active_workspaces.get(role),
            container_id=active_containers.get(role),
        )
        active_workspaces.pop(role, None)
        active_containers.pop(role, None)
    return {
        "review_results": updated_results,
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "event_log": [{"event": "publish_review_reports", "at": now_utc()}],
    }


async def run_release_gate(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"]["publisher"],
    )
    result = await app.role_runner.run_release_gate(
        state=state,
        workspace=workspace,
        container_id=state["active_containers"]["publisher"],
    )
    return {
        "release_decision": result["decision"],
        "release_decision_path": result["decision_path"],
        "rework_summary_path": result["rework_summary_path"] or None,
        "event_log": [{"event": "run_release_gate", "decision": result["decision"], "at": now_utc()}],
    }


async def publish_release_decision(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    workspace = app.workspace_manager.load_workspace(
        run_id=state["run_id"],
        workspace_id=state["active_workspaces"]["publisher"],
    )
    paths = [visible_relpath(state["release_decision_path"])]
    if state["release_decision"] == "REWORK" and state.get("rework_summary_path"):
        paths.append(visible_relpath(state["rework_summary_path"]))
    commit_sha = await app.git_service.commit_paths(
        container_id=state["active_containers"]["publisher"],
        workspace=workspace,
        message=f"release(release-{state['release_no']:03d}): {state['release_decision'].lower()}",
        paths=paths,
    )
    if commit_sha:
        await app.git_service.push(
            container_id=state["active_containers"]["publisher"],
            workspace=workspace,
            repo_url=state["repo_url"],
            run_branch=state["run_branch"],
        )
    return {
        "event_log": [{"event": "publish_release_decision", "decision": state["release_decision"], "at": now_utc()}],
    }


async def reset_for_replan(state: OrchestrationState, app: OrchestratorApp) -> dict[str, Any]:
    active_workspaces = dict(state["active_workspaces"])
    active_containers = dict(state["active_containers"])
    await cleanup_workspace_and_container(
        app,
        run_id=state["run_id"],
        workspace_id=active_workspaces.get("publisher"),
        container_id=active_containers.get("publisher"),
    )
    active_workspaces.pop("publisher", None)
    active_containers.pop("publisher", None)
    return {
        "cycle_no": state["cycle_no"] + 1,
        "planned_stages": [],
        "current_stage_plan": None,
        "stage_index": 0,
        "attempt_no": 0,
        "stage_no": 0,
        "active_workspaces": active_workspaces,
        "active_containers": active_containers,
        "run_status": "REWORK",
        "event_log": [{"event": "reset_for_replan", "at": now_utc()}],
    }
