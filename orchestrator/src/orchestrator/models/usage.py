from __future__ import annotations

from dataclasses import dataclass
from typing import Any


NUMERIC_USAGE_FIELDS = (
    "requests",
    "retries",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_output_tokens",
)


def _read_usage_field(payload: Any, field: str, default: Any = None) -> Any:
    if payload is None:
        return default
    if isinstance(payload, dict):
        return payload.get(field, default)
    return getattr(payload, field, default)


def _int_field(payload: Any, field: str) -> int:
    value = _read_usage_field(payload, field, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def estimate_cost_usd(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing_by_model: dict[str, dict[str, float]],
) -> float | None:
    pricing = pricing_by_model.get(model) or pricing_by_model.get("*")
    if not pricing:
        return None
    input_rate = float(pricing.get("input_per_million", 0.0))
    output_rate = float(pricing.get("output_per_million", 0.0))
    estimate = ((input_tokens * input_rate) + (output_tokens * output_rate)) / 1_000_000
    return round(estimate, 8)


@dataclass(slots=True)
class RoleUsage:
    model: str
    requests: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    estimated_cost_usd: float | None = None

    def add_retry(self) -> None:
        self.retries += 1

    def add_response(
        self,
        response: Any,
        *,
        pricing_by_model: dict[str, dict[str, float]] | None = None,
    ) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.requests += 1
        self.input_tokens += _int_field(usage, "input_tokens")
        self.output_tokens += _int_field(usage, "output_tokens")
        total_tokens = _int_field(usage, "total_tokens")
        if total_tokens:
            self.total_tokens += total_tokens
        else:
            self.total_tokens += _int_field(usage, "input_tokens") + _int_field(usage, "output_tokens")

        input_details = _read_usage_field(usage, "input_tokens_details")
        output_details = _read_usage_field(usage, "output_tokens_details")
        self.cached_input_tokens += _int_field(input_details, "cached_tokens")
        self.reasoning_output_tokens += _int_field(output_details, "reasoning_tokens")

        if pricing_by_model:
            estimate = estimate_cost_usd(
                model=self.model,
                input_tokens=_int_field(usage, "input_tokens"),
                output_tokens=_int_field(usage, "output_tokens"),
                pricing_by_model=pricing_by_model,
            )
            if estimate is not None:
                self.estimated_cost_usd = round((self.estimated_cost_usd or 0.0) + estimate, 8)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "requests": self.requests,
            "retries": self.retries,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_output_tokens": self.reasoning_output_tokens,
        }
        if self.estimated_cost_usd is not None:
            payload["estimated_cost_usd"] = self.estimated_cost_usd
        return payload


def merge_usage_metrics(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    left = left or {}
    right = right or {}
    model = right.get("model") or left.get("model")
    if model:
        merged["model"] = model
    for field in NUMERIC_USAGE_FIELDS:
        total = int(left.get(field, 0) or 0) + int(right.get(field, 0) or 0)
        if total:
            merged[field] = total
    if "estimated_cost_usd" in left or "estimated_cost_usd" in right:
        merged["estimated_cost_usd"] = round(
            float(left.get("estimated_cost_usd", 0.0) or 0.0)
            + float(right.get("estimated_cost_usd", 0.0) or 0.0),
            8,
        )
    return merged


def merge_usage_summaries(left: dict[str, Any] | None, right: dict[str, Any] | None) -> dict[str, Any]:
    left = left or {}
    right = right or {}
    merged_by_role: dict[str, dict[str, Any]] = {}
    for role in sorted(set(left.get("by_role", {})) | set(right.get("by_role", {}))):
        merged_by_role[role] = merge_usage_metrics(
            left.get("by_role", {}).get(role),
            right.get("by_role", {}).get(role),
        )
    totals = merge_usage_metrics(left.get("totals"), right.get("totals"))
    payload: dict[str, Any] = {}
    if merged_by_role:
        payload["by_role"] = merged_by_role
    if totals:
        payload["totals"] = totals
    return payload


def usage_summary_delta(role: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    if not usage:
        return {}
    totals = {key: value for key, value in usage.items() if key != "model"}
    return {
        "by_role": {role: usage},
        "totals": totals,
    }
