"""Conversation storage (SQLite, separate from the mock MCP database).

Every message is kept for audit. The model only sees the last `window` messages
plus a running summary of everything before them (`summarized_through`)."""
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id     TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'handoff')),
    summary             TEXT NOT NULL DEFAULT '',
    summarized_through  INTEGER NOT NULL DEFAULT 0,   -- last message_id folded into summary
    customer_json       TEXT,                         -- set once find_customer succeeds
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE TABLE IF NOT EXISTS messages (
    message_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations(conversation_id),
    role             TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content          TEXT NOT NULL,
    tool_notes       TEXT NOT NULL DEFAULT '',        -- compact tool trace (assistant only)
    input_tokens     INTEGER NOT NULL DEFAULT 0,      -- usage of the turn (assistant only),
    output_tokens    INTEGER NOT NULL DEFAULT 0,      -- including summary calls
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_messages_conv ON messages(conversation_id, message_id);
CREATE TABLE IF NOT EXISTS tool_calls (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL,
    message_id       INTEGER,                         -- assistant message of the turn
    name             TEXT NOT NULL,
    input_json       TEXT NOT NULL,
    output           TEXT NOT NULL,
    is_error         INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

# Columns added after the first release; ALTERed into existing databases.
MIGRATIONS = {
    "messages": {
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
    },
    "tool_calls": {"message_id": "INTEGER"},
}

USAGE_COLUMNS = {  # API usage field -> messages column
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_write_tokens",
}


@dataclass
class Conversation:
    conversation_id: str
    status: str
    summary: str
    summarized_through: int
    customer: dict[str, Any] | None


@dataclass
class StoredMessage:
    message_id: int
    role: str
    content: str
    tool_notes: str
    created_at: str


class Storage:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            for table, cols in MIGRATIONS.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for col, ddl in cols.items():
                    if col not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_or_create(self, conversation_id: str) -> Conversation:
        with self._conn() as c:
            c.execute("INSERT OR IGNORE INTO conversations (conversation_id) VALUES (?)",
                      (conversation_id,))
            r = c.execute("SELECT * FROM conversations WHERE conversation_id = ?",
                          (conversation_id,)).fetchone()
        return Conversation(
            conversation_id=r["conversation_id"],
            status=r["status"],
            summary=r["summary"],
            summarized_through=r["summarized_through"],
            customer=json.loads(r["customer_json"]) if r["customer_json"] else None,
        )

    def add_message(self, conversation_id: str, role: str, content: str,
                    tool_notes: str = "", usage: dict[str, int] | None = None) -> int:
        u = usage or {}
        cols = ", ".join(USAGE_COLUMNS.values())
        with self._conn() as c:
            cur = c.execute(
                f"INSERT INTO messages (conversation_id, role, content, tool_notes, {cols}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, role, content, tool_notes,
                 *(u.get(k, 0) for k in USAGE_COLUMNS)))
            # Link this turn's tool calls to the assistant message that closed it.
            if role == "assistant":
                c.execute("UPDATE tool_calls SET message_id = ? "
                          "WHERE conversation_id = ? AND message_id IS NULL",
                          (cur.lastrowid, conversation_id))
            c.execute("UPDATE conversations SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                      "WHERE conversation_id = ?", (conversation_id,))
            return cur.lastrowid

    def unsummarized(self, conversation_id: str, after_id: int) -> list[StoredMessage]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT message_id, role, content, tool_notes, created_at FROM messages "
                "WHERE conversation_id = ? AND message_id > ? ORDER BY message_id",
                (conversation_id, after_id)).fetchall()
        return [StoredMessage(**dict(r)) for r in rows]

    def all_messages(self, conversation_id: str) -> list[StoredMessage]:
        return self.unsummarized(conversation_id, 0)

    # ------------------------------------------------------------ read views
    def transcript(self, conversation_id: str) -> list[dict[str, Any]]:
        """Messages with their usage and tool calls, for the chat UI and admin."""
        with self._conn() as c:
            msgs = [dict(r) for r in c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY message_id",
                (conversation_id,))]
            calls = c.execute(
                "SELECT message_id, name, input_json, output, is_error FROM tool_calls "
                "WHERE conversation_id = ? ORDER BY id", (conversation_id,)).fetchall()
        by_msg: dict[int, list[dict[str, Any]]] = {}
        for r in calls:
            by_msg.setdefault(r["message_id"], []).append({
                "name": r["name"], "input": json.loads(r["input_json"]),
                "output": r["output"], "is_error": bool(r["is_error"])})
        for m in msgs:
            m["tool_calls"] = by_msg.get(m["message_id"], [])
            m["usage"] = {k: m.pop(col) for k, col in USAGE_COLUMNS.items()}
        return msgs

    def conversations_overview(self) -> list[dict[str, Any]]:
        sums = ", ".join(f"coalesce(sum(m.{col}), 0) AS {col}" for col in USAGE_COLUMNS.values())
        with self._conn() as c:
            rows = c.execute(f"""
                SELECT c.conversation_id, c.status, c.customer_json, c.created_at, c.updated_at,
                       count(m.message_id) AS messages,
                       coalesce(sum(m.role = 'user'), 0) AS user_messages,
                       {sums},
                       (SELECT count(*) FROM tool_calls t
                         WHERE t.conversation_id = c.conversation_id) AS tool_calls
                  FROM conversations c
                  LEFT JOIN messages m ON m.conversation_id = c.conversation_id
                 GROUP BY c.conversation_id
                 ORDER BY c.updated_at DESC""").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            cust = json.loads(d.pop("customer_json")) if r["customer_json"] else None
            d["customer"] = cust
            out.append(d)
        return out

    def successful_tool_outputs(self, name: str) -> list[dict[str, Any]]:
        """Parsed outputs of successful calls to `name` (e.g. create_ticket), newest first.
        Read from our own log, so it works the same against the mock or the real server."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT conversation_id, output, created_at FROM tool_calls "
                "WHERE name = ? AND is_error = 0 ORDER BY id DESC", (name,)).fetchall()
        out = []
        for r in rows:
            try:
                data = json.loads(r["output"])
            except ValueError:
                data = {"raw": r["output"]}
            out.append({**data, "conversation_id": data.get("conversation_id", r["conversation_id"]),
                        "logged_at": r["created_at"]})
        return out

    def set_summary(self, conversation_id: str, summary: str, through_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE conversations SET summary = ?, summarized_through = ? "
                      "WHERE conversation_id = ?", (summary, through_id, conversation_id))

    def set_customer(self, conversation_id: str, customer: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute("UPDATE conversations SET customer_json = ? WHERE conversation_id = ?",
                      (json.dumps(customer, ensure_ascii=False), conversation_id))

    def set_status(self, conversation_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE conversations SET status = ? WHERE conversation_id = ?",
                      (status, conversation_id))

    def log_tool_call(self, conversation_id: str, name: str, args: dict, output: str,
                      is_error: bool) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO tool_calls (conversation_id, name, input_json, output, is_error) "
                "VALUES (?, ?, ?, ?, ?)",
                (conversation_id, name, json.dumps(args, ensure_ascii=False), output, int(is_error)))
