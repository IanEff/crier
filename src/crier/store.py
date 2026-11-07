"""The local cache: messages, per-channel cursors, and finished sections.

A SQLite file inside the RC vault's hidden `.crier/` folder, so Recursers'
words stay behind the same wall as the rest of the RC vault.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path

from crier.zulip import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    stream TEXT NOT NULL,
    stream_id INTEGER NOT NULL,
    topic TEXT NOT NULL,
    sender TEXT NOT NULL,
    sender_email TEXT NOT NULL,
    ts INTEGER NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages (ts);
CREATE TABLE IF NOT EXISTS cursors (
    stream TEXT PRIMARY KEY,
    last_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sections (
    key TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL,
    output TEXT NOT NULL,
    updated INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pushed (
    msg_id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

COLUMNS = "id, stream, stream_id, topic, sender, sender_email, ts, content"


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        # SQLite's lower() folds ASCII only; channel names can be "Écrire" or "🧑‍💻 x".
        self.db.create_function("pylower", 1, str.lower, deterministic=True)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def cursor(self, channel: str) -> int | None:
        row = self.db.execute(
            "SELECT last_id FROM cursors WHERE stream = ?", (channel.lower(),)
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, channel: str, last_id: int) -> None:
        self.db.execute(
            "INSERT INTO cursors (stream, last_id) VALUES (?, ?)"
            " ON CONFLICT (stream) DO UPDATE SET last_id = max(last_id, excluded.last_id)",
            (channel.lower(), last_id),
        )
        self.db.commit()

    def add(self, msgs: Iterable[Message]) -> int:
        rows = [
            (m.id, m.stream, m.stream_id, m.topic, m.sender, m.sender_email, m.timestamp, m.content)
            for m in msgs
        ]
        self.db.executemany(f"INSERT OR REPLACE INTO messages ({COLUMNS}) VALUES (?,?,?,?,?,?,?,?)", rows)
        self.db.commit()
        return len(rows)

    def window(self, channels: Iterable[str], since_ts: int, until_ts: int | None = None) -> list[Message]:
        """Messages in these channels (any case) from since_ts up to until_ts, oldest first."""
        names = [c.lower() for c in channels]
        if not names:
            return []
        marks = ",".join("?" * len(names))
        until = until_ts if until_ts is not None else 2**62
        rows = self.db.execute(
            f"SELECT {COLUMNS} FROM messages WHERE pylower(stream) IN ({marks})"
            " AND ts >= ? AND ts < ? ORDER BY ts, id",
            (*names, since_ts, until),
        ).fetchall()
        return [Message(*row) for row in rows]

    def prune(self, before_ts: int) -> int:
        cur = self.db.execute("DELETE FROM messages WHERE ts < ?", (before_ts,))
        self.db.execute("DELETE FROM pushed WHERE ts < ?", (before_ts,))
        self.db.commit()
        return cur.rowcount

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def pushed(self, ids: Iterable[int]) -> set[int]:
        """The subset of these message ids already pushed."""
        ids = list(ids)
        if not ids:
            return set()
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(f"SELECT msg_id FROM pushed WHERE msg_id IN ({marks})", ids)
        return {r[0] for r in rows}

    def mark_pushed(self, ids: Iterable[int]) -> None:
        now = int(time.time())
        self.db.executemany(
            "INSERT OR IGNORE INTO pushed (msg_id, ts) VALUES (?, ?)", [(i, now) for i in ids]
        )
        self.db.commit()

    def cached(self, key: str, input_hash: str) -> str | None:
        row = self.db.execute(
            "SELECT output FROM sections WHERE key = ? AND input_hash = ?", (key, input_hash)
        ).fetchone()
        return row[0] if row else None

    def remember(self, key: str, input_hash: str, output: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sections (key, input_hash, output, updated) VALUES (?, ?, ?, ?)",
            (key, input_hash, output, int(time.time())),
        )
        self.db.commit()
