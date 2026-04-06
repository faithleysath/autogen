from __future__ import annotations

from orchestrator.services.prompt_loader import load_role_prompt


def test_load_role_prompt_includes_bundled_skills():
    prompt = load_role_prompt("developer")
    assert "You are the developer role for the Autogen v1 harness." in prompt
    assert "[Skill: stage-implementation]" in prompt
    assert "background task" in prompt
