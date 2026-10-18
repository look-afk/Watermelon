import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Storage:
    def __init__(self, path: str):
        self.path = path
        parent = Path(path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
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
            if "sender_name" not in columns:
                con.execute("ALTER TABLE messages ADD COLUMN sender_name TEXT")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_user ON messages(chat_id, user_id, id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, created_at)")
            con.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_telegram_id
                ON messages(chat_id, telegram_message_id)
                WHERE telegram_message_id IS NOT NULL
            """)
            # Remembers how far each chat has been synced, so the automatic sync can be
            # incremental and never skips old history because a live message arrived first.
            con.execute("""
                CREATE TABLE IF NOT EXISTS sync_state (
                    chat_id INTEGER PRIMARY KEY,
                    synced_max_id INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

    # ---------- writes ----------
    def save_message(
        self,
        chat_id: int,
        user_id: int,
        username: str | None,
        message_text: str,
        telegram_message_id: int | None = None,
        created_at: str | None = None,
        sender_name: str | None = None,
    ) -> int:
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO messages
                    (chat_id, user_id, username, sender_name, message_text, created_at, telegram_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, username, sender_name, message_text[:10000], timestamp, telegram_message_id),
            )
            return cur.rowcount

    def save_messages_bulk(self, rows: list[tuple]) -> int:
        """rows: (chat_id, user_id, username, sender_name, message_text, telegram_message_id, created_at).
        One transaction for the whole batch; returns how many rows were really inserted."""
        if not rows:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        prepared = [
            (chat_id, user_id, username, name, (text or "")[:10000], created_at or now, tg_id)
            for chat_id, user_id, username, name, text, tg_id, created_at in rows
        ]
        with self._connect() as con:
            before = con.total_changes
            con.executemany(
                """
                INSERT OR IGNORE INTO messages
                    (chat_id, user_id, username, sender_name, message_text, created_at, telegram_message_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                prepared,
            )
            return con.total_changes - before

    def get_sync_marker(self, chat_id: int) -> int | None:
        with self._connect() as con:
            row = con.execute("SELECT synced_max_id FROM sync_state WHERE chat_id=?", (chat_id,)).fetchone()
        return row[0] if row else None

    def set_sync_marker(self, chat_id: int, max_id: int) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT INTO sync_state (chat_id, synced_max_id, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    synced_max_id = MAX(synced_max_id, excluded.synced_max_id),
                    updated_at = excluded.updated_at
                """,
                (chat_id, max_id, datetime.now(timezone.utc).isoformat()),
            )

    # ---------- analysis reads ----------
    def get_messages(self, chat_id: int, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE chat_id=? AND user_id=? "
                "ORDER BY created_at DESC, id DESC LIMIT ?",
                (chat_id, user_id, limit),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    def get_messages_all(self, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [row[0] for row in reversed(rows)]

    # ---------- panel reads ----------
    def get_user_stats(self, chat_id: int, user_id: int) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*), MAX(created_at) FROM messages WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()
        return {"message_count": row[0] or 0, "last_seen": row[1]}

    def get_chat_counts(self) -> dict[int, tuple[int, str | None]]:
        """{chat_id: (stored messages, last message time)} for all chats in one query."""
        with self._connect() as con:
            rows = con.execute("SELECT chat_id, COUNT(*), MAX(created_at) FROM messages GROUP BY chat_id").fetchall()
        return {cid: (cnt, last) for cid, cnt, last in rows}

    def get_totals(self) -> dict:
        with self._connect() as con:
            row = con.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id) FROM messages").fetchone()
        return {"messages": row[0] or 0, "chats": row[1] or 0}

    def get_chat_summary(self, chat_id: int) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT user_id), MIN(created_at), MAX(created_at) FROM messages WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return {"messages": row[0] or 0, "authors": row[1] or 0, "first": row[2], "last": row[3]}

    def get_chat_senders(self, chat_id: int) -> dict[int, dict]:
        """Everyone who wrote in the chat, with stats, in ONE query."""
        with self._connect() as con:
            rows = con.execute(
                """
                SELECT user_id, COUNT(*), MIN(created_at), MAX(created_at), MAX(sender_name), MAX(username)
                FROM messages WHERE chat_id=? GROUP BY user_id
                """,
                (chat_id,),
            ).fetchall()
        return {
            uid: {"message_count": cnt, "first_seen": first, "last_seen": last, "name": name, "username": uname}
            for uid, cnt, first, last, name, uname in rows
        }

    def get_chat_stats(self, chat_id: int) -> dict[int, dict]:
        return {uid: {"message_count": s["message_count"], "last_seen": s["last_seen"]}
                for uid, s in self.get_chat_senders(chat_id).items()}

    def get_chat_messages(
        self, chat_id: int, user_id: int | None = None, limit: int = 40, offset: int = 0, query: str | None = None
    ) -> list[dict]:
        sql = ["SELECT user_id, username, sender_name, message_text, created_at FROM messages WHERE chat_id=?"]
        params: list = [chat_id]
        if user_id is not None:
            sql.append("AND user_id=?")
            params.append(user_id)
        if query:
            sql.append("AND message_text LIKE ? ESCAPE '\\'")
            params.append("%" + _like_escape(query) + "%")
        sql.append("ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?")
        params += [limit, offset]
        with self._connect() as con:
            rows = con.execute(" ".join(sql), params).fetchall()
        return [
            {"user_id": uid, "username": uname, "name": name, "text": text, "date": created}
            for uid, uname, name, text, created in rows
        ]
