from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from orchestrator.services.role_runner import OpenAIRoleRunner
from orchestrator.tools.base import ToolSpec


class RetryableError(RuntimeError):
    status_code = 429


class FakeResponsesApi:
    def __init__(self, sequence):
        self._sequence = list(sequence)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._sequence.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, sequence):
        self.responses = FakeResponsesApi(sequence)


class RoleRunnerHarness(OpenAIRoleRunner):
    def __init__(self, client, tools):
        super().__init__(
            SimpleNamespace(openai_api_key=None, model_pricing={}),
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
        [
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
