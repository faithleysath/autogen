from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orchestrator.observability import current_langsmith_ids


RESERVED_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "message",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
}


class JsonFormatter(logging.Formatter):
    def __init__(self, *, default_fields: dict[str, Any] | None = None) -> None:
        super().__init__()
        self._default_fields = default_fields or {}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **self._default_fields,
        }
        for key, value in record.__dict__.items():
            if key in RESERVED_FIELDS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class LangSmithContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in current_langsmith_ids().items():
            setattr(record, key, value)
        return True


def setup_logging(*, logs_dir: Path, thread_id: str, command: str) -> Path:
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{thread_id}.log"
    formatter = JsonFormatter(
        default_fields={
            "thread_id": thread_id,
            "command": command,
            "service": "autogen-orchestrator",
        }
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(LangSmithContextFilter())
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(LangSmithContextFilter())
    root.addHandler(stream_handler)
    root.addHandler(file_handler)
    logging.captureWarnings(True)

    logging.getLogger(__name__).info("logging_configured", extra={"log_path": str(log_path)})
    return log_path
