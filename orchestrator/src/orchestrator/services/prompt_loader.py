from __future__ import annotations

from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
SKILL_DIR = Path(__file__).resolve().parent.parent / "skills"

ROLE_SKILL_MAP: dict[str, tuple[str, ...]] = {
    "architect": ("repo-survey", "planning-contract", "artifact-discipline"),
    "developer": ("repo-survey", "stage-implementation", "validation-hygiene"),
    "stage_gate": ("verification-evidence", "artifact-discipline"),
    "compliance": ("verification-evidence", "artifact-discipline"),
    "qa": ("verification-evidence", "artifact-discipline"),
    "e2e": ("verification-evidence", "artifact-discipline"),
    "release_gate": ("release-adjudication", "artifact-discipline"),
}


def load_role_prompt(role: str) -> str:
    prompt_path = PROMPT_DIR / f"{role}.md"
    base_prompt = prompt_path.read_text(encoding="utf-8").strip()
    skill_names = ROLE_SKILL_MAP.get(role, ())
    if not skill_names:
        return base_prompt
    sections = [base_prompt, "", "Capability packs:"]
    for skill_name in skill_names:
        skill_path = SKILL_DIR / f"{skill_name}.md"
        skill_text = skill_path.read_text(encoding="utf-8").strip()
        sections.extend(["", f"[Skill: {skill_name}]", skill_text])
    return "\n".join(sections).strip()
