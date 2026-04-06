from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any, Protocol

from langsmith import Client
from openai import AsyncOpenAI
from langsmith.wrappers import wrap_openai

from orchestrator.config import OrchestratorConfig
from orchestrator.models.usage import RoleUsage
from orchestrator.observability import traced_block
from orchestrator.policy.role_policy import build_role_policy
from orchestrator.services.artifact_service import ArtifactService
from orchestrator.services.background_tasks import BackgroundTaskManager
from orchestrator.services.docker_manager import DockerManager
from orchestrator.services.git_service import GitService
from orchestrator.services.prompt_loader import load_role_prompt
from orchestrator.tools.artifact_tools import ArtifactToolset
from orchestrator.tools.base import ToolContext, ToolSpec
from orchestrator.tools.bash_tool import BashToolset
from orchestrator.tools.file_tools import FileToolset
from orchestrator.tools.git_tools import GitReadToolset
from orchestrator.tools.task_tools import TaskToolset


logger = logging.getLogger(__name__)
MAX_RESPONSE_RETRIES = 5


def _require_path(actual_path: str, expected_path: str, *, label: str) -> None:
    if actual_path != expected_path:
        raise RuntimeError(f"{label} path mismatch: expected {expected_path}, got {actual_path}")


def _require_frontmatter_fields(meta: dict[str, Any], *, path: str, fields: list[str]) -> None:
    missing = [field for field in fields if field not in meta]
    if missing:
        raise RuntimeError(f"{path} is missing required frontmatter fields: {', '.join(missing)}")


def _require_frontmatter_value(
    meta: dict[str, Any],
    *,
    path: str,
    field: str,
    expected: Any,
) -> None:
    actual = meta.get(field)
    if actual != expected:
        raise RuntimeError(f"{path} frontmatter field {field!r} expected {expected!r}, got {actual!r}")


class RoleRunnerProtocol(Protocol):
    async def run_architect(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]: ...

    async def run_developer(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]: ...

    async def run_stage_gate(
        self,
        *,
        state: dict[str, Any],
        workspace,
        container_id: str,
        is_final_stage: bool,
    ) -> dict[str, Any]: ...

    async def run_compliance(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]: ...

    async def run_qa(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]: ...

    async def run_e2e(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]: ...

    async def run_release_gate(
        self,
        *,
        state: dict[str, Any],
        workspace,
        container_id: str,
    ) -> dict[str, Any]: ...


