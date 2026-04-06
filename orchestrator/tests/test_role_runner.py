from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestrator.models.runtime import VISIBLE_ROOT, WorkspaceView
from orchestrator.services.artifact_service import ArtifactService
from orchestrator.services.role_runner import OpenAIRoleRunner
from orchestrator.tools.base import ToolSpec


class RetryableError(RuntimeError):
    status_code = 429


class FakeResponsesApi:
    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self._sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeChatCompletionsApi:
    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self._sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, responses_sequence=None, chat_sequence=None):
        self.responses = FakeResponsesApi(responses_sequence or [])
        self.chat = SimpleNamespace(completions=FakeChatCompletionsApi(chat_sequence or []))


class NeverEndingStream:
    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(3600)
        raise StopAsyncIteration


class RoleRunnerHarness(OpenAIRoleRunner):
    def __init__(
        self,
        client,
        tools,
        *,
        api_mode: str = "responses",
        stateless_responses: bool = False,
        response_timeout_seconds: float = 90,
    ):
        super().__init__(
            SimpleNamespace(
                openai_api_key=None,
                openai_base_url=None,
                openai_api_mode=api_mode,
                openai_stateless_responses=stateless_responses,
                openai_response_timeout_seconds=response_timeout_seconds,
                model_pricing={},
            ),
            None,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )
        self._client = client
        self._tools = tools

    def _load_prompt(self, role: str) -> str:
        return f"prompt for {role}"

    def _build_tools(self, **kwargs):
        del kwargs
        return self._tools


class DummyConfig:
    openai_api_key = None
    openai_base_url = None
    openai_api_mode = "responses"
    openai_stateless_responses = False
    openai_response_timeout_seconds = 90
    model_pricing: dict[str, object] = {}

    def model_for_role(self, role: str) -> str:
        del role
        return "gpt-test"


def _workspace(tmp_path: Path) -> WorkspaceView:
    return WorkspaceView(
        workspace_id="ws-1",
        visible_root=VISIBLE_ROOT,
        backing_root=tmp_path,
        run_id="run-1",
        workspace_kind="stage-dev",
    )


def _response(response_id: str, output, *, input_tokens: int, output_tokens: int):
    return SimpleNamespace(
        id=response_id,
        output=output,
        output_text="",
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _chat_completion(tool_calls, *, input_tokens: int, output_tokens: int, content: str | None = None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls,
                )
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
        ),
    )


def _chat_tool_call(name: str, arguments: str, *, call_id: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


@pytest.mark.anyio
async def test_role_runner_retries_tracks_usage_and_parallelizes_read_only_tools(monkeypatch):
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("orchestrator.services.role_runner.asyncio.sleep", no_sleep)

    async def slow_reader(args):
        await asyncio.to_thread(time.sleep, 0.2)
        return {"value": args["value"]}

    tools = {
        "read_one": ToolSpec(
            name="read_one",
            description="reader one",
            parameters={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
            handler=slow_reader,
            read_only=True,
        ),
        "read_two": ToolSpec(
            name="read_two",
            description="reader two",
            parameters={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
            handler=slow_reader,
            read_only=True,
        ),
    }
    client = FakeClient(
        responses_sequence=[
            RetryableError("rate limited"),
            _response(
                "resp-1",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="read_one",
                        arguments='{"value": 1}',
                        call_id="call-1",
                    ),
                    SimpleNamespace(
                        type="function_call",
                        name="read_two",
                        arguments='{"value": 2}',
                        call_id="call-2",
                    ),
                ],
                input_tokens=10,
                output_tokens=5,
            ),
            _response(
                "resp-2",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="submit_result",
                        arguments='{"summary": "done"}',
                        call_id="call-3",
                    )
                ],
                input_tokens=4,
                output_tokens=2,
            ),
        ]
    )
    runner = RoleRunnerHarness(client, tools)

    started = time.monotonic()
    result = await runner._run_role(
        role="developer",
        model="gpt-test",
        user_prompt="hello",
        state={"run_id": "run-1", "cycle_no": 1},
        workspace=SimpleNamespace(workspace_id="ws-1"),
        container_id="container-1",
        result_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )
    elapsed = time.monotonic() - started

    assert result["summary"] == "done"
    assert result["usage"]["requests"] == 2
    assert result["usage"]["retries"] == 1
    assert result["usage"]["input_tokens"] == 14
    assert result["usage"]["output_tokens"] == 7
    assert elapsed < 0.3
    assert len(client.responses.calls) == 3
    assert client.responses.calls[1]["stream"] is True
    assert client.responses.calls[2]["previous_response_id"] == "resp-1"


