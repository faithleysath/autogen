from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

import httpx
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
_STAGE_DEV_BROWSER_COMMAND_MARKERS = (
    "bun run test:e2e",
    "playwright test",
    "bunx playwright",
    "bun x playwright",
    "npx playwright",
    "cypress run",
    "cypress open",
)
_STAGE_DEV_BROWSER_EXECUTION_HINTS = (
    " passes",
    " succeeds",
    " successful",
    " runs successfully",
    " execute ",
    " executes",
    " executed",
    " running ",
    " verify by running",
)
_STAGE_DEV_BROWSER_NON_EXECUTION_HINTS = (
    " configured",
    " documented",
    " exists",
    " present",
    " added",
    " declared",
    " references",
    " usage",
    " script",
    " config",
)


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


def _first_present(meta: dict[str, Any], *fields: str) -> Any:
    for field in fields:
        value = meta.get(field)
        if value not in (None, ""):
            return value
    return None


def _contains_stage_dev_browser_execution(text: str) -> bool:
    normalized = text.strip().lower()
    if not any(marker in normalized for marker in _STAGE_DEV_BROWSER_COMMAND_MARKERS):
        return False
    if any(hint in normalized for hint in _STAGE_DEV_BROWSER_EXECUTION_HINTS):
        return True
    if any(hint in normalized for hint in _STAGE_DEV_BROWSER_NON_EXECUTION_HINTS):
        return False
    return True


def _validate_stage_dev_boundaries(raw_stages: list[dict[str, Any]], *, path: str) -> None:
    violations: list[str] = []
    for index, raw in enumerate(raw_stages, start=1):
        stage_label = str(raw.get("stage_id") or f"stage-{index:03d}")
        for criterion in raw.get("exit_criteria", []):
            criterion_text = str(criterion)
            if _contains_stage_dev_browser_execution(criterion_text):
                violations.append(
                    f"{stage_label} exit_criteria assigns browser execution to stage-dev: {criterion_text}"
                )
    if violations:
        joined = "\n".join(f"- {item}" for item in violations)
        raise RuntimeError(
            f"{path} violates the stage-dev/browser boundary.\n"
            "Stage plans may author repo-owned E2E assets in stage-dev, but actual browser execution must stay in the release e2e role.\n"
            f"{joined}"
        )


