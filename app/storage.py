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
                    created_at TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_user ON messages(chat_id, user_id, id)")

    def _connect(self):
        return sqlite3.connect(self.path)

    def save_message(self, chat_id: int, user_id: int, username: str | None, message_text: str):
        with self._connect() as con:
            con.execute(
                "INSERT INTO messages(chat_id,user_id,username,message_text,created_at) VALUES(?,?,?,?,?)",
                (chat_id, user_id, username, message_text[:10000], datetime.now(timezone.utc).isoformat())
            )

    def get_messages(self, chat_id: int, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE chat_id=? AND user_id=? ORDER BY id DESC LIMIT ?",
                (chat_id, user_id, limit)
            ).fetchall()
        return [row[0] for row in reversed(rows)]