@pytest.mark.anyio
async def test_role_runner_can_use_stateless_followups():
    tools = {
        "read_one": ToolSpec(
            name="read_one",
            description="reader one",
            parameters={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
            handler=lambda args: asyncio.sleep(0, result={"value": args["value"]}),
            read_only=True,
        ),
    }
    client = FakeClient(
        responses_sequence=[
            _response(
                "resp-1",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="read_one",
                        arguments='{"value": 1}',
                        call_id="call-1",
                    ),
                ],
                input_tokens=10,
                output_tokens=5,
            ),
            _response(
                "resp-2",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="submit_result",
                        arguments='{"summary": "done"}',
                        call_id="call-2",
                    )
                ],
                input_tokens=4,
                output_tokens=2,
            ),
        ]
    )
    runner = RoleRunnerHarness(client, tools, stateless_responses=True)

    result = await runner._run_role(
        role="developer",
        model="gpt-test",
        user_prompt="hello",
        state={"run_id": "run-1", "cycle_no": 1},
        workspace=SimpleNamespace(workspace_id="ws-1"),
        container_id="container-1",
        result_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )

    assert result["summary"] == "done"
    assert "previous_response_id" not in client.responses.calls[1]
    assert client.responses.calls[1]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "read_one",
            "arguments": '{"value": 1}',
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": '{"value": 1}',
        },
    ]


@pytest.mark.anyio
async def test_role_runner_returns_tool_errors_to_model():
    async def failing_tool(_args):
        raise PermissionError("git command is restricted to read-only subcommands")

    tools = {
        "run_command": ToolSpec(
            name="run_command",
            description="runner",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            handler=failing_tool,
            read_only=True,
        ),
    }
    client = FakeClient(
        responses_sequence=[
            _response(
                "resp-1",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="run_command",
                        arguments="{}",
                        call_id="call-1",
                    ),
                ],
                input_tokens=10,
                output_tokens=5,
            ),
            _response(
                "resp-2",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="submit_result",
                        arguments='{"summary": "recovered"}',
                        call_id="call-2",
                    )
                ],
                input_tokens=4,
                output_tokens=2,
            ),
        ]
    )
    runner = RoleRunnerHarness(client, tools, stateless_responses=True)

    result = await runner._run_role(
        role="architect",
        model="gpt-test",
        user_prompt="hello",
        state={"run_id": "run-1", "cycle_no": 1},
        workspace=SimpleNamespace(workspace_id="ws-1"),
        container_id="container-1",
        result_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )

    assert result["summary"] == "recovered"
    assert client.responses.calls[1]["input"] == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
        {
            "type": "function_call",
            "call_id": "call-1",
            "name": "run_command",
            "arguments": "{}",
        },
        {
            "type": "function_call_output",
            "call_id": "call-1",
            "output": (
                '{"error": "git command is restricted to read-only subcommands", '
                '"error_type": "PermissionError"}'
            ),
        },
    ]


@pytest.mark.anyio
async def test_role_runner_retries_when_stream_never_completes():
    client = FakeClient(
        responses_sequence=[
            NeverEndingStream(),
            _response(
                "resp-2",
                [
                    SimpleNamespace(
                        type="function_call",
                        name="submit_result",
                        arguments='{"summary": "done after retry"}',
                        call_id="call-1",
                    )
                ],
                input_tokens=4,
                output_tokens=2,
            ),
        ]
    )
    runner = RoleRunnerHarness(
        client,
        tools={},
        response_timeout_seconds=0.01,
    )

    result = await runner._run_role(
        role="developer",
        model="gpt-test",
        user_prompt="hello",
        state={"run_id": "run-1", "cycle_no": 1},
        workspace=SimpleNamespace(workspace_id="ws-1"),
        container_id="container-1",
        result_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )

    assert result["summary"] == "done after retry"
    assert result["usage"]["requests"] == 1
    assert result["usage"]["retries"] == 1
    assert len(client.responses.calls) == 2


