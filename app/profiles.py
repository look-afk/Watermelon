"""Statistics, conversation context and the background profile-analysis worker."""
import asyncio
import bisect
import re
from datetime import datetime, timezone

from .analyzer import analyze_profile
from .backup import request_backup
from .config import settings
from .storage import Storage

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_LINK = re.compile(r"https?://|t\.me/|www\.", re.I)
INFLIGHT: set[int] = set()


def _dt(value: str):
    try:
        d = datetime.fromisoformat(value)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def compute_stats(rows: list[tuple], total: int) -> dict:
    """rows: (created_at, text, chat_id, tg_id, reply_to), newest first (capped); total = real message count."""
    now = datetime.now(timezone.utc)
    parsed = [(_dt(r[0]), r[1] or "", r[4]) for r in rows]
    parsed = [p for p in parsed if p[0]]
    n = len(parsed)
    if not n:
        return {"total": total, "empty": True}
    dts = [p[0] for p in parsed]
    first, last = min(dts), max(dts)
    hours, weekdays, weekly = [0] * 24, [0] * 7, [0] * 12
    last30 = prev30 = 0
    for d in dts:
        hours[d.hour] += 1
        weekdays[d.weekday()] += 1
        age = (now - d).days
        if 0 <= age < 84:
            weekly[11 - age // 7] += 1
        if age < 30:
            last30 += 1
        elif age < 60:
            prev30 += 1
    texts = [p[1] for p in parsed]
    letters = lambda t: [c for c in t if c.isalpha()]
    caps = sum(1 for t in texts if len(letters(t)) >= 8 and sum(c.isupper() for c in letters(t)) / len(letters(t)) > 0.7)
    active_days = len({d.date() for d in dts})
    return {
        "total": total,
        "sampled": n,
        "first": first.isoformat(),
        "last": last.isoformat(),
        "active_days": active_days,
        "per_active_day": round(n / max(1, active_days), 1),
        "avg_len": round(sum(len(t) for t in texts) / n),
        "avg_words": round(sum(len(t.split()) for t in texts) / n, 1),
        "question_pct": round(100 * sum("?" in t for t in texts) / n),
        "exclaim_pct": round(100 * sum("!" in t for t in texts) / n),
        "caps_pct": round(100 * caps / n),
        "link_pct": round(100 * sum(bool(_LINK.search(t)) for t in texts) / n),
        "emoji_per_msg": round(sum(len(_EMOJI.findall(t)) for t in texts) / n, 2),
        "reply_pct": round(100 * sum(1 for p in parsed if p[2]) / n),
        "hours_utc": hours,
        "weekdays_utc": weekdays,
        "weekly": weekly,
        "last30": last30,
        "prev30": prev30,
    }


def person_stats(storage: Storage, user_id: int) -> dict:
    rows = storage.get_person_rows(user_id, 5000)
    total = sum(c["count"] for c in storage.get_person_chats(user_id))
    return compute_stats(rows, total)


def build_transcript(ctx: dict, me_id: int, max_chars: int = 14000) -> tuple[str, int]:
    """Person's messages plus the owner's messages that directly surround them."""
    person, mine, titles = ctx["person"], ctx["mine"], ctx["titles"]
    person_times: dict[int, list[float]] = {}
    replied_to = set()
    entries = []
    for chat, tg_id, created, text, reply_to in person:
        d = _dt(created)
        if d:
            person_times.setdefault(chat, []).append(d.timestamp())
        if reply_to:
            replied_to.add((chat, reply_to))
        entries.append((created, chat, "ОБЪЕКТ", text))
    for times in person_times.values():
        times.sort()
    kept_mine = 0
    for chat, tg_id, created, text in mine:
        d = _dt(created)
        if not d:
            continue
        times = person_times.get(chat, [])
        i = bisect.bisect_left(times, d.timestamp())
        near = i < len(times) and times[i] - d.timestamp() <= 600
        if near or (chat, tg_id) in replied_to:
            entries.append((created, chat, "ВЛАДЕЛЕЦ", text))
            kept_mine += 1
    entries.sort(key=lambda e: e[0])
    lines = []
    for created, chat, who, text in entries:
        title, ctype = titles.get(chat, (f"Чат {chat}", "group"))
        label = "личный чат" if ctype == "private" else title
        lines.append(f"[{label}] {who}: {' '.join((text or '').split())[:350]}")
    # keep the most recent part when over budget
    out, size = [], 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > max_chars:
            break
        out.append(line)
    out.reverse()
    return "\n".join(out), kept_mine


async def run_analysis(storage: Storage, user_id: int, me_id: int, owner_name: str) -> dict:
    """Build/refresh one person's profile. Safe to call from the worker and the panel."""
    if user_id in INFLIGHT:
        raise RuntimeError("Анализ этого человека уже выполняется")
    INFLIGHT.add(user_id)
    try:
        ctx = await asyncio.to_thread(storage.get_context_rows, user_id, me_id)
        if len(ctx["person"]) < 3:
            raise RuntimeError("Слишком мало сообщений для анализа (нужно хотя бы 3)")
        person = await asyncio.to_thread(storage.get_person, user_id) or {}
        stats = await asyncio.to_thread(person_stats, storage, user_id)
        previous = await asyncio.to_thread(storage.get_profile, user_id)
        transcript, _ = build_transcript(ctx, me_id)
        compact_stats = {k: v for k, v in stats.items() if k not in ("hours_utc", "weekdays_utc", "weekly")}
        result = await asyncio.to_thread(
            analyze_profile, owner_name, person.get("name") or f"ID {user_id}", compact_stats, transcript,
            previous["data"] if previous and previous.get("data") else None,
        )
        await asyncio.to_thread(storage.save_profile, user_id, result, stats.get("total", 0))
        return result
    except Exception as exc:
        await asyncio.to_thread(storage.save_profile_error, user_id, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        INFLIGHT.discard(user_id)


async def profile_loop(storage: Storage, me_id: int, owner_name: str):
    """Keeps profiles fresh: new people get a profile once they have enough messages, and
    existing profiles are updated after enough new messages."""
    await asyncio.sleep(45)
    while True:
        try:
            ids = await asyncio.to_thread(
                storage.people_needing_analysis,
                settings.profile_min_messages, settings.profile_refresh_messages,
                settings.profile_max_per_cycle, me_id,
            )
            for uid in ids:
                try:
                    await run_analysis(storage, uid, me_id, owner_name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"Анализ проф��ля {uid} не удался: {exc}")
                await asyncio.sleep(3)
            if ids:
                print(f"Профили обновлены: {len(ids)}")
                request_backup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Ошибка цикла анализа профилей: {type(exc).__name__}: {exc}")
        await asyncio.sleep(max(1, settings.profile_interval_min) * 60)
