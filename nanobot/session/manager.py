"""Session management for conversation history — SQLite backend."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Any

from loguru import logger

from nanobot.agent.memory import init_user_workspace

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    key              TEXT PRIMARY KEY,
    user_id          TEXT REFERENCES users(user_id),
    title            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    last_consolidated INTEGER NOT NULL DEFAULT 0,
    metadata         TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key  TEXT NOT NULL REFERENCES sessions(key),
    role         TEXT NOT NULL,
    content      TEXT,
    timestamp    TEXT NOT NULL,
    extra        TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_key, id);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
"""

# Message fields that go into the `extra` JSON column
_EXTRA_KEYS = ("tool_calls", "tool_call_id", "name", "tools_used",
               "reasoning_content", "thinking_blocks")


@dataclass
class Session:
    """
    An in-memory conversation session.

    Messages are append-only for LLM cache efficiency.
    Consolidation writes summaries to MEMORY.md/HISTORY.md but does NOT
    modify the messages list or get_history() output.
    """

    key: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0
    # Tracks how many messages are already persisted in DB (private, not compared/repr'd)
    _db_message_count: int = field(default=0, repr=False, compare=False)

    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs,
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()

    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Return unconsolidated messages for LLM input, aligned to a user turn."""
        unconsolidated = self.messages[self.last_consolidated:]
        sliced = unconsolidated[-max_messages:]

        # Drop leading non-user messages to avoid orphaned tool_result blocks
        for i, m in enumerate(sliced):
            if m.get("role") == "user":
                sliced = sliced[i:]
                break

        out: list[dict[str, Any]] = []
        for m in sliced:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out

    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        # Keep _db_message_count unchanged so save() can detect the clear and DELETE from DB
        self.updated_at = datetime.now()


class SessionManager:
    """Manages conversation sessions backed by SQLite."""

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self._db_path = workspace / "sessions.db"
        self._conn: Optional[sqlite3.Connection] = None
        self._cache: dict[str, Session] = {}
        self._init_db()

    # ── DB connection ─────────────────────────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_user_id(key: str) -> Optional[str]:
        parts = key.split(":", 2)
        if len(parts) >= 2 and parts[0] in ("http", "cli") and parts[1]:
            return parts[1]
        return None

    # ── Public API ────────────────────────────────────────────────────────────

    def get_or_create(self, key: str) -> Session:
        if key in self._cache:
            return self._cache[key]

        session = self._load(key)
        if session is None:
            session = Session(key=key)
            user_id = self._extract_user_id(key)
            if user_id:
                init_user_workspace(self.workspace, user_id)
                self.create_user(user_id)  # ensure user record exists
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO sessions (key, user_id, created_at, updated_at, last_consolidated, metadata)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (key, user_id,
                 session.created_at.isoformat(), session.updated_at.isoformat(),
                 0, "{}"),
            )
            conn.commit()

        self._cache[key] = session
        return session

    def _load(self, key: str) -> Optional[Session]:
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM sessions WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None

        msg_rows = conn.execute(
            "SELECT role, content, timestamp, extra FROM messages"
            " WHERE session_key = ? ORDER BY id",
            (key,),
        ).fetchall()

        messages: list[dict[str, Any]] = []
        for mr in msg_rows:
            msg: dict[str, Any] = {
                "role": mr["role"],
                "content": mr["content"],
                "timestamp": mr["timestamp"],
            }
            extra = json.loads(mr["extra"]) if mr["extra"] and mr["extra"] != "{}" else {}
            msg.update(extra)
            messages.append(msg)

        return Session(
            key=key,
            messages=messages,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
            last_consolidated=row["last_consolidated"],
            _db_message_count=len(messages),
        )

    def save(self, session: Session) -> None:
        conn = self._get_conn()
        with conn:  # auto-commit / rollback transaction
            # Detect clear(): messages were wiped but DB still has rows
            if len(session.messages) < session._db_message_count:
                conn.execute("DELETE FROM messages WHERE session_key = ?", (session.key,))
                session._db_message_count = 0

            # Append only new messages
            new_messages = session.messages[session._db_message_count:]
            for msg in new_messages:
                extra = {k: msg[k] for k in _EXTRA_KEYS if k in msg}
                conn.execute(
                    "INSERT INTO messages (session_key, role, content, timestamp, extra)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        session.key,
                        msg["role"],
                        msg.get("content"),
                        msg.get("timestamp", datetime.now().isoformat()),
                        json.dumps(extra, ensure_ascii=False) if extra else "{}",
                    ),
                )

            conn.execute(
                "UPDATE sessions SET updated_at = ?, last_consolidated = ?, metadata = ?"
                " WHERE key = ?",
                (
                    session.updated_at.isoformat(),
                    session.last_consolidated,
                    json.dumps(session.metadata, ensure_ascii=False),
                    session.key,
                ),
            )

            # Set title from first user message if not set
            if new_messages:
                first_user = next((m for m in new_messages if m.get("role") == "user"), None)
                if first_user and first_user.get("content"):
                    title = first_user["content"][:60]
                    conn.execute(
                        "UPDATE sessions SET title = ? WHERE key = ? AND title IS NULL",
                        (title, session.key),
                    )

        session._db_message_count = len(session.messages)
        self._cache[session.key] = session

    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache (forces reload from DB next time)."""
        self._cache.pop(key, None)

    def list_sessions(self, user_id: Optional[str] = None) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by user_id, newest first."""
        conn = self._get_conn()
        query = """
            SELECT s.key, s.user_id, s.title, s.created_at, s.updated_at,
                   COUNT(m.id) as message_count
            FROM sessions s
            LEFT JOIN messages m ON m.session_key = s.key
            {where}
            GROUP BY s.key
            ORDER BY s.updated_at DESC
        """
        if user_id:
            rows = conn.execute(
                query.format(where="WHERE s.user_id = ?"), (user_id,)
            ).fetchall()
        else:
            rows = conn.execute(query.format(where="")).fetchall()
        return [dict(r) for r in rows]

    def create_user(self, user_id: str) -> dict[str, Any]:
        """Create a new user. Returns the user dict. No-op if already exists."""
        now = datetime.now().isoformat()
        conn = self._get_conn()
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, created_at) VALUES (?, ?)",
            (user_id, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)

    def list_users(self) -> list[dict[str, Any]]:
        """List all users ordered by creation time."""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT user_id, created_at FROM users ORDER BY created_at ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, key: str) -> None:
        """Delete a session and all its messages."""
        conn = self._get_conn()
        with conn:
            conn.execute("DELETE FROM messages WHERE session_key = ?", (key,))
            conn.execute("DELETE FROM sessions WHERE key = ?", (key,))
        self._cache.pop(key, None)

    def get_session_messages(self, key: str) -> list[dict[str, Any]]:
        """Return full message sequence for a session (for display/replay, not LLM history).

        Returns all roles including tool results so the frontend can reconstruct
        thinking sections (tool calls are stored in the extra JSON column).
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT role, content, extra FROM messages WHERE session_key = ? ORDER BY id",
            (key,),
        ).fetchall()
        result = []
        for r in rows:
            msg: dict[str, Any] = {"role": r["role"], "content": r["content"]}
            extra = json.loads(r["extra"]) if r["extra"] and r["extra"] != "{}" else {}
            if extra.get("tool_calls"):
                msg["tool_calls"] = extra["tool_calls"]
            if extra.get("tool_call_id"):
                msg["tool_call_id"] = extra["tool_call_id"]
            result.append(msg)
        return result
