"""Promises/deadlines, the daily digest, alerts and the scheduler that runs them."""
import asyncio
import os
import re
from datetime import timedelta

import httpx

from . import llm
from .assistdb import db, init, iso, kv_get, kv_set, now, parse_dt
from .config import settings

UTC_OFFSET = float(os.getenv("OWNER_UTC_OFFSET", "5"))  # hours; Tashkent = 5


def local_now():
    return now() + timedelta(hours=UTC_OFFSET)


def _clip(text, n=160):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


async def bot_send(text: str, owner_id: int, markup: dict | None = None):
    """Send a message through the Bot API (works from any background task, no Bot object needed)."""
    if not settings.bot_token or not owner_id:
        return
    payload = {"chat_id": owner_id, "text": text[:3900], "disable_web_page_preview": True}
    if markup:
        payload["reply_markup"] = markup
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            await client.post(f"https://api.telegram.org/bot{settings.bot_token}/sendMessage", json=payload)
    except Exception as exc:
        print(f"Не удалось отправить сообщение владельцу: {type(exc).__name__}")


# ====================================================================== promises
HINT = re.compile(
    r"(обещ|пришл[юё]|скин[уь]|отправл[юя]|сдела[юе]|напиш[уе]|позвон|созвон|договор[иеь]|до\s+(пятниц|понедельник|вторник|сред|"
    r"четверг|суббот|воскресень|завтра|вечера|утра|конца|\d)|завтра|послезавтра|на\s+(этой|следующей)\s+неделе|дедлайн|срок|"
    r"не\s+забуд|напомни|жду\s|когда\s+(будет|скин|пришл)|успе[еш]|вернусь|потом\s+(скин|напиш|сдела)|"
    r"promise|will send|deadline|tomorrow|by\s+(monday|tuesday|wednesday|thursday|friday))", re.I)

COMMIT_PROMPT = """Ты находишь в переписке реальные обязательства: обещания, договорённости, просьбы с ожиданием действия, дедлайны.
Каждый фрагмент — несколько реплик; последняя строка — кандидат. «Я» — владелец аккаунта.
Верни JSON: {"items":[{"i":номер, "direction":"i_owe|they_owe", "person":"имя другой стороны", "what":"что именно, до 12 слов",
"due":"YYYY-MM-DD или null", "due_text":"как сказано"}]}.
i_owe — должен сделать владелец; they_owe — должен сделать другой. Относительные сроки («завтра», «в пятницу») переводи в дату
от даты сообщения. Болтовню, шутки, неопределённое «потом как-нибудь» и уже выполненное не включай. Если обязательств нет — {"items":[]}."""


def candidate_windows(storage, since_iso: str, owner_id: int, limit: int = 60) -> list[dict]:
    with db(storage) as con:
        rows = con.execute(
            """SELECT m.id, m.chat_id, m.user_id, m.created_at, m.message_text, m.telegram_message_id,
                      COALESCE(p.name, m.sender_name, m.username, 'ID '||m.user_id), ct.title
               FROM messages m LEFT JOIN people p ON p.user_id=m.user_id LEFT JOIN chats_meta ct ON ct.chat_id=m.chat_id
               WHERE m.created_at > ? ORDER BY m.created_at LIMIT 6000""", (since_iso,)).fetchall()
        windows = []
        for mid, chat_id, uid, created, text, tg_id, author, title in rows:
            if not text or len(text) < 6 or not HINT.search(text):
                continue
            prev = con.execute(
                """SELECT m.user_id, COALESCE(p.name, m.sender_name, 'ID '||m.user_id), m.message_text FROM messages m
                   LEFT JOIN people p ON p.user_id=m.user_id WHERE m.chat_id=? AND m.created_at < ? AND m.created_at > ?
                   ORDER BY m.created_at DESC LIMIT 3""",
                (chat_id, created, iso(parse_dt(created) - timedelta(hours=6)))).fetchall()[::-1]
            lines = [f"{'Я' if u == owner_id else n}: {_clip(t, 220)}" for u, n, t in prev]
            lines.append(f"{'Я' if uid == owner_id else author}: {_clip(text, 300)}")
            windows.append({"chat_id": chat_id, "tg_id": tg_id, "user_id": uid, "author": author, "date": created,
                            "kind": "private" if chat_id > 0 else f"group «{title or chat_id}»", "lines": lines})
            if len(windows) >= limit:
                break
    return windows


