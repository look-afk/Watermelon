import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class Storage:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path(".") else None
        with self._connect() as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    message_text TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    telegram_message_id INTEGER
                )
            """)
            columns = {row[1] for row in con.execute("PRAGMA table_info(messages)")}
            if "telegram_message_id" not in columns:
                con.execute("ALTER TABLE messages ADD COLUMN telegram_message_id INTEGER")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_user ON messages(chat_id, user_id, id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)")
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_telegram_id
                ON messages(chat_id, telegram_message_id)
                WHERE telegram_message_id IS NOT NULL
            """)

    def _connect(self):
        return sqlite3.connect(self.path)

    def save_message(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        message_text: str,
        telegram_message_id: int | None = None,
        created_at: str | None = None,
    ):
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute(
                """
                INSERT OR IGNORE INTO messages
                    (chat_id, user_id, username, message_text, created_at, telegram_message_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, username, message_text[:10000], timestamp, telegram_message_id),
            )

    def get_messages(self, chat_id: int, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, user_id, limit),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    def get_messages_all(self, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    def get_user_stats(self, chat_id: int, user_id: int) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*), MAX(created_at) FROM messages WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
        return {"message_count": row[0] or 0, "last_seen": row[1]}