@pytest.mark.anyio
async def test_role_runner_can_use_chat_completions_tool_loop():
    tools = {
        "read_one": ToolSpec(
            name="read_one",
            description="reader one",
            parameters={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
            handler=lambda args: asyncio.sleep(0, result={"value": args["value"]}),
            read_only=True,
        ),
    }
    client = FakeClient(
        chat_sequence=[
            _chat_completion(
                [
                    _chat_tool_call("read_one", '{"value": 1}', call_id="call-1"),
                ],
                input_tokens=10,
                output_tokens=5,
            ),
            _chat_completion(
                [
                    _chat_tool_call("submit_result", '{"summary": "done"}', call_id="call-2"),
                ],
                input_tokens=4,
                output_tokens=2,
            ),
        ]
    )
    runner = RoleRunnerHarness(client, tools, api_mode="chat_completions")

    result = await runner._run_role(
        role="developer",
        model="gpt-test",
        user_prompt="hello",
        state={"run_id": "run-1", "cycle_no": 1},
        workspace=SimpleNamespace(workspace_id="ws-1"),
        container_id="container-1",
        result_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
    )

    assert result["summary"] == "done"
    assert result["usage"]["requests"] == 2
    assert result["usage"]["input_tokens"] == 14
    assert result["usage"]["output_tokens"] == 7
    assert client.chat.completions.calls[0]["stream"] is False
    assert client.chat.completions.calls[0]["tool_choice"] == "required"
    assert "prompt for developer" in client.chat.completions.calls[0]["messages"][0]["content"]
    assert "hello" in client.chat.completions.calls[0]["messages"][0]["content"]
    assert client.chat.completions.calls[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": '{"value": 1}',
    }


@pytest.mark.anyio
async def test_run_stage_gate_normalizes_legacy_frontmatter(tmp_path):
    workspace = _workspace(tmp_path)
    artifact_service = ArtifactService()
    gate_path = "/workspace/.autogen/runs/run-1/20-stages/stage-001/attempt-001/gate-decision.md"
    artifact_service.write_artifact(
        workspace,
        gate_path,
        {
            "kind": "gate_decision",
            "stage_id": "stage-001",
            "attempt_no": 1,
            "decision": "NEXT_STAGE",
        },
        "Stage 1 passed.\n",
    )
    runner = OpenAIRoleRunner(
        DummyConfig(),
        None,
        artifact_service,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    async def fake_run_role(**kwargs):
        del kwargs
        return {"decision": "NEXT_STAGE", "gate_path": gate_path, "summary": "ok"}

    runner._run_role = fake_run_role  # type: ignore[method-assign]

    result = await runner.run_stage_gate(
        state={
            "run_id": "run-1",
            "cycle_no": 1,
            "stage_no": 1,
            "attempt_no": 1,
            "current_stage_plan": {"stage_id": "stage-1", "goal": "goal"},
            "execution_contract_path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
        },
        workspace=workspace,
        container_id="container-1",
        is_final_stage=False,
    )

    assert result["decision"] == "NEXT_STAGE"
    artifact = artifact_service.read_artifact(workspace, gate_path)
    assert artifact.meta["cycle"] == 1
    assert artifact.meta["stage"] == "stage-001"
    assert artifact.meta["attempt"] == 1
    assert artifact.meta["run_id"] == "run-1"
    assert artifact.meta["role"] == "stage_gate"
    assert artifact.meta["status"] == "PASS"


@pytest.mark.anyio
async def test_run_compliance_normalizes_sparse_report_frontmatter(tmp_path):
    workspace = _workspace(tmp_path)
    artifact_service = ArtifactService()
    report_path = "/workspace/.autogen/runs/run-1/30-reviews/release-001/compliance/report.md"
    artifact_service.write_artifact(
        workspace,
        report_path,
        {
            "kind": "compliance_report",
        },
        "Scope matches.\n",
    )
    runner = OpenAIRoleRunner(
        DummyConfig(),
        None,
        artifact_service,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    async def fake_run_role(**kwargs):
        del kwargs
        return {"report_path": report_path, "verdict": "PASS", "summary": "ok"}

    runner._run_role = fake_run_role  # type: ignore[method-assign]

    result = await runner.run_compliance(
        state={
            "run_id": "run-1",
            "release_no": 1,
            "candidate_code_sha": "abc123",
            "execution_contract_path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
            "e2e_plan_path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/e2e-plan.md",
        },
        workspace=workspace,
        container_id="container-1",
    )

    assert result["verdict"] == "PASS"
    assert result["candidate_code_sha"] == "abc123"
    artifact = artifact_service.read_artifact(workspace, report_path)
    assert artifact.meta["release"] == 1
    assert artifact.meta["role"] == "compliance"
    assert artifact.meta["candidate_code_sha"] == "abc123"
    assert artifact.meta["status"] == "PASS"
    assert artifact.meta["verdict"] == "PASS"


@pytest.mark.anyio
async def test_run_e2e_passes_evidence_dir_and_normalizes_sparse_report_frontmatter(tmp_path):
    workspace = _workspace(tmp_path)
    artifact_service = ArtifactService()
    report_path = "/workspace/.autogen/runs/run-1/30-reviews/release-001/e2e/report.md"
    artifact_service.write_artifact(
        workspace,
        report_path,
        {
            "kind": "e2e_report",
        },
        "Browser validation passed.\n",
    )
    runner = OpenAIRoleRunner(
        DummyConfig(),
        None,
        artifact_service,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    captured: dict[str, object] = {}

    async def fake_run_role(**kwargs):
        captured.update(kwargs)
        return {"report_path": report_path, "verdict": "PASS", "summary": "ok"}

    runner._run_role = fake_run_role  # type: ignore[method-assign]

    result = await runner.run_e2e(
        state={
            "run_id": "run-1",
            "cycle_no": 1,
            "release_no": 1,
            "candidate_code_sha": "abc123",
            "execution_contract_path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
            "e2e_plan_path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/e2e-plan.md",
        },
        workspace=workspace,
        container_id="container-1",
    )

    assert result["verdict"] == "PASS"
    payload = json.loads(str(captured["user_prompt"]))
    assert payload["evidence_dir"] == "/workspace/.autogen/runs/run-1/30-reviews/release-001/e2e/evidence"
    assert payload["required_report_path"] == report_path
    artifact = artifact_service.read_artifact(workspace, report_path)
    assert artifact.meta["release"] == 1
    assert artifact.meta["role"] == "e2e"
    assert artifact.meta["candidate_code_sha"] == "abc123"
    assert artifact.meta["status"] == "PASS"
    assert artifact.meta["verdict"] == "PASS"


@pytest.mark.anyio
async def test_run_release_gate_normalizes_sparse_decision_frontmatter(tmp_path):
    workspace = _workspace(tmp_path)
    artifact_service = ArtifactService()
    decision_path = "/workspace/.autogen/runs/run-1/40-release/release-001/decision.md"
    artifact_service.write_artifact(
        workspace,
        decision_path,
        {
            "kind": "release_decision",
        },
        "Ship it.\n",
    )
    runner = OpenAIRoleRunner(
        DummyConfig(),
        None,
        artifact_service,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    async def fake_run_role(**kwargs):
        del kwargs
        return {
            "decision": "PASS",
            "decision_path": decision_path,
            "rework_summary_path": "",
            "summary": "ok",
        }

    runner._run_role = fake_run_role  # type: ignore[method-assign]

    result = await runner.run_release_gate(
        state={
            "run_id": "run-1",
            "release_no": 1,
            "execution_contract_path": "/workspace/.autogen/runs/run-1/10-planning/cycle-001/execution-contract.md",
            "review_results": {
                "compliance": {"report_path": "/workspace/.autogen/runs/run-1/30-reviews/release-001/compliance/report.md"}
            },
        },
        workspace=workspace,
        container_id="container-1",
    )

    assert result["decision"] == "PASS"
    artifact = artifact_service.read_artifact(workspace, decision_path)
    assert artifact.meta["release"] == 1
    assert artifact.meta["role"] == "release_gate"
    assert artifact.meta["decision"] == "PASS"


def test_build_tools_only_adds_file_write_tools_for_e2e_review_role(tmp_path):
    workspace = _workspace(tmp_path)
    runner = OpenAIRoleRunner(
        DummyConfig(),
        None,
        ArtifactService(),
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    e2e_tools = runner._build_tools(
        role="e2e",
        workspace=workspace,
        container_id="container-1",
        state={"run_id": "run-1", "cycle_no": 1, "release_no": 1},
        result_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    qa_tools = runner._build_tools(
        role="qa",
        workspace=workspace,
        container_id="container-1",
        state={"run_id": "run-1", "cycle_no": 1, "release_no": 1},
        result_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )

    assert "write_file" in e2e_tools
    assert "replace_in_file" in e2e_tools
    assert "write_file" not in qa_tools
    assert "replace_in_file" not in qa_tools
