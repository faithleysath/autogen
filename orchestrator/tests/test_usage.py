from __future__ import annotations

from orchestrator.models.usage import merge_usage_summaries


def test_merge_usage_summaries_adds_parallel_role_updates():
    merged = merge_usage_summaries(
        {
            "by_role": {"developer": {"model": "gpt-a", "requests": 1, "input_tokens": 10}},
            "totals": {"requests": 1, "input_tokens": 10},
        },
        {
            "by_role": {"qa": {"model": "gpt-a", "requests": 2, "input_tokens": 20}},
            "totals": {"requests": 2, "input_tokens": 20},
        },
    )

    assert merged["totals"]["requests"] == 3
    assert merged["totals"]["input_tokens"] == 30
    assert merged["by_role"]["developer"]["requests"] == 1
    assert merged["by_role"]["qa"]["requests"] == 2
