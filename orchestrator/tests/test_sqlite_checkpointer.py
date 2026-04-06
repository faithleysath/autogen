from __future__ import annotations

import pytest

from orchestrator.persistence.sqlite import build_checkpointer


@pytest.mark.anyio
async def test_checkpointer_supports_async_methods(tmp_path):
    checkpointer = build_checkpointer(tmp_path / "state" / "orchestrator.sqlite")
    config = {"configurable": {"thread_id": "thread-1", "checkpoint_ns": ""}}
    checkpoint = {
        "id": "cp-1",
        "ts": "2026-04-06T12:00:00Z",
        "channel_values": {},
        "channel_versions": {},
        "versions_seen": {},
    }

    stored_config = await checkpointer.aput(config, checkpoint, {"source": "input", "step": 1}, {})
    restored = await checkpointer.aget_tuple(stored_config)

    assert restored is not None
    assert restored.checkpoint["id"] == "cp-1"
