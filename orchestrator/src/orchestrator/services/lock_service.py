from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

logger = logging.getLogger(__name__)


class LockService:
    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._setup()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._sqlite_path)

    def _setup(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_locks (
                    lock_key TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            conn.commit()

    def _try_acquire(self, lock_key: str, owner_id: str, lease_seconds: int) -> bool:
        now = time.time()
        expires_at = now + lease_seconds
        with self._connect() as conn:
            conn.execute("DELETE FROM push_locks WHERE expires_at <= ?", (now,))
            try:
                conn.execute(
                    "INSERT INTO push_locks(lock_key, owner_id, expires_at) VALUES (?, ?, ?)",
                    (lock_key, owner_id, expires_at),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            conn.commit()
            return True

    def _release(self, lock_key: str, owner_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM push_locks WHERE lock_key = ? AND owner_id = ?",
                (lock_key, owner_id),
            )
            conn.commit()

    @asynccontextmanager
    async def push_lock(
        self,
        *,
        repo_url: str,
        run_branch: str,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        lock_key = f"{repo_url}::{run_branch}"
        owner_id = uuid.uuid4().hex
        deadline = time.monotonic() + timeout_seconds
        while True:
            acquired = await asyncio.to_thread(
                self._try_acquire,
                lock_key,
                owner_id,
                timeout_seconds,
            )
            if acquired:
                logger.info("push_lock_acquired", extra={"lock_key": lock_key})
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for push lock {lock_key}")
            await asyncio.sleep(0.5)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release, lock_key, owner_id)
            logger.info("push_lock_released", extra={"lock_key": lock_key})