async def scan_commitments(storage, owner_id: int) -> int:
    if not llm.available():
        return 0
    since = kv_get(storage, "commit_since") or iso(now() - timedelta(days=2))
    windows = await asyncio.to_thread(candidate_windows, storage, since, owner_id)
    if not windows:
        kv_set(storage, "commit_since", iso())
        return 0
    added = 0
    for start in range(0, len(windows), 15):
        batch = windows[start:start + 15]
        payload = [{"i": i, "date": w["date"][:10], "chat": w["kind"], "lines": w["lines"]} for i, w in enumerate(batch)]
        try:
            data = await llm.achat_json([{"role": "system", "content": COMMIT_PROMPT},
                                         {"role": "user", "content": __import__("json").dumps(payload, ensure_ascii=False)}],
                                        temperature=0.1)
        except Exception as exc:
            print(f"Поиск обещаний: ИИ не ответил ({type(exc).__name__})")
            return added  # keep the marker: this period is retried next time
        for item in (data.get("items") or []) if isinstance(data, dict) else []:
            try:
                w = batch[int(item["i"])]
                direction = item["direction"]
                what = str(item["what"]).strip()[:200]
            except (KeyError, ValueError, IndexError, TypeError):
                continue
            if direction not in ("i_owe", "they_owe") or not what:
                continue
            due = str(item.get("due") or "")[:10]
            due_at = due if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due) else None
            person_id = w["user_id"] if direction == "they_owe" else (w["chat_id"] if w["chat_id"] > 0 else None)
            with db(storage) as con:
                cur = con.execute(
                    """INSERT OR IGNORE INTO commitments(chat_id, tg_message_id, person_id, person_name, direction, what,
                       due_at, due_text, created_at, said_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (w["chat_id"], w["tg_id"], person_id, str(item.get("person") or w["author"])[:80], direction, what,
                     due_at, str(item.get("due_text") or "")[:80], iso(), w["date"]))
                added += cur.rowcount
    kv_set(storage, "commit_since", iso())
    return added


def list_commitments(storage, status: str = "open", limit: int = 40) -> list[dict]:
    with db(storage) as con:
        rows = con.execute("SELECT id, direction, person_name, what, due_at, due_text, chat_id FROM commitments "
                           "WHERE status=? ORDER BY COALESCE(due_at,'9999'), id DESC LIMIT ?", (status, limit)).fetchall()
    return [dict(zip(("id", "direction", "person", "what", "due", "due_text", "chat_id"), r)) for r in rows]


def set_commitment(storage, cid: int, status: str) -> bool:
    with db(storage) as con:
        return con.execute("UPDATE commitments SET status=? WHERE id=?", (status, cid)).rowcount > 0


def format_commitment(c: dict) -> str:
    due = f" — до {c['due'][8:10]}.{c['due'][5:7]}" if c["due"] else (f" — {c['due_text']}" if c["due_text"] else "")
    overdue = c["due"] and c["due"] < local_now().strftime("%Y-%m-%d")
    return f"{'⚠️ ' if overdue else ''}{c['person'] or '?'}: {c['what']}{due}"


# ====================================================================== digest
def unanswered(storage, owner_id: int, username: str | None) -> dict:
    t7, t2h = iso(now() - timedelta(days=7)), iso(now() - timedelta(hours=2))
    with db(storage) as con:
        private = con.execute(
            """SELECT * FROM (SELECT m.chat_id, m.user_id, m.created_at, m.message_text,
                      COALESCE(p.name, p.username, 'ID '||m.chat_id) AS nm,
                      ROW_NUMBER() OVER (PARTITION BY m.chat_id ORDER BY m.created_at DESC, m.id DESC) AS rn
               FROM messages m LEFT JOIN people p ON p.user_id=m.chat_id WHERE m.chat_id>0 AND m.created_at>=?)
               WHERE rn=1 AND user_id!=? AND created_at<=? ORDER BY created_at DESC LIMIT 8""", (t7, owner_id, t2h)).fetchall()
        mentions = []
        if username:
            mentions = con.execute(
                """SELECT m.chat_id, m.created_at, m.message_text, COALESCE(p.name, m.sender_name, 'ID '||m.user_id), ct.title
                   FROM messages m LEFT JOIN people p ON p.user_id=m.user_id LEFT JOIN chats_meta ct ON ct.chat_id=m.chat_id
                   WHERE m.chat_id<0 AND m.user_id!=? AND m.created_at>=? AND ruhas(m.message_text, ?)
                     AND NOT EXISTS (SELECT 1 FROM messages o WHERE o.chat_id=m.chat_id AND o.user_id=? AND o.created_at>m.created_at)
                   ORDER BY m.created_at DESC LIMIT 6""", (owner_id, iso(now() - timedelta(days=2)), "@" + username.lower(), owner_id)).fetchall()
    return {"private": private, "mentions": mentions}


def chat_samples(storage, hours: int = 24, top: int = 5) -> list[dict]:
    since = iso(now() - timedelta(hours=hours))
    out = []
    with db(storage) as con:
        chats = con.execute("""SELECT m.chat_id, COALESCE(ct.title, 'чат '||m.chat_id), COUNT(*) c FROM messages m
                               LEFT JOIN chats_meta ct ON ct.chat_id=m.chat_id WHERE m.chat_id<0 AND m.created_at>=?
                               GROUP BY m.chat_id ORDER BY c DESC LIMIT ?""", (since, top)).fetchall()
        for chat_id, title, count in chats:
            rows = con.execute("""SELECT COALESCE(p.name, m.sender_name, 'ID '||m.user_id), m.message_text FROM messages m
                                  LEFT JOIN people p ON p.user_id=m.user_id WHERE m.chat_id=? AND m.created_at>=?
                                  ORDER BY m.created_at""", (chat_id, since)).fetchall()
            step = max(1, len(rows) // 45)
            out.append({"chat_id": chat_id, "title": title, "count": count,
                        "sample": [f"{n}: {_clip(t, 150)}" for n, t in rows[::step][:45]]})
    return out


DIGEST_PROMPT = """Для каждого чата сделай выжимку суток по репликам: главные темы, решения, важное для владельца.
2–3 коротких предложения на чат, без воды. Верни JSON {"chats":[{"chat_id":число,"text":"..."}]}."""


async def build_digest(storage, owner_id: int, username: str | None, hours: int = 24) -> str:
    out = [f"Сводка · {local_now():%d.%m %H:%M}"]
    un = await asyncio.to_thread(unanswered, storage, owner_id, username)
    if un["private"]:
        out.append("\nВам не ответили вы:")
        for chat_id, uid, created, text, name, _ in un["private"]:
            d = parse_dt(created)
            age = int((now() - d).total_seconds() // 3600)
            out.append(f"• {name} ({age} ч назад): «{_clip(text, 90)}»")
    if un["mentions"]:
        out.append("\nУпоминания без ответа:")
        for chat_id, created, text, author, title in un["mentions"]:
            out.append(f"• {title or chat_id} — {author}: «{_clip(text, 90)}»")
    todo = await asyncio.to_thread(list_commitments, storage)
    today = local_now().strftime("%Y-%m-%d")
    soon = [c for c in todo if c["due"] and c["due"] <= (local_now() + timedelta(days=2)).strftime("%Y-%m-%d")]
    if soon:
        out.append("\nСроки:")
        out += [f"• {'вы должны' if c['direction'] == 'i_owe' else 'вам должны'} — {format_commitment(c)}" for c in soon[:8]]
    samples = await asyncio.to_thread(chat_samples, storage, hours)
    if samples:
        out.append("\nЧто в чатах:")
        summaries = {}
        try:
            data = await llm.achat_json([{"role": "system", "content": DIGEST_PROMPT},
                                         {"role": "user", "content": __import__("json").dumps(
                                             [{"chat_id": s["chat_id"], "chat": s["title"], "messages": s["sample"]} for s in samples],
                                             ensure_ascii=False)}], temperature=0.3)
            summaries = {int(i["chat_id"]): str(i["text"]) for i in data.get("chats", []) if "chat_id" in i}
        except Exception:
            pass
        for s in samples:
            out.append(f"• {s['title']} ({s['count']} сообщ.): " + (summaries.get(s["chat_id"]) or "много активности, ИИ-выжимка недоступна"))
    auto = await asyncio.to_thread(auto_activity, storage, hours)
    if auto:
        out.append("\n" + auto)
    if len(out) == 1:
        out.append("Тихо: нечего отметить.")
    return "\n".join(out)


def auto_activity(storage, hours: int) -> str:
    with db(storage) as con:
        rows = con.execute("""SELECT COALESCE(a.title, 'чат '||l.chat_id), COUNT(*) FROM auto_log l LEFT JOIN auto_chats a ON a.chat_id=l.chat_id
                              WHERE l.kind='sent' AND l.at>=? GROUP BY l.chat_id""", (iso(now() - timedelta(hours=hours)),)).fetchall()
    return ("Автоответчик написал: " + ", ".join(f"{t} — {n}" for t, n in rows)) if rows else ""


# ====================================================================== alerts
def mention_alert_text(chat_title: str, author: str, text: str, why: str) -> str:
    return f"🔔 {why} · {chat_title}\n{author}: {_clip(text, 300)}"


_ALERT_LAST: dict[int, float] = {}


def alert_due(chat_id: int, gap: float = 300.0) -> bool:
    import time
    t = time.monotonic()
    if t - _ALERT_LAST.get(chat_id, -1e9) < gap:
        return False
    _ALERT_LAST[chat_id] = t
    return True


# ====================================================================== scheduler
async def scheduler_loop(storage, get_owner_id, get_username):
    """Every minute: scan promises (every 30 min), send the digest at the chosen hour."""
    last_scan = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            owner_id = get_owner_id()
            if owner_id:
                if loop.time() - last_scan > 1800:
                    last_scan = loop.time()
                    added = await scan_commitments(storage, owner_id)
                    if added:
                        print(f"Найдено новых обязательств: {added}")
                cfg = kv_get(storage, "digest") or {"on": False, "hour": 9}
                today = local_now().strftime("%Y-%m-%d")
                if cfg.get("on") and local_now().hour >= int(cfg.get("hour", 9)) and kv_get(storage, "digest_sent") != today:
                    kv_set(storage, "digest_sent", today)
                    await bot_send(await build_digest(storage, owner_id, get_username()), owner_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Планировщик: {type(exc).__name__}: {exc}")
        await asyncio.sleep(60)
