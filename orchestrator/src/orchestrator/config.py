from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name)
    if not raw:
        return None
    return Path(raw).expanduser()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class OrchestratorConfig:
    workspace_root: Path
    state_dir: Path
    sqlite_path: Path
    logs_dir: Path
    tasks_dir: Path
    ssh_dir: Path | None
    docker_host: str
    dev_image: str
    e2e_image: str
    git_user_name: str
    git_user_email: str
    host_uid: int
    host_gid: int
    default_model: str
    architect_model: str
    developer_model: str
    gate_model: str
    review_model: str
    release_model: str
    langsmith_tracing: bool
    langsmith_api_key: str | None
    langsmith_project: str | None
    langsmith_workspace_id: str | None
    cmd_timeout_seconds: int
    container_start_timeout_seconds: int
    review_timeout_seconds: int
    push_lock_timeout_seconds: int
    openai_api_key: str | None
    model_pricing: dict[str, dict[str, float]]

    @classmethod
    def from_env(cls) -> "OrchestratorConfig":
        workspace_root = _env_path("AUTOGEN_WORKSPACE_ROOT")
        if workspace_root is None:
            raise ValueError("AUTOGEN_WORKSPACE_ROOT is required")
        if not workspace_root.is_absolute():
            raise ValueError("AUTOGEN_WORKSPACE_ROOT must be an absolute path")

        state_dir = _env_path("AUTOGEN_STATE_DIR") or (workspace_root / "_state")
        sqlite_path = _env_path("AUTOGEN_SQLITE_PATH") or (state_dir / "orchestrator.sqlite")
        ssh_dir = _env_path("AGENT_SSH_DIR")
        pricing_raw = os.getenv("AUTOGEN_MODEL_PRICING_JSON")
        model_pricing = json.loads(pricing_raw) if pricing_raw else {}

        default_model = os.getenv("AUTOGEN_MODEL_DEFAULT", "gpt-5.4")

        return cls(
            workspace_root=workspace_root,
            state_dir=state_dir,
            sqlite_path=sqlite_path,
            logs_dir=state_dir / "logs" / "orchestrator",
            tasks_dir=state_dir / "tasks",
            ssh_dir=ssh_dir,
            docker_host=os.getenv("DOCKER_HOST", "unix:///var/run/docker.sock"),
            dev_image=os.getenv("AUTOGEN_DEV_IMAGE", "autogen-agent-dev"),
            e2e_image=os.getenv("AUTOGEN_E2E_IMAGE", "autogen-agent-e2e"),
            git_user_name=os.getenv("AUTOGEN_GIT_USER_NAME", "Autogen"),
            git_user_email=os.getenv("AUTOGEN_GIT_USER_EMAIL", "autogen@local"),
            host_uid=int(os.getenv("HOST_UID", str(os.getuid()))),
            host_gid=int(os.getenv("HOST_GID", str(os.getgid()))),
            default_model=default_model,
            architect_model=os.getenv("AUTOGEN_MODEL_ARCHITECT", default_model),
            developer_model=os.getenv("AUTOGEN_MODEL_DEVELOPER", default_model),
            gate_model=os.getenv("AUTOGEN_MODEL_GATE", default_model),
            review_model=os.getenv("AUTOGEN_MODEL_REVIEW", default_model),
            release_model=os.getenv("AUTOGEN_MODEL_RELEASE", default_model),
            langsmith_tracing=_env_bool("LANGSMITH_TRACING", default=True),
            langsmith_api_key=os.getenv("LANGSMITH_API_KEY") or None,
            langsmith_project=os.getenv("LANGSMITH_PROJECT") or None,
            langsmith_workspace_id=os.getenv("LANGSMITH_WORKSPACE_ID") or None,
            cmd_timeout_seconds=int(os.getenv("AUTOGEN_CMD_TIMEOUT_SECONDS", "300")),
            container_start_timeout_seconds=int(
                os.getenv("AUTOGEN_CONTAINER_START_TIMEOUT_SECONDS", "60")
            ),
            review_timeout_seconds=int(os.getenv("AUTOGEN_REVIEW_TIMEOUT_SECONDS", "900")),
            push_lock_timeout_seconds=int(os.getenv("AUTOGEN_PUSH_LOCK_TIMEOUT_SECONDS", "120")),
            openai_api_key=os.getenv("OPENAI_API_KEY") or None,
            model_pricing=model_pricing,
        )

    def model_for_role(self, role: str) -> str:
        if role == "architect":
            return self.architect_model
        if role == "developer":
            return self.developer_model
        if role == "stage_gate":
            return self.gate_model
        if role in {"compliance", "qa", "e2e"}:
            return self.review_model
        if role == "release_gate":
            return self.release_model
        return self.default_model
