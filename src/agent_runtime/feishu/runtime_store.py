from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from agent_runtime.feishu.events import FeishuMessageEvent, effective_thread_id, queue_key_for_event


def _hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12] if value else ""


@dataclass(frozen=True)
class EventClaim:
    status: str
    should_process: bool


class RuntimeStore:
    def __init__(self, db_path: str, ttl_seconds: int, max_items: int) -> None:
        self.db_path = Path(db_path)
        self.ttl_seconds = max(0, ttl_seconds)
        self.max_items = max(1, max_items)
        self._lock = Lock()
        self._ensure_schema()

    def claim_event(self, event: FeishuMessageEvent) -> EventClaim:
        event_key = event.message_id or event.event_id
        if not event_key:
            return EventClaim(status="processing", should_process=True)
        now = time.time()
        with self._lock, self._connect() as connection:
            self._prune(connection, now)
            row = connection.execute(
                "SELECT status FROM seen_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is not None:
                previous_status = str(row[0] or "")
                if previous_status in {"agent_failed", "reply_failed"}:
                    connection.execute(
                        "UPDATE seen_events SET status = ?, updated_at = ? WHERE event_key = ?",
                        ("processing", now, event_key),
                    )
                    return EventClaim(status=f"retry_{previous_status}", should_process=True)
                return EventClaim(status="duplicate", should_process=False)

            connection.execute(
                """
                INSERT INTO seen_events (
                    event_key, message_id, event_id, chat_id_hash, thread_id_hash, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    event.message_id,
                    event.event_id,
                    _hash(event.chat_id),
                    _hash(effective_thread_id(event)),
                    "processing",
                    now,
                    now,
                ),
            )
            self._trim(connection)
            return EventClaim(status="processing", should_process=True)

    def try_record_event(self, event: FeishuMessageEvent) -> bool:
        return self.claim_event(event).should_process

    def mark_event_status(self, event: FeishuMessageEvent, status: str) -> None:
        event_key = event.message_id or event.event_id
        if not event_key:
            return
        now = time.time()
        with self._lock, self._connect() as connection:
            self._mark_event_status(connection, event_key, status, now)

    def record_reply(
        self,
        event: FeishuMessageEvent,
        status: str,
        reply_message_id: str = "",
        error: str = "",
    ) -> None:
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reply_ledger (
                    source_message_id, event_id, chat_id_hash, thread_id_hash,
                    reply_message_id, status, error, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_message_id) DO UPDATE SET
                    reply_message_id=excluded.reply_message_id,
                    status=excluded.status,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    event.message_id,
                    event.event_id,
                    _hash(event.chat_id),
                    _hash(effective_thread_id(event)),
                    reply_message_id,
                    status,
                    error[:1000],
                    now,
                ),
            )
            self._mark_event_status(connection, event.message_id or event.event_id, status, now)

    def record_event_error(self, stage: str, event: FeishuMessageEvent, error: str) -> None:
        now = time.time()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO event_errors (
                    stage, event_key, chat_id_hash, thread_id_hash, message_id_hash, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stage,
                    event.message_id or event.event_id,
                    _hash(event.chat_id),
                    _hash(effective_thread_id(event)),
                    _hash(event.message_id),
                    error[:1000],
                    now,
                ),
            )
        if stage == "agent":
            self.mark_event_status(event, "agent_failed")

    def record_sender_turn(self, event: FeishuMessageEvent, is_bot_sender: bool) -> int:
        key = queue_key_for_event(event)
        now = time.time()
        with self._lock, self._connect() as connection:
            if not is_bot_sender:
                connection.execute(
                    """
                    INSERT INTO bot_turns (queue_key, consecutive_bot_turns, updated_at)
                    VALUES (?, 0, ?)
                    ON CONFLICT(queue_key) DO UPDATE SET
                        consecutive_bot_turns=0,
                        updated_at=excluded.updated_at
                    """,
                    (key, now),
                )
                return 0
            row = connection.execute(
                "SELECT consecutive_bot_turns FROM bot_turns WHERE queue_key = ?",
                (key,),
            ).fetchone()
            count = int(row[0]) + 1 if row else 1
            connection.execute(
                """
                INSERT INTO bot_turns (queue_key, consecutive_bot_turns, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(queue_key) DO UPDATE SET
                    consecutive_bot_turns=excluded.consecutive_bot_turns,
                    updated_at=excluded.updated_at
                """,
                (key, count, now),
            )
            return count

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS seen_events (
                    event_key TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL DEFAULT '',
                    chat_id_hash TEXT NOT NULL DEFAULT '',
                    thread_id_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'processing',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_seen_events_created_at
                    ON seen_events(created_at);

                CREATE TABLE IF NOT EXISTS reply_ledger (
                    source_message_id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL DEFAULT '',
                    chat_id_hash TEXT NOT NULL DEFAULT '',
                    thread_id_hash TEXT NOT NULL DEFAULT '',
                    reply_message_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS event_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    stage TEXT NOT NULL,
                    event_key TEXT NOT NULL DEFAULT '',
                    chat_id_hash TEXT NOT NULL DEFAULT '',
                    thread_id_hash TEXT NOT NULL DEFAULT '',
                    message_id_hash TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS bot_turns (
                    queue_key TEXT PRIMARY KEY,
                    consecutive_bot_turns INTEGER NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL
                );
                """
            )
            self._ensure_seen_events_columns(connection)

    def _ensure_seen_events_columns(self, connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(seen_events)").fetchall()}
        if "status" not in columns:
            connection.execute("ALTER TABLE seen_events ADD COLUMN status TEXT NOT NULL DEFAULT 'replied'")
        if "updated_at" not in columns:
            connection.execute("ALTER TABLE seen_events ADD COLUMN updated_at REAL NOT NULL DEFAULT 0")

    def _event_exists(self, connection: sqlite3.Connection, event_key: str) -> bool:
        return connection.execute("SELECT 1 FROM seen_events WHERE event_key = ?", (event_key,)).fetchone() is not None

    def _mark_event_status(self, connection: sqlite3.Connection, event_key: str, status: str, now: float) -> None:
        if not event_key:
            return
        if self._event_exists(connection, event_key):
            connection.execute(
                "UPDATE seen_events SET status = ?, updated_at = ? WHERE event_key = ?",
                (status, now, event_key),
            )

    def _prune(self, connection: sqlite3.Connection, now: float) -> None:
        if self.ttl_seconds <= 0:
            return
        cutoff = now - self.ttl_seconds
        connection.execute("DELETE FROM seen_events WHERE created_at < ?", (cutoff,))

    def _trim(self, connection: sqlite3.Connection) -> None:
        count = connection.execute("SELECT COUNT(*) FROM seen_events").fetchone()[0]
        overflow = int(count) - self.max_items
        if overflow <= 0:
            return
        connection.execute(
            """
            DELETE FROM seen_events
            WHERE event_key IN (
                SELECT event_key FROM seen_events
                ORDER BY created_at ASC
                LIMIT ?
            )
            """,
            (overflow,),
        )