class OpenAIRoleRunner:
    def __init__(
        self,
        config: OrchestratorConfig,
        langsmith_client: Client | None,
        artifact_service: ArtifactService,
        docker_manager: DockerManager,
        git_service: GitService,
        background_tasks: BackgroundTaskManager,
    ) -> None:
        self._config = config
        self._langsmith_client = langsmith_client
        self._artifact_service = artifact_service
        self._docker = docker_manager
        self._git = git_service
        self._background_tasks = background_tasks
        if config.openai_api_key:
            openai_client = AsyncOpenAI(api_key=config.openai_api_key)
            if langsmith_client is not None:
                openai_client = wrap_openai(
                    openai_client,
                    tracing_extra={
                        "client": langsmith_client,
                        "tags": ["autogen", "orchestrator", "openai"],
                        "metadata": {"component": "role_runner"},
                    },
                )
            self._client = openai_client
        else:
            self._client = None

    def _load_prompt(self, role: str) -> str:
        return load_role_prompt(role)

    def _make_submit_tool(self, result_schema: dict[str, Any]) -> ToolSpec:
        async def _submit_result(args: dict[str, Any]) -> dict[str, Any]:
            return args

        return ToolSpec(
            name="submit_result",
            description="Call this exactly once when the role has finished all work and is ready to return a structured result.",
            parameters=result_schema,
            handler=_submit_result,
            read_only=True,
        )

    def _build_tools(
        self,
        *,
        role: str,
        workspace,
        container_id: str,
        state: dict[str, Any],
        result_schema: dict[str, Any],
    ) -> dict[str, ToolSpec]:
        if role == "architect":
            policy = build_role_policy(
                role=role,
                run_id=state["run_id"],
                cycle_no=state["cycle_no"],
            )
        elif role == "developer":
            policy = build_role_policy(
                role=role,
                run_id=state["run_id"],
                cycle_no=state["cycle_no"],
            )
        elif role == "stage_gate":
            policy = build_role_policy(
                role=role,
                run_id=state["run_id"],
                cycle_no=state["cycle_no"],
                stage_no=state["stage_no"],
                attempt_no=state["attempt_no"],
            )
        elif role in {"compliance", "qa", "e2e"}:
            policy = build_role_policy(
                role=role,
                run_id=state["run_id"],
                cycle_no=state["cycle_no"],
                release_no=state["release_no"],
            )
        elif role == "release_gate":
            policy = build_role_policy(
                role=role,
                run_id=state["run_id"],
                cycle_no=state["cycle_no"],
                release_no=state["release_no"],
            )
        else:
            raise ValueError(f"unsupported role: {role}")

        context = ToolContext(
            role=role,
            workspace=workspace,
            container_id=container_id,
            policy=policy,
        )
        specs: list[ToolSpec] = []
        specs.extend(
            FileToolset(
                context,
                include_write_tools=role in {"architect", "developer"},
            ).specs()
        )
        specs.extend(ArtifactToolset(context, self._artifact_service).specs())
        specs.extend(BashToolset(context, self._docker, self._git, self._config).specs())
        specs.extend(GitReadToolset(context, self._git).specs())
        specs.extend(TaskToolset(context, self._background_tasks).specs())
        specs.append(self._make_submit_tool(result_schema))
        return {spec.name: spec for spec in specs}

    def _is_retryable_error(self, exc: Exception) -> bool:
        status_code = getattr(exc, "status_code", None)
        if status_code in {408, 409, 429, 500, 502, 503, 504}:
            return True
        response = getattr(exc, "response", None)
        response_status = getattr(response, "status_code", None)
        if response_status in {408, 409, 429, 500, 502, 503, 504}:
            return True
        message = str(exc).lower()
        keywords = ("timeout", "temporar", "connection", "rate limit", "server error", "unavailable")
        return any(keyword in message for keyword in keywords)

    async def _create_response_with_retry(
        self,
        *,
        usage: RoleUsage,
        role: str,
        model: str,
        request_kwargs: dict[str, Any],
    ):
        for attempt in range(MAX_RESPONSE_RETRIES):
            try:
                response = await self._client.responses.create(**request_kwargs)
                usage.add_response(response, pricing_by_model=self._config.model_pricing)
                return response
            except Exception as exc:
                if attempt >= MAX_RESPONSE_RETRIES - 1 or not self._is_retryable_error(exc):
                    raise
                usage.add_retry()
                delay = min(8.0, (2**attempt) + random.uniform(0.0, 0.5))
                logger.warning(
                    "role_model_retry",
                    extra={
                        "role": role,
                        "model": model,
                        "attempt": attempt + 1,
                        "delay_seconds": delay,
                        "error": str(exc),
                    },
                )
                await asyncio.sleep(delay)

    async def _execute_tool_call(
        self,
        *,
        role: str,
        workspace,
        tools: dict[str, ToolSpec],
        item,
    ) -> dict[str, Any]:
        raw_args = item.arguments if isinstance(item.arguments, str) else json.dumps(item.arguments)
        parsed_args = json.loads(raw_args or "{}")
        tool_spec = tools[item.name]
        logger.info(
            "role_tool_call",
            extra={"role": role, "tool_name": item.name, "workspace_id": workspace.workspace_id},
        )
        async with traced_block(
            enabled=self._langsmith_client is not None,
            name=f"tool.{item.name}",
            run_type="tool",
            inputs={"arguments": parsed_args},
            metadata={"role": role, "workspace_id": workspace.workspace_id},
            tags=["autogen", "orchestrator", role, "tool"],
            client=self._langsmith_client,
        ) as tool_trace:
            tool_result = await tool_spec.handler(parsed_args)
            if tool_trace is not None:
                tool_trace.end(
                    outputs={
                        "result_preview": json.dumps(tool_result, ensure_ascii=False)[:4000]
                    }
                )
        return {
            "type": "function_call_output",
            "call_id": item.call_id,
            "output": json.dumps(tool_result, ensure_ascii=False),
        }

    async def _run_role(
        self,
        *,
        role: str,
        model: str,
        user_prompt: str,
        state: dict[str, Any],
        workspace,
        container_id: str,
        result_schema: dict[str, Any],
    ) -> dict[str, Any]:
        if self._client is None:
            raise RuntimeError("OPENAI_API_KEY is required to execute live role agents")

        system_prompt = self._load_prompt(role)
        tools = self._build_tools(
            role=role,
            workspace=workspace,
            container_id=container_id,
            state=state,
            result_schema=result_schema,
        )
        usage = RoleUsage(model=model)
        async with traced_block(
            enabled=self._langsmith_client is not None,
            name=f"role.{role}",
            run_type="chain",
            inputs={
                "role": role,
                "workspace_id": workspace.workspace_id,
                "container_id": container_id,
                "state": {
                    "run_id": state.get("run_id"),
                    "cycle_no": state.get("cycle_no"),
                    "stage_no": state.get("stage_no"),
                    "attempt_no": state.get("attempt_no"),
                    "release_no": state.get("release_no"),
                },
            },
            metadata={
                "role": role,
                "model": model,
                "workspace_id": workspace.workspace_id,
                "container_id": container_id,
            },
            tags=["autogen", "orchestrator", role],
            client=self._langsmith_client,
        ) as role_trace:
            response = await self._create_response_with_retry(
                usage=usage,
                role=role,
                model=model,
                request_kwargs={
                    "model": model,
                    "instructions": system_prompt,
                    "input": user_prompt,
                    "tools": [spec.to_openai_tool() for spec in tools.values()],
                },
            )
            logger.info(
                "role_started",
                extra={
                    "role": role,
                    "model": model,
                    "workspace_id": workspace.workspace_id,
                    "container_id": container_id,
                },
            )

            for step in range(64):
                tool_calls: list[tuple[Any, dict[str, Any]]] = []
                for item in response.output:
                    if getattr(item, "type", None) != "function_call":
                        continue
                    raw_args = item.arguments if isinstance(item.arguments, str) else json.dumps(item.arguments)
                    parsed_args = json.loads(raw_args or "{}")
                    if item.name == "submit_result":
                        parsed_args["usage"] = usage.to_dict()
                        if role_trace is not None:
                            role_trace.end(outputs={"result": parsed_args})
                        logger.info(
                            "role_completed",
                            extra={
                                "role": role,
                                "workspace_id": workspace.workspace_id,
                                "iterations": step + 1,
                                "usage": usage.to_dict(),
                            },
                        )
                        return parsed_args
                    tool_calls.append((item, parsed_args))

                if not tool_calls:
                    raise RuntimeError(
                        f"{role} did not call submit_result. Final model text:\n{response.output_text}"
                    )

                if all(tools[item.name].invocation_is_read_only(args) for item, args in tool_calls):
                    tool_outputs = list(
                        await asyncio.gather(
                            *[
                                self._execute_tool_call(
                                    role=role,
                                    workspace=workspace,
                                    tools=tools,
                                    item=item,
                                )
                                for item, _ in tool_calls
                            ]
                        )
                    )
                else:
                    tool_outputs = []
                    for item, _ in tool_calls:
                        tool_outputs.append(
                            await self._execute_tool_call(
                                role=role,
                                workspace=workspace,
                                tools=tools,
                                item=item,
                            )
                        )

                response = await self._create_response_with_retry(
                    usage=usage,
                    role=role,
                    model=model,
                    request_kwargs={
                        "model": model,
                        "instructions": system_prompt,
                        "previous_response_id": response.id,
                        "input": tool_outputs,
                        "tools": [spec.to_openai_tool() for spec in tools.values()],
                    },
                )

        raise RuntimeError(f"{role} exceeded the maximum number of tool iterations")

    async def run_architect(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]:
        cycle_no = state["cycle_no"]
        run_id = state["run_id"]
        planning_root = f"/workspace/.autogen/runs/{run_id}/10-planning/cycle-{cycle_no:03d}"
        expected_execution_contract_path = f"{planning_root}/execution-contract.md"
        expected_plan_path = f"{planning_root}/architecture-plan.md"
        expected_e2e_plan_path = f"{planning_root}/e2e-plan.md"
        user_prompt = json.dumps(
            {
                "run_id": run_id,
                "repo_url": state["repo_url"],
                "cycle_no": cycle_no,
                "prd_path": f"/workspace/.autogen/runs/{run_id}/00-input/prd.md",
                "previous_execution_contract_path": state.get("execution_contract_path"),
                "previous_rework_summary_path": state.get("rework_summary_path"),
                "required_outputs": {
                    "execution_contract_path": expected_execution_contract_path,
                    "plan_path": expected_plan_path,
                    "e2e_plan_path": expected_e2e_plan_path,
                },
                "artifact_requirements": {
                    "execution_contract_kind": "execution_contract",
                    "plan_kind": "architecture_plan",
                    "plan_frontmatter_fields": ["stage_count", "stages"],
                    "stage_shape": {
                        "stage_id": "string",
                        "goal": "string",
                        "inputs": ["string"],
                        "exit_criteria": ["string"],
                    },
                    "e2e_kind": "e2e_plan",
                    "e2e_frontmatter_fields": ["scenario_count", "scenarios"],
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        schema = {
            "type": "object",
            "properties": {
                "execution_contract_path": {"type": "string"},
                "plan_path": {"type": "string"},
                "e2e_plan_path": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["execution_contract_path", "plan_path", "e2e_plan_path", "summary"],
            "additionalProperties": False,
        }
        result = await self._run_role(
            role="architect",
            model=self._config.model_for_role("architect"),
            user_prompt=user_prompt,
            state=state,
            workspace=workspace,
            container_id=container_id,
            result_schema=schema,
        )
        _require_path(
            result["execution_contract_path"],
            expected_execution_contract_path,
            label="execution contract",
        )
        _require_path(result["plan_path"], expected_plan_path, label="architecture plan")
        _require_path(result["e2e_plan_path"], expected_e2e_plan_path, label="e2e plan")

        execution_contract = self._artifact_service.read_artifact(workspace, result["execution_contract_path"])
        _require_frontmatter_fields(
            execution_contract.meta,
            path=result["execution_contract_path"],
            fields=["kind", "run_id", "role", "created_at"],
        )
        _require_frontmatter_value(
            execution_contract.meta,
            path=result["execution_contract_path"],
            field="run_id",
            expected=run_id,
        )

        plan_doc = self._artifact_service.read_artifact(workspace, result["plan_path"])
        _require_frontmatter_fields(
            plan_doc.meta,
            path=result["plan_path"],
            fields=["kind", "run_id", "role", "created_at", "stage_count", "stages"],
        )
        _require_frontmatter_value(
            plan_doc.meta,
            path=result["plan_path"],
            field="run_id",
            expected=run_id,
        )

        e2e_plan = self._artifact_service.read_artifact(workspace, result["e2e_plan_path"])
        _require_frontmatter_fields(
            e2e_plan.meta,
            path=result["e2e_plan_path"],
            fields=["kind", "run_id", "role", "created_at", "scenario_count", "scenarios"],
        )
        _require_frontmatter_value(
            e2e_plan.meta,
            path=result["e2e_plan_path"],
            field="run_id",
            expected=run_id,
        )
        return result

    async def run_developer(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]:
        stage_plan = state["current_stage_plan"]
        user_prompt = json.dumps(
            {
                "run_id": state["run_id"],
                "cycle_no": state["cycle_no"],
                "stage": stage_plan,
                "execution_contract_path": state["execution_contract_path"],
                "plan_path": state["plan_path"],
                "latest_gate_path": state.get("current_stage_gate_path"),
                "workspace_root": "/workspace",
            },
            ensure_ascii=False,
            indent=2,
        )
        schema = {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "changed_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["summary", "changed_paths"],
            "additionalProperties": False,
        }
        return await self._run_role(
            role="developer",
            model=self._config.model_for_role("developer"),
            user_prompt=user_prompt,
            state=state,
            workspace=workspace,
            container_id=container_id,
            result_schema=schema,
        )

    async def run_stage_gate(
        self,
        *,
        state: dict[str, Any],
        workspace,
        container_id: str,
        is_final_stage: bool,
    ) -> dict[str, Any]:
        gate_path = (
            f"/workspace/.autogen/runs/{state['run_id']}/20-stages/"
            f"stage-{state['stage_no']:03d}/attempt-{state['attempt_no']:03d}/gate-decision.md"
        )
        user_prompt = json.dumps(
            {
                "run_id": state["run_id"],
                "cycle_no": state["cycle_no"],
                "stage": state["current_stage_plan"],
                "attempt_no": state["attempt_no"],
                "execution_contract_path": state["execution_contract_path"],
                "required_gate_path": gate_path,
                "allowed_decisions": ["FAIL", "NEXT_STAGE", "COMPLETE_ALL_STAGES"],
                "is_final_stage": is_final_stage,
            },
            ensure_ascii=False,
            indent=2,
        )
        schema = {
            "type": "object",
            "properties": {
                "decision": {
                    "type": "string",
                    "enum": ["FAIL", "NEXT_STAGE", "COMPLETE_ALL_STAGES"],
                },
                "gate_path": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["decision", "gate_path", "summary"],
            "additionalProperties": False,
        }
        result = await self._run_role(
            role="stage_gate",
            model=self._config.model_for_role("stage_gate"),
            user_prompt=user_prompt,
            state=state,
            workspace=workspace,
            container_id=container_id,
            result_schema=schema,
        )
        _require_path(result["gate_path"], gate_path, label="stage gate artifact")
        artifact = self._artifact_service.read_artifact(workspace, result["gate_path"])
        _require_frontmatter_fields(
            artifact.meta,
            path=result["gate_path"],
            fields=["kind", "run_id", "cycle", "stage", "attempt", "role", "created_at", "decision", "status"],
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["gate_path"],
            field="run_id",
            expected=state["run_id"],
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["gate_path"],
            field="role",
            expected="stage_gate",
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["gate_path"],
            field="decision",
            expected=result["decision"],
        )
        return result

    async def run_compliance(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]:
        return await self._run_review_role(
            role="compliance",
            state=state,
            workspace=workspace,
            container_id=container_id,
        )

    async def run_qa(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]:
        return await self._run_review_role(
            role="qa",
            state=state,
            workspace=workspace,
            container_id=container_id,
        )

    async def run_e2e(self, *, state: dict[str, Any], workspace, container_id: str) -> dict[str, Any]:
        return await self._run_review_role(
            role="e2e",
            state=state,
            workspace=workspace,
            container_id=container_id,
        )

    async def _run_review_role(
        self,
        *,
        role: str,
        state: dict[str, Any],
        workspace,
        container_id: str,
    ) -> dict[str, Any]:
        release_no = state["release_no"]
        report_path = f"/workspace/.autogen/runs/{state['run_id']}/30-reviews/release-{release_no:03d}/{role}/report.md"
        user_prompt = json.dumps(
            {
                "run_id": state["run_id"],
                "release_no": release_no,
                "candidate_code_sha": state["candidate_code_sha"],
                "execution_contract_path": state["execution_contract_path"],
                "e2e_plan_path": state.get("e2e_plan_path"),
                "required_report_path": report_path,
                "allowed_verdicts": ["PASS", "FAIL", "PARTIAL"],
            },
            ensure_ascii=False,
            indent=2,
        )
        schema = {
            "type": "object",
            "properties": {
                "report_path": {"type": "string"},
                "verdict": {"type": "string", "enum": ["PASS", "FAIL", "PARTIAL"]},
                "summary": {"type": "string"},
            },
            "required": ["report_path", "verdict", "summary"],
            "additionalProperties": False,
        }
        result = await self._run_role(
            role=role,
            model=self._config.model_for_role(role),
            user_prompt=user_prompt,
            state=state,
            workspace=workspace,
            container_id=container_id,
            result_schema=schema,
        )
        _require_path(result["report_path"], report_path, label=f"{role} report")
        artifact = self._artifact_service.read_artifact(workspace, result["report_path"])
        _require_frontmatter_fields(
            artifact.meta,
            path=result["report_path"],
            fields=["kind", "run_id", "release", "role", "candidate_code_sha", "status", "verdict"],
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["report_path"],
            field="run_id",
            expected=state["run_id"],
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["report_path"],
            field="role",
            expected=role,
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["report_path"],
            field="candidate_code_sha",
            expected=state["candidate_code_sha"],
        )
        _require_frontmatter_value(
            artifact.meta,
            path=result["report_path"],
            field="verdict",
            expected=result["verdict"],
        )
        result["candidate_code_sha"] = artifact.meta["candidate_code_sha"]
        return result

    async def run_release_gate(
        self,
        *,
        state: dict[str, Any],
        workspace,
        container_id: str,
    ) -> dict[str, Any]:
        release_no = state["release_no"]
        user_prompt = json.dumps(
            {
                "run_id": state["run_id"],
                "release_no": release_no,
                "execution_contract_path": state["execution_contract_path"],
                "review_reports": {
                    role: data["report_path"] for role, data in state["review_results"].items()
                },
                "required_decision_path": f"/workspace/.autogen/runs/{state['run_id']}/40-release/release-{release_no:03d}/decision.md",
                "required_rework_path": f"/workspace/.autogen/runs/{state['run_id']}/50-rework/release-{release_no:03d}/rework-summary.md",
                "allowed_decisions": ["PASS", "REWORK"],
            },
            ensure_ascii=False,
            indent=2,
        )
        schema = {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["PASS", "REWORK"]},
                "decision_path": {"type": "string"},
                "rework_summary_path": {"type": "string"},
                "summary": {"type": "string"},
            },
            "required": ["decision", "decision_path", "rework_summary_path", "summary"],
            "additionalProperties": False,
        }
        result = await self._run_role(
            role="release_gate",
            model=self._config.model_for_role("release_gate"),
            user_prompt=user_prompt,
            state=state,
            workspace=workspace,
            container_id=container_id,
            result_schema=schema,
        )
        decision_path = f"/workspace/.autogen/runs/{state['run_id']}/40-release/release-{release_no:03d}/decision.md"
        rework_path = f"/workspace/.autogen/runs/{state['run_id']}/50-rework/release-{release_no:03d}/rework-summary.md"
        _require_path(result["decision_path"], decision_path, label="release decision")
        decision_artifact = self._artifact_service.read_artifact(workspace, result["decision_path"])
        _require_frontmatter_fields(
            decision_artifact.meta,
            path=result["decision_path"],
            fields=["kind", "run_id", "release", "role", "created_at", "decision"],
        )
        _require_frontmatter_value(
            decision_artifact.meta,
            path=result["decision_path"],
            field="run_id",
            expected=state["run_id"],
        )
        _require_frontmatter_value(
            decision_artifact.meta,
            path=result["decision_path"],
            field="role",
            expected="release_gate",
        )
        _require_frontmatter_value(
            decision_artifact.meta,
            path=result["decision_path"],
            field="decision",
            expected=result["decision"],
        )

        if result["decision"] == "REWORK":
            _require_path(result["rework_summary_path"], rework_path, label="rework summary")
            rework_artifact = self._artifact_service.read_artifact(workspace, result["rework_summary_path"])
            _require_frontmatter_fields(
                rework_artifact.meta,
                path=result["rework_summary_path"],
                fields=["kind", "run_id", "release", "role", "created_at"],
            )
            _require_frontmatter_value(
                rework_artifact.meta,
                path=result["rework_summary_path"],
                field="run_id",
                expected=state["run_id"],
            )
            _require_frontmatter_value(
                rework_artifact.meta,
                path=result["rework_summary_path"],
                field="role",
                expected="release_gate",
            )
        elif result["rework_summary_path"]:
            raise RuntimeError("release_gate returned rework_summary_path for a PASS decision")
        return result
