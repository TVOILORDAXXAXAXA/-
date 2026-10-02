"""Локальная история чатов и избранные ответы. Сохранение истории включается пользователем."""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
import uuid
from pathlib import Path

BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "jarvis_chat_history.sqlite3"


def _ensure_db() -> None:
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS conversations ("
            "id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL, "
            "text TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id, id)")
        db.execute("CREATE INDEX IF NOT EXISTS messages_text_idx ON messages(text)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS bookmarks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, role TEXT NOT NULL, "
            "text TEXT NOT NULL, created_at TEXT NOT NULL)"
        )


def new_conversation_id() -> str:
    return uuid.uuid4().hex


def save_message(conversation_id: str, role: str, text: str) -> None:
    if role not in {"Ты", "Джарвис"} or not text.strip():
        return
    _ensure_db()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, "Новый диалог", now, now),
        )
        db.execute(
            "INSERT INTO messages(conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, text[:50_000], now),
        )
        if role == "Ты":
            title = " ".join(text.split())[:80] or "Новый диалог"
            db.execute(
                "UPDATE conversations SET title = CASE WHEN title = 'Новый диалог' THEN ? ELSE title END, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )
        else:
            db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))


def search_messages(query: str = "", limit: int = 100) -> list[tuple[int, str, str, str, str, str]]:
    _ensure_db()
    limit = max(1, min(int(limit), 500))
    pattern = f"%{query.strip()}%"
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT messages.id, messages.conversation_id, conversations.title, messages.role, messages.text, messages.created_at "
            "FROM messages JOIN conversations ON conversations.id = messages.conversation_id "
            "WHERE (? = '' OR messages.text LIKE ?) ORDER BY messages.id DESC LIMIT ?",
            (query.strip(), pattern, limit),
        ).fetchall()
    return [(int(mid), str(cid), str(title), str(role), str(text), str(created)) for mid, cid, title, role, text, created in rows]


def clear_history() -> int:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as db:
        count = int(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
        db.execute("DELETE FROM messages")
        db.execute("DELETE FROM conversations")
    return count


def add_bookmark(conversation_id: str, role: str, text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    _ensure_db()
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT OR IGNORE INTO conversations(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (conversation_id, "Избранное", now, now),
        )
        cursor = db.execute(
            "INSERT INTO bookmarks(conversation_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (conversation_id, role, text[:50_000], now),
        )
        return int(cursor.lastrowid)


def list_bookmarks(query: str = "") -> list[tuple[int, str, str, str]]:
    _ensure_db()
    pattern = f"%{query.strip()}%"
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT id, role, text, created_at FROM bookmarks WHERE (? = '' OR text LIKE ?) ORDER BY id DESC",
            (query.strip(), pattern),
        ).fetchall()
    return [(int(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]


def delete_bookmark(bookmark_id: int) -> bool:
    _ensure_db()
    with sqlite3.connect(DB_PATH) as db:
        cursor = db.execute("DELETE FROM bookmarks WHERE id = ?", (int(bookmark_id),))
    return cursor.rowcount > 0
