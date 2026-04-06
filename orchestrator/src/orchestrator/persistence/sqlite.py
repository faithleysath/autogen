from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def build_checkpointer(sqlite_path: Path) -> Any:
    from langgraph.checkpoint.sqlite import SqliteSaver

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(sqlite_path, check_same_thread=False)
    saver = SqliteSaver(connection)
    if hasattr(saver, "setup"):
        saver.setup()
    return saver