def _normalize_execution_contract_meta(
    meta: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    normalized = dict(meta)
    normalized["kind"] = str(_first_present(meta, "kind") or "execution_contract")
    normalized["run_id"] = str(run_id)
    normalized["role"] = str(_first_present(meta, "role") or "architect")
    normalized["created_at"] = _control_timestamp(meta)
    return normalized


def _normalize_architecture_plan_meta(
    meta: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    stages = [dict(stage) for stage in meta.get("stages", [])]
    normalized = dict(meta)
    normalized["kind"] = str(_first_present(meta, "kind") or "architecture_plan")
    normalized["run_id"] = str(run_id)
    normalized["role"] = str(_first_present(meta, "role") or "architect")
    normalized["created_at"] = _control_timestamp(meta)
    normalized["stage_count"] = len(stages)
    normalized["stages"] = stages
    return normalized


def _normalize_e2e_plan_meta(
    meta: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    scenarios = [dict(item) for item in meta.get("scenarios", [])]
    normalized = dict(meta)
    normalized["kind"] = str(_first_present(meta, "kind") or "e2e_plan")
    normalized["run_id"] = str(run_id)
    normalized["role"] = str(_first_present(meta, "role") or "architect")
    normalized["created_at"] = _control_timestamp(meta)
    normalized["scenario_count"] = len(scenarios)
    normalized["scenarios"] = scenarios
    return normalized


def _parse_tool_arguments(raw_args: str, *, tool_name: str) -> dict[str, Any]:
    candidate = raw_args or "{}"
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{tool_name} arguments must be valid JSON. "
            f"Parse error: {exc.msg} at line {exc.lineno} column {exc.colno}."
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{tool_name} arguments must decode to a JSON object.")
    return parsed


def _invalid_tool_call_output(*, item, error: str) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": getattr(item, "call_id", None) or getattr(item, "id"),
        "output": json.dumps(
            {
                "error": error,
                "error_type": "InvalidToolArguments",
            },
            ensure_ascii=False,
        ),
    }


def _control_timestamp(meta: dict[str, Any]) -> str:
    value = _first_present(meta, "created_at")
    if value is not None:
        return str(value)
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_stage_gate_meta(
    meta: dict[str, Any],
    *,
    state: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    return {
        "kind": str(_first_present(meta, "kind") or "gate_decision"),
        "run_id": str(_first_present(meta, "run_id") or state["run_id"]),
        "cycle": _first_present(meta, "cycle", "cycle_no") or state["cycle_no"],
        "stage": _first_present(meta, "stage", "stage_no", "stage_id") or state["stage_no"],
        "attempt": _first_present(meta, "attempt", "attempt_no") or state["attempt_no"],
        "role": str(_first_present(meta, "role") or "stage_gate"),
        "created_at": _control_timestamp(meta),
        "decision": str(_first_present(meta, "decision", "verdict", "result") or decision),
        "status": str(_first_present(meta, "status") or ("FAIL" if decision == "FAIL" else "PASS")),
    }


def _normalize_review_meta(
    meta: dict[str, Any],
    *,
    state: dict[str, Any],
    role: str,
    verdict: str,
) -> dict[str, Any]:
    return {
        "kind": str(_first_present(meta, "kind") or f"{role}_report"),
        "run_id": str(_first_present(meta, "run_id") or state["run_id"]),
        "release": _first_present(meta, "release", "release_no") or state["release_no"],
        "role": str(_first_present(meta, "role") or role),
        "created_at": _control_timestamp(meta),
        "candidate_code_sha": str(
            _first_present(meta, "candidate_code_sha", "code_sha", "commit_sha")
            or state["candidate_code_sha"]
        ),
        "status": str(_first_present(meta, "status") or verdict),
        "verdict": str(_first_present(meta, "verdict", "decision", "result") or verdict),
    }


def _normalize_release_decision_meta(
    meta: dict[str, Any],
    *,
    state: dict[str, Any],
    decision: str,
) -> dict[str, Any]:
    return {
        "kind": str(_first_present(meta, "kind") or "release_decision"),
        "run_id": str(_first_present(meta, "run_id") or state["run_id"]),
        "release": _first_present(meta, "release", "release_no") or state["release_no"],
        "role": str(_first_present(meta, "role") or "release_gate"),
        "created_at": _control_timestamp(meta),
        "decision": str(_first_present(meta, "decision", "verdict", "result") or decision),
    }


def _normalize_rework_summary_meta(
    meta: dict[str, Any],
    *,
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "kind": str(_first_present(meta, "kind") or "rework_summary"),
        "run_id": str(_first_present(meta, "run_id") or state["run_id"]),
        "release": _first_present(meta, "release", "release_no") or state["release_no"],
        "role": str(_first_present(meta, "role") or "release_gate"),
        "created_at": _control_timestamp(meta),
    }


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
            client_kwargs = {"api_key": config.openai_api_key}
            if config.openai_base_url:
                client_kwargs["base_url"] = config.openai_base_url
            openai_client = AsyncOpenAI(**client_kwargs)
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
                include_write_tools=role in {"architect", "developer", "e2e"},
            ).specs()
        )
        specs.extend(ArtifactToolset(context, self._artifact_service).specs())
        specs.extend(BashToolset(context, self._docker, self._git, self._config).specs())
        specs.extend(GitReadToolset(context, self._git).specs())
        specs.extend(TaskToolset(context, self._background_tasks).specs())
        specs.append(self._make_submit_tool(result_schema))
        return {spec.name: spec for spec in specs}

    def _is_retryable_error(self, exc: Exception) -> bool:
        if isinstance(exc, TimeoutError):
            return True
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
                request_kwargs = {
                    **request_kwargs,
                    "timeout": httpx.Timeout(self._config.openai_response_timeout_seconds),
                }
                response = await self._client.responses.create(**request_kwargs)
                async with asyncio.timeout(self._config.openai_response_timeout_seconds):
                    final_response = await self._coerce_response(response)
                usage.add_response(final_response, pricing_by_model=self._config.model_pricing)
                return final_response
            except Exception as exc:
                if attempt >= MAX_RESPONSE_RETRIES - 1 or not self._is_retryable_error(exc):
                    raise
                usage.add_retry()
                delay = self._retry_delay_seconds(exc, attempt)
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

    async def _create_chat_completion_with_retry(
        self,
        *,
        usage: RoleUsage,
        role: str,
        model: str,
        request_kwargs: dict[str, Any],
    ):
        for attempt in range(MAX_RESPONSE_RETRIES):
            try:
                response = await self._client.chat.completions.create(
                    **{
                        **request_kwargs,
                        "timeout": httpx.Timeout(self._config.openai_response_timeout_seconds),
                    }
                )
                usage.add_response(response, pricing_by_model=self._config.model_pricing)
                return response
            except Exception as exc:
                if attempt >= MAX_RESPONSE_RETRIES - 1 or not self._is_retryable_error(exc):
                    raise
                usage.add_retry()
                delay = self._retry_delay_seconds(exc, attempt)
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

    async def _coerce_response(self, response):
        if hasattr(response, "output") and hasattr(response, "id"):
            return response
        if hasattr(response, "__aiter__"):
            final_response = None
            try:
                async for event in response:
                    if getattr(event, "type", None) == "response.completed":
                        final_response = event.response
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    maybe_awaitable = close()
                    if hasattr(maybe_awaitable, "__await__"):
                        await maybe_awaitable
            if final_response is None:
                raise RuntimeError("response stream ended without a response.completed event")
            return final_response
        return response

    def _normalize_response_input(self, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, str):
            return [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": payload}],
                }
            ]
        raise TypeError(f"unsupported response input payload type: {type(payload).__name__}")

    def _build_chat_messages(self, *, system_prompt: str, user_prompt: str) -> list[dict[str, Any]]:
        combined_prompt = (
            "Follow the role instructions exactly.\n\n"
            "[Role Instructions]\n"
            f"{system_prompt}\n\n"
            "[Task Payload]\n"
            f"{user_prompt}"
        )
        return [{"role": "user", "content": combined_prompt}]

    def _retry_delay_seconds(self, exc: Exception, attempt: int) -> float:
        message = str(exc)
        normalized_message = message.lower()
        is_rate_limited = (
            getattr(exc, "status_code", None) == 429
            or "429" in normalized_message
            or "rate limit" in normalized_message
            or "速率限制" in message
        )
        if is_rate_limited:
            return min(30.0, (2**attempt) * 4.0 + random.uniform(0.0, 1.0))
        return min(8.0, (2**attempt) + random.uniform(0.0, 0.5))

    def _history_items_from_response(self, response) -> list[dict[str, Any]]:
        history_items: list[dict[str, Any]] = []
        for item in getattr(response, "output", []):
            if getattr(item, "type", None) != "function_call":
                continue
            history_items.append(
                {
                    "type": "function_call",
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                }
            )
        return history_items

    async def _execute_tool_call(
        self,
        *,
        role: str,
        workspace,
        tools: dict[str, ToolSpec],
        item,
        parsed_args: dict[str, Any],
    ) -> dict[str, Any]:
        tool_spec = tools[item.name]
        logger.info(
            "role_tool_call",
            extra={
                "role": role,
                "tool_name": item.name,
                "workspace_id": workspace.workspace_id,
                "tool_args_preview": json.dumps(parsed_args, ensure_ascii=False)[:1000],
            },
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
            try:
                tool_result = await tool_spec.handler(parsed_args)
            except Exception as exc:
                tool_result = {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                }
                logger.warning(
                    "role_tool_call_failed",
                    extra={
                        "role": role,
                        "tool_name": item.name,
                        "workspace_id": workspace.workspace_id,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                if tool_trace is not None:
                    tool_trace.end(outputs={"error": tool_result})
            else:
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
        if self._config.openai_api_mode == "chat_completions":
            return await self._run_role_chat_completions(
                role=role,
                model=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                state=state,
                workspace=workspace,
                container_id=container_id,
                tools=tools,
            )
        if self._config.openai_api_mode != "responses":
            raise RuntimeError(f"unsupported OPENAI API mode: {self._config.openai_api_mode}")
        return await self._run_role_responses(
            role=role,
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            state=state,
            workspace=workspace,
            container_id=container_id,
            tools=tools,
        )

    async def _run_role_responses(
        self,
        *,
        role: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        state: dict[str, Any],
        workspace,
        container_id: str,
        tools: dict[str, ToolSpec],
    ) -> dict[str, Any]:
        input_history = self._normalize_response_input(user_prompt)
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
                    "input": input_history,
                    "tools": [spec.to_openai_tool() for spec in tools.values()],
                    "stream": True,
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
                parse_error_outputs: list[dict[str, Any]] = []
                for item in response.output:
                    if getattr(item, "type", None) != "function_call":
                        continue
                    raw_args = item.arguments if isinstance(item.arguments, str) else json.dumps(item.arguments)
                    try:
                        parsed_args = _parse_tool_arguments(raw_args, tool_name=item.name)
                    except ValueError as exc:
                        logger.warning(
                            "role_tool_call_failed",
                            extra={
                                "role": role,
                                "tool_name": item.name,
                                "workspace_id": workspace.workspace_id,
                                "error_type": "InvalidToolArguments",
                                "error": str(exc),
                            },
                        )
                        parse_error_outputs.append(_invalid_tool_call_output(item=item, error=str(exc)))
                        continue
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

                if parse_error_outputs:
                    tool_outputs = parse_error_outputs
                elif not tool_calls:
                    raise RuntimeError(
                        f"{role} did not call submit_result. Final model text:\n{response.output_text}"
                    )
                elif all(tools[item.name].invocation_is_read_only(args) for item, args in tool_calls):
                    tool_outputs = list(
                        await asyncio.gather(
                            *[
                                self._execute_tool_call(
                                    role=role,
                                    workspace=workspace,
                                    tools=tools,
                                    item=item,
                                    parsed_args=args,
                                )
                                for item, args in tool_calls
                            ]
                        )
                    )
                else:
                    tool_outputs = []
                    for item, args in tool_calls:
                        tool_outputs.append(
                            await self._execute_tool_call(
                                role=role,
                                workspace=workspace,
                                tools=tools,
                                item=item,
                                parsed_args=args,
                            )
                        )

                response = await self._create_response_with_retry(
                    usage=usage,
                    role=role,
                    model=model,
                    request_kwargs=self._followup_request_kwargs(
                        model=model,
                        system_prompt=system_prompt,
                        response=response,
                        input_history=input_history,
                        tool_outputs=tool_outputs,
                        tools=tools,
                    ),
                )

        raise RuntimeError(f"{role} exceeded the maximum number of tool iterations")

    async def _run_role_chat_completions(
        self,
        *,
        role: str,
        model: str,
        system_prompt: str,
        user_prompt: str,
        state: dict[str, Any],
        workspace,
        container_id: str,
        tools: dict[str, ToolSpec],
    ) -> dict[str, Any]:
        messages = self._build_chat_messages(system_prompt=system_prompt, user_prompt=user_prompt)
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
            response = await self._create_chat_completion_with_retry(
                usage=usage,
                role=role,
                model=model,
                request_kwargs={
                    "model": model,
                    "messages": messages,
                    "tools": [spec.to_chat_tool() for spec in tools.values()],
                    "tool_choice": "required",
                    "parallel_tool_calls": True,
                    "stream": False,
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
                choice = response.choices[0]
                message = choice.message
                tool_calls = list(getattr(message, "tool_calls", None) or [])
                if not tool_calls:
                    raise RuntimeError(
                        f"{role} did not call submit_result. Final model text:\n{getattr(message, 'content', '')}"
                    )

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": item.id,
                            "type": "function",
                            "function": {
                                "name": item.function.name,
                                "arguments": item.function.arguments,
                            },
                        }
                        for item in tool_calls
                    ],
                }
                if getattr(message, "content", None):
                    assistant_message["content"] = message.content
                messages.append(assistant_message)

                pending_tool_calls: list[tuple[Any, dict[str, Any]]] = []
                parse_error_outputs: list[dict[str, Any]] = []
                for item in tool_calls:
                    raw_args = item.function.arguments if isinstance(item.function.arguments, str) else json.dumps(
                        item.function.arguments
                    )
                    try:
                        parsed_args = _parse_tool_arguments(raw_args, tool_name=item.function.name)
                    except ValueError as exc:
                        logger.warning(
                            "role_tool_call_failed",
                            extra={
                                "role": role,
                                "tool_name": item.function.name,
                                "workspace_id": workspace.workspace_id,
                                "error_type": "InvalidToolArguments",
                                "error": str(exc),
                            },
                        )
                        parse_error_outputs.append(_invalid_tool_call_output(item=item, error=str(exc)))
                        continue
                    if item.function.name == "submit_result":
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
                    pending_tool_calls.append(
                        (
                            SimpleNamespace(
                                name=item.function.name,
                                arguments=item.function.arguments,
                                call_id=item.id,
                            ),
                            parsed_args,
                        )
                    )

                if parse_error_outputs:
                    tool_outputs = parse_error_outputs
                elif all(tools[item.name].invocation_is_read_only(args) for item, args in pending_tool_calls):
                    tool_outputs = list(
                        await asyncio.gather(
                            *[
                                self._execute_tool_call(
                                    role=role,
                                    workspace=workspace,
                                    tools=tools,
                                    item=item,
                                    parsed_args=args,
                                )
                                for item, args in pending_tool_calls
                            ]
                        )
                    )
                else:
                    tool_outputs = []
                    for item, args in pending_tool_calls:
                        tool_outputs.append(
                            await self._execute_tool_call(
                                role=role,
                                workspace=workspace,
                                tools=tools,
                                item=item,
                                parsed_args=args,
                            )
                        )

                for output in tool_outputs:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": output["call_id"],
                            "content": output["output"],
                        }
                    )

                response = await self._create_chat_completion_with_retry(
                    usage=usage,
                    role=role,
                    model=model,
                    request_kwargs={
                        "model": model,
                        "messages": messages,
                        "tools": [spec.to_chat_tool() for spec in tools.values()],
                        "tool_choice": "required",
                        "parallel_tool_calls": True,
                        "stream": False,
                    },
                )

        raise RuntimeError(f"{role} exceeded the maximum number of tool iterations")

    def _followup_request_kwargs(
        self,
        *,
        model: str,
        system_prompt: str,
        response,
        input_history: list[dict[str, Any]],
        tool_outputs: list[dict[str, Any]],
        tools: dict[str, ToolSpec],
    ) -> dict[str, Any]:
        request_kwargs = {
            "model": model,
            "instructions": system_prompt,
            "tools": [spec.to_openai_tool() for spec in tools.values()],
            "stream": True,
        }
        if self._config.openai_stateless_responses:
            input_history.extend(self._history_items_from_response(response))
            input_history.extend(tool_outputs)
            request_kwargs["input"] = input_history
            return request_kwargs

        request_kwargs["previous_response_id"] = response.id
        request_kwargs["input"] = tool_outputs
        return request_kwargs

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
        normalized_execution_contract_meta = _normalize_execution_contract_meta(
            execution_contract.meta,
            run_id=run_id,
        )
        if normalized_execution_contract_meta != execution_contract.meta:
            self._artifact_service.write_artifact(
                workspace,
                result["execution_contract_path"],
                normalized_execution_contract_meta,
                execution_contract.body,
            )
            execution_contract = self._artifact_service.read_artifact(
                workspace,
                result["execution_contract_path"],
            )
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
        normalized_plan_meta = _normalize_architecture_plan_meta(
            plan_doc.meta,
            run_id=run_id,
        )
        if normalized_plan_meta != plan_doc.meta:
            self._artifact_service.write_artifact(
                workspace,
                result["plan_path"],
                normalized_plan_meta,
                plan_doc.body,
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
        _validate_stage_dev_boundaries(
            [dict(stage) for stage in plan_doc.meta.get("stages", [])],
            path=result["plan_path"],
        )

        e2e_plan = self._artifact_service.read_artifact(workspace, result["e2e_plan_path"])
        normalized_e2e_plan_meta = _normalize_e2e_plan_meta(
            e2e_plan.meta,
            run_id=run_id,
        )
        if normalized_e2e_plan_meta != e2e_plan.meta:
            self._artifact_service.write_artifact(
                workspace,
                result["e2e_plan_path"],
                normalized_e2e_plan_meta,
                e2e_plan.body,
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
        normalized_meta = _normalize_stage_gate_meta(
            artifact.meta,
            state=state,
            decision=result["decision"],
        )
        if normalized_meta != artifact.meta:
            self._artifact_service.write_artifact(
                workspace,
                result["gate_path"],
                normalized_meta,
                artifact.body,
            )
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
        prompt_payload = {
            "run_id": state["run_id"],
            "release_no": release_no,
            "candidate_code_sha": state["candidate_code_sha"],
            "execution_contract_path": state["execution_contract_path"],
            "e2e_plan_path": state.get("e2e_plan_path"),
            "required_report_path": report_path,
            "allowed_verdicts": ["PASS", "FAIL", "PARTIAL"],
        }
        if role == "e2e":
            prompt_payload["evidence_dir"] = (
                f"/workspace/.autogen/runs/{state['run_id']}/30-reviews/release-{release_no:03d}/e2e/evidence"
            )
        user_prompt = json.dumps(
            prompt_payload,
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
        normalized_meta = _normalize_review_meta(
            artifact.meta,
            state=state,
            role=role,
            verdict=result["verdict"],
        )
        if normalized_meta != artifact.meta:
            self._artifact_service.write_artifact(
                workspace,
                result["report_path"],
                normalized_meta,
                artifact.body,
            )
            artifact = self._artifact_service.read_artifact(workspace, result["report_path"])
        _require_frontmatter_fields(
            artifact.meta,
            path=result["report_path"],
            fields=["kind", "run_id", "release", "role", "created_at", "candidate_code_sha", "status", "verdict"],
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
                "rework_summary_path": {"type": ["string", "null"]},
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
        rework_exists = workspace.to_backing_path(rework_path).exists()
        if result["decision"] != "REWORK":
            # For PASS, trust the actual artifact presence over any placeholder
            # or echoed path in the structured output.
            result["rework_summary_path"] = rework_path if rework_exists else None
        elif result["rework_summary_path"] == "":
            result["rework_summary_path"] = None
        _require_path(result["decision_path"], decision_path, label="release decision")
        decision_artifact = self._artifact_service.read_artifact(workspace, result["decision_path"])
        normalized_decision_meta = _normalize_release_decision_meta(
            decision_artifact.meta,
            state=state,
            decision=result["decision"],
        )
        if normalized_decision_meta != decision_artifact.meta:
            self._artifact_service.write_artifact(
                workspace,
                result["decision_path"],
                normalized_decision_meta,
                decision_artifact.body,
            )
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
            normalized_rework_meta = _normalize_rework_summary_meta(
                rework_artifact.meta,
                state=state,
            )
            if normalized_rework_meta != rework_artifact.meta:
                self._artifact_service.write_artifact(
                    workspace,
                    result["rework_summary_path"],
                    normalized_rework_meta,
                    rework_artifact.body,
                )
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
