"""Tables and small helpers for the assistant features. Lives in the same SQLite file (so it is backed up too)."""
import contextlib
import json
import re
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS assist_kv(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS commitments(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, tg_message_id INTEGER,
    person_id INTEGER, person_name TEXT, direction TEXT NOT NULL, what TEXT NOT NULL,
    due_at TEXT, due_text TEXT, status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL, said_at TEXT, notified_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commit_uniq ON commitments(chat_id, tg_message_id, what);
CREATE TABLE IF NOT EXISTS auto_chats(
    chat_id INTEGER PRIMARY KEY, title TEXT, enabled INTEGER NOT NULL DEFAULT 1, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS auto_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, at TEXT, kind TEXT, text TEXT);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat()


def parse_dt(value):
    try:
        d = datetime.fromisoformat(value)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ruhas(text, needle):
    # SQLite's LIKE/lower() only fold ASCII, so Cyrillic needs this.
    return 1 if text and needle in text.lower() else 0


@contextlib.contextmanager
def db(storage):
    with storage._connect() as con:
        con.create_function("ruhas", 2, _ruhas)
        yield con


def init(storage):
    with db(storage) as con:
        con.executescript(SCHEMA)


def kv_get(storage, key: str, default=None):
    with db(storage) as con:
        row = con.execute("SELECT value FROM assist_kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except ValueError:
        return default


def kv_set(storage, key: str, value):
    with db(storage) as con:
        con.execute("INSERT INTO assist_kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)))


_STOP = set("и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня "
            "еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам "
            "ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто "
            "чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти "
            "мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над "
            "больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда "
            "лучше чуть том нельзя такой им более всегда конечно всю между говорил говорила сказал писал спросить найди "
            "найти покажи расскажи было были мне мои мой моя наши чат чате чаты".split())


def stem(word: str) -> str:
    w = re.sub(r"[^\w-]", "", word.lower())
    if len(w) >= 8:
        return w[:-3]
    if len(w) >= 6:
        return w[:-2]
    if len(w) == 5:
        return w[:-1]
    return w


def keywords(text: str, limit: int = 8) -> list[str]:
    seen, out = set(), []
    for token in re.findall(r"[\w@-]{3,}", text.lower()):
        if token in _STOP:
            continue
        s = stem(token)
        if len(s) >= 3 and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:limit]


def find_people(storage, query: str, limit: int = 6) -> list[dict]:
    q = query.strip().lstrip("@").lower()
    if not q:
        return []
    with db(storage) as con:
        if q.lstrip("-").isdigit():
            rows = con.execute("SELECT user_id, name, username FROM people WHERE user_id=?", (int(q),)).fetchall()
        else:
            rows = con.execute(
                """SELECT p.user_id, p.name, p.username, COUNT(m.id) c FROM people p
                   LEFT JOIN messages m ON m.user_id = p.user_id
                   WHERE ruhas(COALESCE(p.name,''), ?) OR lower(COALESCE(p.username,'')) LIKE ?
                   GROUP BY p.user_id ORDER BY (lower(COALESCE(p.username,''))=?) DESC, c DESC LIMIT ?""",
                (q, "%" + q + "%", q, limit)).fetchall()
    return [{"id": r[0], "name": r[1] or (f"@{r[2]}" if r[2] else f"ID {r[0]}"), "username": r[2]} for r in rows]


def find_chats(storage, query: str, limit: int = 6) -> list[dict]:
    q = query.strip().lower()
    with db(storage) as con:
        rows = con.execute("SELECT chat_id, title, type FROM chats_meta WHERE ruhas(COALESCE(title,''), ?) LIMIT ?",
                           (q, limit)).fetchall()
    return [{"id": r[0], "title": r[1], "type": r[2]} for r in rows]
