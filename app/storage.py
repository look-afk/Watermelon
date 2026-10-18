import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    # One shared instance per database file. The rules cache lives in the instance, so the bot,
    # the userbot and the panel MUST share it, otherwise a rule changed in the panel would not
    # reach the collector until the next restart.
    _instances: dict = {}
    _instances_lock = threading.Lock()

    def __new__(cls, path: str):
        key = str(Path(path).resolve())
        with cls._instances_lock:
            inst = cls._instances.get(key)
            if inst is None:
                inst = super().__new__(cls)
                inst._ready = False
                cls._instances[key] = inst
            return inst

    def __init__(self, path: str):
        if self._ready:
            return
        self.path = path
        self._lock = threading.Lock()
        self._rules = None
        self._init_schema()
        self._ready = True

    def reload(self):
        """Re-run migrations after the database file was replaced (restore) and drop cached rules."""
        self._init_schema()
        self._invalidate_rules()

    def _init_schema(self):
        parent = Path(self.path).parent
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
            for col, ddl in (
                ("telegram_message_id", "INTEGER"),
                ("sender_name", "TEXT"),
                ("reply_to_msg_id", "INTEGER"),
            ):
                if col not in columns:
                    con.execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_user ON messages(chat_id, user_id, id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)")
            con.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(chat_id, created_at)")
            con.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_telegram_id
                ON messages(chat_id, telegram_message_id)
                WHERE telegram_message_id IS NOT NULL
            """)
            con.execute("""CREATE TABLE IF NOT EXISTS sync_marks (
                chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL DEFAULT 0,
                synced_max_id INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id))""")
            if con.execute("SELECT name FROM sqlite_master WHERE name='sync_state'").fetchone():
                con.execute("""INSERT OR IGNORE INTO sync_marks (chat_id, user_id, synced_max_id, updated_at)
                               SELECT chat_id, 0, synced_max_id, updated_at FROM sync_state""")
            con.execute("""CREATE TABLE IF NOT EXISTS people (
                user_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
                is_contact INTEGER NOT NULL DEFAULT 0, updated_at TEXT)""")
            con.execute("""CREATE TABLE IF NOT EXISTS chats_meta (
                chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT, username TEXT, updated_at TEXT)""")
            con.execute("CREATE TABLE IF NOT EXISTS chat_rules (chat_id INTEGER PRIMARY KEY, is_excluded INTEGER NOT NULL DEFAULT 0)")
            con.execute("""CREATE TABLE IF NOT EXISTS person_rules (
                user_id INTEGER PRIMARY KEY, ignored INTEGER NOT NULL DEFAULT 0, tracked INTEGER NOT NULL DEFAULT 0)""")
            con.execute("""CREATE TABLE IF NOT EXISTS profiles (
                user_id INTEGER PRIMARY KEY, data TEXT, updated_at TEXT,
                msg_count INTEGER NOT NULL DEFAULT 0, error TEXT, error_at TEXT)""")
            # people that exist in older databases (before the people table)
            con.execute("""INSERT OR IGNORE INTO people (user_id, name, username, updated_at)
                           SELECT user_id, MAX(sender_name), MAX(username), ? FROM messages GROUP BY user_id""", (_now(),))

    @contextmanager
    def _connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

    # ================= rules =================
    def _load_rules(self):
        with self._connect() as con:
            excluded = {r[0] for r in con.execute("SELECT chat_id FROM chat_rules WHERE is_excluded=1")}
            ignored = {r[0] for r in con.execute("SELECT user_id FROM person_rules WHERE ignored=1")}
            tracked = {r[0] for r in con.execute("SELECT user_id FROM person_rules WHERE tracked=1")}
        return excluded, ignored, tracked

    def rules(self):
        with self._lock:
            if self._rules is None:
                self._rules = self._load_rules()
            return self._rules

    def _invalidate_rules(self):
        with self._lock:
            self._rules = None

    def should_store(self, chat_id: int, user_id: int) -> bool:
        """Ignored people are never stored. Excluded chats are not stored either,
        except messages of people explicitly marked as tracked."""
        excluded, ignored, tracked = self.rules()
        if user_id in ignored:
            return False
        if chat_id in excluded:
            return user_id in tracked
        return True

    def is_chat_excluded(self, chat_id: int) -> bool:
        return chat_id in self.rules()[0]

    def tracked_users(self) -> list[int]:
        return sorted(self.rules()[2])

    def get_excluded_chats(self) -> set[int]:
        return set(self.rules()[0])

    def get_person_rule(self, user_id: int) -> dict:
        _, ignored, tracked = self.rules()
        return {"ignored": user_id in ignored, "tracked": user_id in tracked}

    def set_chat_excluded(self, chat_id: int, excluded: bool) -> int:
        """Excluding stops collection and analysis of the chat and removes what was stored
        (except tracked people). Re-including triggers a fresh automatic backfill."""
        purged = 0
        with self._connect() as con:
            con.execute(
                "INSERT INTO chat_rules (chat_id, is_excluded) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET is_excluded=excluded.is_excluded",
                (chat_id, 1 if excluded else 0),
            )
            if excluded:
                purged = con.execute(
                    "DELETE FROM messages WHERE chat_id=? AND user_id NOT IN "
                    "(SELECT user_id FROM person_rules WHERE tracked=1)", (chat_id,)
                ).rowcount
                con.execute("DELETE FROM sync_marks WHERE chat_id=? AND user_id=0", (chat_id,))
        self._invalidate_rules()
        return purged

    def set_person_rule(self, user_id: int, ignored: bool | None = None, tracked: bool | None = None) -> int:
        purged = 0
        with self._connect() as con:
            row = con.execute("SELECT ignored, tracked FROM person_rules WHERE user_id=?", (user_id,)).fetchone()
            cur_ignored, cur_tracked = (row if row else (0, 0))
            if ignored is not None:
                cur_ignored = 1 if ignored else 0
                if cur_ignored:
                    cur_tracked = 0
            if tracked is not None:
                cur_tracked = 1 if tracked else 0
                if cur_tracked:
                    cur_ignored = 0
            con.execute(
                "INSERT INTO person_rules (user_id, ignored, tracked) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET ignored=excluded.ignored, tracked=excluded.tracked",
                (user_id, cur_ignored, cur_tracked),
            )
            if cur_ignored:
                purged = con.execute("DELETE FROM messages WHERE user_id=?", (user_id,)).rowcount
                con.execute("DELETE FROM profiles WHERE user_id=?", (user_id,))
        self._invalidate_rules()
        return purged

    # ================= writes =================
    @staticmethod
    def _upsert_people(con, people: dict):
        if not people:
            return
        con.executemany(
            """INSERT INTO people (user_id, name, username, updated_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name = COALESCE(excluded.name, people.name),
                 username = COALESCE(excluded.username, people.username),
                 updated_at = excluded.updated_at""",
            [(uid, n, u, _now()) for uid, (n, u) in people.items()],
        )

    def save_message(self, chat_id, user_id, username, message_text, telegram_message_id=None,
                     created_at=None, sender_name=None, reply_to_msg_id=None) -> int:
        with self._connect() as con:
            cur = con.execute(
                """INSERT OR IGNORE INTO messages
                   (chat_id, user_id, username, sender_name, message_text, created_at, telegram_message_id, reply_to_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, user_id, username, sender_name, message_text[:10000], created_at or _now(),
                 telegram_message_id, reply_to_msg_id),
            )
            self._upsert_people(con, {user_id: (sender_name, username)})
            return cur.rowcount

    def save_messages_bulk(self, rows: list[tuple]) -> int:
        """rows: (chat_id, user_id, username, sender_name, text, telegram_message_id, created_at, reply_to_msg_id)"""
        if not rows:
            return 0
        now = _now()
        prepared, people = [], {}
        for chat_id, user_id, username, name, text, tg_id, created_at, reply_to in rows:
            prepared.append((chat_id, user_id, username, name, (text or "")[:10000], created_at or now, tg_id, reply_to))
            people[user_id] = (name, username)
        with self._connect() as con:
            before = con.total_changes
            con.executemany(
                """INSERT OR IGNORE INTO messages
                   (chat_id, user_id, username, sender_name, message_text, created_at, telegram_message_id, reply_to_msg_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", prepared)
            inserted = con.total_changes - before
            self._upsert_people(con, people)
            return inserted

    def upsert_people(self, rows: list[tuple]):
        """rows: (user_id, name, username)"""
        with self._connect() as con:
            self._upsert_people(con, {uid: (n, u) for uid, n, u in rows})

    def set_contacts(self, rows: list[tuple]):
        """rows: (user_id, name, username) of the account's Telegram contacts."""
        with self._connect() as con:
            con.execute("UPDATE people SET is_contact=0")
            self._upsert_people(con, {uid: (n, u) for uid, n, u in rows})
            con.executemany("UPDATE people SET is_contact=1 WHERE user_id=?", [(uid,) for uid, _, _ in rows])

    def upsert_chat_meta(self, chat_id: int, title: str, ctype: str, username: str | None):
        with self._connect() as con:
            con.execute(
                """INSERT INTO chats_meta (chat_id, title, type, username, updated_at) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type,
                   username=excluded.username, updated_at=excluded.updated_at""",
                (chat_id, title, ctype, username, _now()),
            )

    def get_sync_marker(self, chat_id: int, user_id: int = 0) -> int | None:
        with self._connect() as con:
            row = con.execute("SELECT synced_max_id FROM sync_marks WHERE chat_id=? AND user_id=?",
                              (chat_id, user_id)).fetchone()
        return row[0] if row else None

    def set_sync_marker(self, chat_id: int, max_id: int, user_id: int = 0) -> None:
        with self._connect() as con:
            con.execute(
                """INSERT INTO sync_marks (chat_id, user_id, synced_max_id, updated_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET
                   synced_max_id = MAX(synced_max_id, excluded.synced_max_id), updated_at = excluded.updated_at""",
                (chat_id, user_id, max_id, _now()),
            )

    # ================= classic reads (used by the bot's /analyze) =================
    def get_messages(self, chat_id: int, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE chat_id=? AND user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (chat_id, user_id, limit)).fetchall()
        return [r[0] for r in reversed(rows)]

    def get_messages_all(self, user_id: int, limit: int) -> list[str]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT message_text FROM messages WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
                (user_id, limit)).fetchall()
        return [r[0] for r in reversed(rows)]

    # ================= panel reads =================
    def get_chat_counts(self) -> dict[int, tuple[int, str | None]]:
        with self._connect() as con:
            rows = con.execute("SELECT chat_id, COUNT(*), MAX(created_at) FROM messages GROUP BY chat_id").fetchall()
        return {cid: (cnt, last) for cid, cnt, last in rows}

    def get_totals(self) -> dict:
        with self._connect() as con:
            m = con.execute("SELECT COUNT(*), COUNT(DISTINCT chat_id), COUNT(DISTINCT user_id) FROM messages").fetchone()
            p = con.execute("SELECT COUNT(*) FROM profiles WHERE data IS NOT NULL").fetchone()
        return {"messages": m[0] or 0, "chats": m[1] or 0, "people": m[2] or 0, "profiles": p[0] or 0}

    def get_chat_summary(self, chat_id: int) -> dict:
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*), COUNT(DISTINCT user_id), MIN(created_at), MAX(created_at) FROM messages WHERE chat_id=?",
                (chat_id,)).fetchone()
            hours = [0] * 24
            for h, c in con.execute(
                    "SELECT CAST(strftime('%H', created_at) AS INTEGER), COUNT(*) FROM messages WHERE chat_id=? GROUP BY 1",
                    (chat_id,)):
                if h is not None:
                    hours[h] = c
        return {"messages": row[0] or 0, "authors": row[1] or 0, "first": row[2], "last": row[3], "hours": hours}

    def get_chat_senders(self, chat_id: int) -> dict[int, dict]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT m.user_id, COUNT(*), MIN(m.created_at), MAX(m.created_at),
                          COALESCE(p.name, MAX(m.sender_name)), COALESCE(p.username, MAX(m.username))
                   FROM messages m LEFT JOIN people p ON p.user_id = m.user_id
                   WHERE m.chat_id=? GROUP BY m.user_id""", (chat_id,)).fetchall()
        return {uid: {"message_count": c, "first_seen": f, "last_seen": l, "name": n, "username": u}
                for uid, c, f, l, n, u in rows}

    def get_chat_meta(self) -> dict[int, dict]:
        with self._connect() as con:
            rows = con.execute("SELECT chat_id, title, type, username FROM chats_meta").fetchall()
        return {cid: {"title": t, "type": ty, "username": u} for cid, t, ty, u in rows}

    def get_people_list(self) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT p.user_id, COALESCE(p.name, MAX(m.sender_name)), COALESCE(p.username, MAX(m.username)),
                          p.is_contact, COUNT(m.id), COUNT(DISTINCT m.chat_id), MAX(m.created_at),
                          COALESCE(r.ignored, 0), COALESCE(r.tracked, 0)
                   FROM people p
                   LEFT JOIN messages m ON m.user_id = p.user_id
                   LEFT JOIN person_rules r ON r.user_id = p.user_id
                   GROUP BY p.user_id""").fetchall()
        return [{"id": uid, "name": n, "username": u, "is_contact": bool(ic), "messages": c, "chats": ch,
                 "last_seen": last, "ignored": bool(ig), "tracked": bool(tr)}
                for uid, n, u, ic, c, ch, last, ig, tr in rows]

    def get_person(self, user_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT user_id, name, username, is_contact FROM people WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return None
        return {"id": row[0], "name": row[1], "username": row[2], "is_contact": bool(row[3])}

    def get_person_rows(self, user_id: int, limit: int = 5000) -> list[tuple]:
        """(created_at, text, chat_id, telegram_message_id, reply_to_msg_id), newest first"""
        with self._connect() as con:
            return con.execute(
                "SELECT created_at, message_text, chat_id, telegram_message_id, reply_to_msg_id FROM messages "
                "WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?", (user_id, limit)).fetchall()

    def get_person_chats(self, user_id: int) -> list[dict]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT m.chat_id, COUNT(*), MAX(m.created_at), cm.title, cm.type, COALESCE(cr.is_excluded, 0)
                   FROM messages m
                   LEFT JOIN chats_meta cm ON cm.chat_id = m.chat_id
                   LEFT JOIN chat_rules cr ON cr.chat_id = m.chat_id
                   WHERE m.user_id=? GROUP BY m.chat_id ORDER BY COUNT(*) DESC""", (user_id,)).fetchall()
        return [{"id": cid, "count": c, "last": last, "title": t or f"Чат {cid}", "type": ty or "group",
                 "excluded": bool(ex)} for cid, c, last, t, ty, ex in rows]

    def get_person_activity(self, user_id: int, limit: int = 20, offset: int = 0) -> tuple[list[dict], bool]:
        with self._connect() as con:
            rows = con.execute(
                """SELECT m.created_at, m.message_text, m.chat_id, cm.title, cm.type,
                          rp.name, rm.message_text
                   FROM messages m
                   LEFT JOIN chats_meta cm ON cm.chat_id = m.chat_id
                   LEFT JOIN messages rm ON rm.chat_id = m.chat_id AND rm.telegram_message_id = m.reply_to_msg_id
                   LEFT JOIN people rp ON rp.user_id = rm.user_id
                   WHERE m.user_id=? ORDER BY m.created_at DESC, m.id DESC LIMIT ? OFFSET ?""",
                (user_id, limit + 1, offset)).fetchall()
        items = [{"date": d, "text": t, "chat_id": cid, "chat": ct or f"Чат {cid}", "chat_type": cty or "group",
                  "reply_to_name": rn, "reply_to_text": (rt or "")[:120] if rt else None}
                 for d, t, cid, ct, cty, rn, rt in rows[:limit]]
        return items, len(rows) > limit

    def get_reply_delays(self, user_id: int, me_id: int) -> dict:
        """Seconds between my message and the person's next message (and vice versa), within one hour."""
        with self._connect() as con:
            rows = con.execute(
                """WITH ord AS (
                       SELECT user_id AS uid, LAG(user_id) OVER w AS pu,
                              CAST(strftime('%s', created_at) AS INTEGER) AS ts,
                              CAST(strftime('%s', LAG(created_at) OVER w) AS INTEGER) AS pts
                       FROM messages
                       WHERE chat_id IN (SELECT DISTINCT chat_id FROM messages WHERE user_id=:u)
                       WINDOW w AS (PARTITION BY chat_id ORDER BY created_at, id))
                   SELECT uid, ts - pts FROM ord
                   WHERE pts IS NOT NULL AND ts - pts BETWEEN 0 AND 3600
                     AND ((uid=:u AND pu=:me) OR (uid=:me AND pu=:u))""", {"u": user_id, "me": me_id}).fetchall()
        to_me = sorted(d for uid, d in rows if uid == user_id)
        from_me = sorted(d for uid, d in rows if uid == me_id)

        def med(a):
            return a[len(a) // 2] if a else None
        return {"to_me_sec": med(to_me), "to_me_n": len(to_me), "from_me_sec": med(from_me), "from_me_n": len(from_me)}

    def get_connections(self, user_id: int, me_id: int, my_username: str | None, limit: int = 12) -> list[dict]:
        """Who this person talks to. Score = quick back-and-forth + replies + @mentions + shared chats."""
        since = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat()
        score: dict[int, dict] = {}

        def slot(uid):
            return score.setdefault(uid, {"near": 0, "replies_out": 0, "replies_in": 0, "mentions": 0, "shared": 0, "dm": 0})

        with self._connect() as con:
            # 1) quick conversational adjacency (<= 3 min between messages of two different people)
            for partner, cnt in con.execute(
                """WITH ord AS (
                       SELECT user_id AS uid, LAG(user_id) OVER w AS pu,
                              CAST(strftime('%s', created_at) AS INTEGER) AS ts,
                              CAST(strftime('%s', LAG(created_at) OVER w) AS INTEGER) AS pts
                       FROM messages
                       WHERE created_at >= :since
                         AND chat_id IN (SELECT DISTINCT chat_id FROM messages WHERE user_id=:u)
                       WINDOW w AS (PARTITION BY chat_id ORDER BY created_at, id))
                   SELECT CASE WHEN uid=:u THEN pu ELSE uid END, COUNT(*) FROM ord
                   WHERE pu IS NOT NULL AND pu != uid AND (uid=:u OR pu=:u) AND ts - pts BETWEEN 0 AND 180
                   GROUP BY 1""", {"u": user_id, "since": since}):
                if partner is not None:
                    slot(partner)["near"] = cnt
            # 2) explicit replies, both directions
            for partner, cnt in con.execute(
                """SELECT r.user_id, COUNT(*) FROM messages m
                   JOIN messages r ON r.chat_id=m.chat_id AND r.telegram_message_id=m.reply_to_msg_id
                   WHERE m.user_id=? AND r.user_id != m.user_id GROUP BY r.user_id""", (user_id,)):
                slot(partner)["replies_out"] = cnt
            for partner, cnt in con.execute(
                """SELECT m.user_id, COUNT(*) FROM messages m
                   JOIN messages r ON r.chat_id=m.chat_id AND r.telegram_message_id=m.reply_to_msg_id
                   WHERE r.user_id=? AND m.user_id != r.user_id GROUP BY m.user_id""", (user_id,)):
                slot(partner)["replies_in"] = cnt
            # 3) @mentions
            person = con.execute("SELECT username FROM people WHERE user_id=?", (user_id,)).fetchone()
            uname = (person[0] if person else None)
            if uname:
                for partner, cnt in con.execute(
                    "SELECT user_id, COUNT(*) FROM messages WHERE user_id != ? AND message_text LIKE ? ESCAPE '\\' GROUP BY user_id",
                    (user_id, "%@" + _like_escape(uname) + "%")):
                    slot(partner)["mentions"] += cnt
            by_username = {u.lower(): i for i, u in con.execute(
                "SELECT user_id, username FROM people WHERE username IS NOT NULL")}
            import re
            for (text,) in con.execute("SELECT message_text FROM messages WHERE user_id=? AND message_text LIKE '%@%' "
                                       "ORDER BY created_at DESC LIMIT 1500", (user_id,)):
                for handle in re.findall(r"@([A-Za-z0-9_]{4,32})", text):
                    pid = by_username.get(handle.lower())
                    if pid and pid != user_id:
                        slot(pid)["mentions"] += 1
            # 4) shared chats
            for partner, cnt in con.execute(
                """SELECT m2.user_id, COUNT(DISTINCT m2.chat_id) FROM messages m1
                   JOIN messages m2 ON m2.chat_id=m1.chat_id AND m2.user_id != m1.user_id
                   WHERE m1.user_id=? GROUP BY m2.user_id""", (user_id,)):
                slot(partner)["shared"] = cnt
            # 5) direct messages with the owner (private chat id == user id)
            dm = con.execute("SELECT COUNT(*) FROM messages WHERE chat_id=?", (user_id,)).fetchone()[0]
            if dm:
                slot(me_id)["dm"] = dm

            names = {i: (n, u) for i, n, u in con.execute("SELECT user_id, name, username FROM people")}
            ignored = self.rules()[1]

        out = []
        for uid, s in score.items():
            if uid == user_id or uid in ignored:
                continue
            value = s["near"] + 3 * (s["replies_out"] + s["replies_in"]) + 2 * s["mentions"] + min(s["shared"], 5) + 0.3 * s["dm"]
            if value <= 0:
                continue
            n, u = names.get(uid, (None, None))
            out.append({"id": uid, "name": n or (f"ID {uid}"), "username": u, "is_me": uid == me_id, "score": round(value, 1), **s})
        out.sort(key=lambda x: -x["score"])
        out = out[:limit]
        top = out[0]["score"] if out else 1
        for item in out:
            item["strength"] = round(100 * item["score"] / top)
            item["close"] = item["score"] >= 12 and item["score"] >= 0.35 * top
        return out

    def get_context_rows(self, user_id: int, me_id: int, max_person: int = 150) -> dict:
        with self._connect() as con:
            person = con.execute(
                "SELECT chat_id, telegram_message_id, created_at, message_text, reply_to_msg_id FROM messages "
                "WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?", (user_id, max_person)).fetchall()
            chat_ids = sorted({r[0] for r in person})
            mine = []
            if chat_ids:
                marks = ",".join("?" * len(chat_ids))
                # include the owner's messages shortly BEFORE the oldest message (replied-to context)
                first = datetime.fromisoformat(min(r[2] for r in person))
                oldest = (first - timedelta(hours=2)).isoformat()
                mine = con.execute(
                    f"SELECT chat_id, telegram_message_id, created_at, message_text FROM messages "
                    f"WHERE user_id=? AND chat_id IN ({marks}) AND created_at >= ? ORDER BY created_at DESC LIMIT 800",
                    [me_id, *chat_ids, oldest]).fetchall()
            titles = {c: (t or f"Чат {c}", ty or "group") for c, t, ty in con.execute(
                "SELECT chat_id, title, type FROM chats_meta")}
        return {"person": list(reversed(person)), "mine": list(reversed(mine)), "titles": titles}

    # ================= profiles =================
    def get_profile(self, user_id: int) -> dict | None:
        with self._connect() as con:
            row = con.execute("SELECT data, updated_at, msg_count, error, error_at FROM profiles WHERE user_id=?",
                              (user_id,)).fetchone()
        if not row:
            return None
        data, updated, count, error, error_at = row
        return {"data": json.loads(data) if data else None, "updated_at": updated, "msg_count": count,
                "error": error, "error_at": error_at}

    def save_profile(self, user_id: int, data: dict, msg_count: int):
        with self._connect() as con:
            con.execute(
                """INSERT INTO profiles (user_id, data, updated_at, msg_count, error, error_at) VALUES (?, ?, ?, ?, NULL, NULL)
                   ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at,
                   msg_count=excluded.msg_count, error=NULL, error_at=NULL""",
                (user_id, json.dumps(data, ensure_ascii=False), _now(), msg_count))

    def save_profile_error(self, user_id: int, error: str):
        with self._connect() as con:
            con.execute(
                """INSERT INTO profiles (user_id, data, updated_at, msg_count, error, error_at) VALUES (?, NULL, NULL, 0, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET error=excluded.error, error_at=excluded.error_at""",
                (user_id, error[:500], _now()))

    def get_headlines(self) -> dict[int, dict]:
        with self._connect() as con:
            rows = con.execute("SELECT user_id, data, updated_at FROM profiles WHERE data IS NOT NULL").fetchall()
        out = {}
        for uid, data, updated in rows:
            try:
                d = json.loads(data)
            except ValueError:
                continue
            out[uid] = {
                "summary": (d.get("summary") or "")[:140],
                "attitude": (d.get("attitude_to_me") or {}).get("label"),
                "hostility": (d.get("attitude_to_me") or {}).get("hostility_score"),
                "usefulness": (d.get("usefulness_to_me") or {}).get("score"),
                "relation": (d.get("relationship_guess") or {}).get("label"),
                "updated_at": updated,
            }
        return out

    def people_needing_analysis(self, min_messages: int, refresh_new: int, limit: int, exclude_id: int) -> list[int]:
        backoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self._connect() as con:
            rows = con.execute(
                """SELECT m.user_id, COUNT(*) AS c, COALESCE(p.msg_count, 0) AS pc
                   FROM messages m
                   LEFT JOIN profiles p ON p.user_id = m.user_id
                   LEFT JOIN person_rules r ON r.user_id = m.user_id
                   WHERE m.user_id != ? AND COALESCE(r.ignored, 0) = 0
                     AND (p.error_at IS NULL OR p.error_at < ?)
                   GROUP BY m.user_id
                   HAVING c >= ? AND (p.data IS NULL OR c - pc >= ?)
                   ORDER BY (c - pc) DESC LIMIT ?""",
                (exclude_id, backoff, min_messages, refresh_new, limit)).fetchall()
        return [r[0] for r in rows]
