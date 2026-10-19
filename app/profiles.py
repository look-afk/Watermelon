"""Statistics, conversation context and the background profile-analysis worker."""
import asyncio
import bisect
import hashlib
import json
import re
import time
from collections import OrderedDict, defaultdict
from datetime import datetime, timedelta, timezone

from .analyzer import analyze_profile, extract_notes, rollup_notes
from .backup import request_backup
from .config import settings
from .storage import Storage

_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_LINK = re.compile(r"https?://|t\.me/|www\.", re.I)
INFLIGHT: set[int] = set()
# Live progress of deep reading, shown in the panel: {user_id: {"done": n, "total": n, "stage": "reading|synthesis"}}
PROGRESS: dict[int, dict] = {}

CHUNK_CHARS = 10000      # one fragment = up to this many characters of the person's text ...
CHUNK_MSGS = 250         # ... or this many messages
MIN_GROUP_CHARS = 2000   # quiet months are merged into one fragment group
MAX_NOTES_CHARS = 60000  # budget for notes handed to the final synthesis
ROLLUP_THRESHOLD = 40    # above this many fragments, old years are rolled up


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
        lines.append(f"{(created or '')[:16].replace('T', ' ')} [{label}] {who}: {' '.join((text or '').split())[:400]}")
    # keep the most recent part when over budget
    out, size = [], 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > max_chars:
            break
        out.append(line)
    out.reverse()
    return "\n".join(out), kept_mine


def build_buckets(rows: list[tuple]) -> list[dict]:
    """Split the person's WHOLE history (oldest first) into fragments to be read one by one.
    Quiet months are merged, busy months are split by size. Each fragment has a fingerprint so that
    an unchanged fragment is never read twice."""
    months: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        months.setdefault((r[2] or "")[:7], []).append(r)
    groups, cur, cur_months, cur_chars = [], [], [], 0
    for m, items in months.items():
        cur.extend(items)
        cur_months.append(m)
        cur_chars += sum(len(r[3] or "") for r in items)
        if cur_chars >= MIN_GROUP_CHARS or len(cur_months) >= 6:
            groups.append((cur_months, cur))
            cur, cur_months, cur_chars = [], [], 0
    if cur:
        groups.append((cur_months, cur))
    buckets = []
    for months_in, items in groups:
        part, part_chars, idx = [], 0, 0

        def close():
            nonlocal part, part_chars, idx
            if not part:
                return
            chars = sum(len(r[3] or "") for r in part)
            buckets.append({
                "key": f"{months_in[0]}+{len(months_in)}#{idx}",
                "rows": part,
                "period": f"{part[0][2][:10]}..{part[-1][2][:10]}",
                "fp": f"{len(part)}:{chars}:{part[0][2]}:{part[-1][2]}",
            })
            part, part_chars, idx = [], 0, idx + 1

        for r in items:
            part.append(r)
            part_chars += len(r[3] or "")
            if part_chars >= CHUNK_CHARS or len(part) >= CHUNK_MSGS:
                close()
        close()
    return buckets


def _titles(storage: Storage) -> dict:
    return {cid: (m["title"] or f"Чат {cid}", m["type"] or "group") for cid, m in storage.get_chat_meta().items()}


def _fragment_transcript(storage: Storage, bucket: dict, me_id: int, titles: dict) -> str:
    rows = bucket["rows"]
    chats = sorted({r[0] for r in rows})
    start = (_dt(rows[0][2]) - timedelta(hours=2)).isoformat()
    mine = storage.get_my_messages_range(me_id, chats, start, rows[-1][2])
    text, _ = build_transcript({"person": rows, "mine": mine, "titles": titles}, me_id, max_chars=26000)
    return text


def prepare_notes(storage: Storage, user_id: int, name: str, ordered: list[dict]) -> list[dict]:
    """Notes for the final synthesis. A long history is compressed with cached yearly roll-ups so
    that EVERYTHING is still represented, just at lower resolution for old years."""
    def size(items):
        return sum(len(json.dumps(i["notes"], ensure_ascii=False)) for i in items)

    plain = [{"period": o["period"], "notes": o["data"]} for o in ordered]
    if len(ordered) <= ROLLUP_THRESHOLD and size(plain) <= MAX_NOTES_CHARS:
        return plain
    by_year = defaultdict(list)
    for o in ordered:
        by_year[o["period"][:4]].append(o)
    cached = storage.get_notes(user_id, 2)
    this_year = str(datetime.now(timezone.utc).year)
    out = []
    for year in sorted(by_year):
        items = by_year[year]
        if year == this_year or len(items) < 8:
            out.extend({"period": o["period"], "notes": o["data"]} for o in items)
            continue
        fp = hashlib.sha1("|".join(f"{o['key']}:{o['fp']}" for o in items).encode()).hexdigest()[:16]
        note = cached.get(year)
        if note and note["fp"] == fp:
            data = note["data"]
        else:
            data = rollup_notes(name, year, [{"period": o["period"], **o["data"]} for o in items])
            storage.save_note(user_id, 2, year, fp, sum(1 for _ in items), year, data)
            time.sleep(settings.profile_call_pause)
        out.append({"period": year, "notes": data})
    while len(out) > 1 and size(out) > MAX_NOTES_CHARS:
        out.pop(0)  # extremely long history: drop the oldest roll-up first
    storage.delete_notes_except(user_id, 2, {o["period"] for o in out if len(o["period"]) == 4})
    return out


async def run_analysis(storage: Storage, user_id: int, me_id: int, owner_name: str,
                       chunk_budget: int | None = None) -> dict:
    """Deep reading of one person: every fragment of the whole stored history is read once and kept as
    notes (resumable), then the final profile is synthesized from ALL notes plus the freshest messages.
    chunk_budget limits LLM calls per run; the rest continues on the next cycle."""
    if user_id in INFLIGHT:
        raise RuntimeError("Анализ этого человека уже выполняется")
    INFLIGHT.add(user_id)
    processed = 0
    try:
        rows = await asyncio.to_thread(storage.get_person_all, user_id)
        if len(rows) < 3:
            raise RuntimeError("Слишком мало сообщений для анализа (нужно хотя бы 3)")
        person = await asyncio.to_thread(storage.get_person, user_id) or {}
        name = person.get("name") or f"ID {user_id}"
        titles = await asyncio.to_thread(_titles, storage)
        buckets = build_buckets(rows)
        existing = await asyncio.to_thread(storage.get_notes, user_id, 1)
        pending = [b for b in buckets if existing.get(b["key"], {}).get("fp") != b["fp"]]
        progress = PROGRESS[user_id] = {"done": len(buckets) - len(pending), "total": len(buckets), "stage": "reading"}
        pending.sort(key=lambda b: b["rows"][-1][2], reverse=True)  # freshest knowledge first

        failures, last_error = 0, None
        for b in pending:
            if chunk_budget is not None and processed >= chunk_budget:
                break
            try:
                transcript = await asyncio.to_thread(_fragment_transcript, storage, b, me_id, titles)
                notes = await asyncio.to_thread(extract_notes, name, owner_name, b["period"], transcript)
                await asyncio.to_thread(storage.save_note, user_id, 1, b["key"], b["fp"], len(b["rows"]), b["period"], notes)
                processed += 1
                progress["done"] += 1
                await asyncio.sleep(settings.profile_call_pause)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures, last_error = failures + 1, exc
                if failures >= 3:
                    raise RuntimeError(f"Не удалось прочитать фрагменты: {last_error}")
        remaining = progress["total"] - progress["done"]
        if remaining > 0:
            return {"status": "partial", "pending": remaining, "processed": processed}

        progress["stage"] = "synthesis"
        current = await asyncio.to_thread(storage.get_notes, user_id, 1)
        keys = {b["key"] for b in buckets}
        await asyncio.to_thread(storage.delete_notes_except, user_id, 1, keys)
        ordered = [{"key": b["key"], "fp": b["fp"], "period": current[b["key"]]["period"], "data": current[b["key"]]["data"]}
                   for b in buckets if b["key"] in current]
        notes_payload = await asyncio.to_thread(prepare_notes, storage, user_id, name, ordered)
        recent = await asyncio.to_thread(storage.get_context_rows, user_id, me_id, 120)
        transcript, _ = build_transcript(recent, me_id, 12000)
        stats = await asyncio.to_thread(person_stats, storage, user_id)
        previous = await asyncio.to_thread(storage.get_profile, user_id)
        prev = None
        if previous and previous.get("data"):
            d = previous["data"]
            prev = {k: d.get(k) for k in ("summary", "attitude_to_me", "usefulness_to_me", "relationship_guess", "traits")}
        compact = {k: v for k, v in stats.items() if k not in ("hours_utc", "weekdays_utc", "weekly")}
        result = await asyncio.to_thread(analyze_profile, owner_name, name, compact, transcript, prev, notes_payload)
        result["coverage"] = {
            "messages": stats.get("total", len(rows)), "fragments": len(buckets),
            "first": rows[0][2], "last": rows[-1][2], "mode": "deep",
        }
        await asyncio.to_thread(storage.save_profile, user_id, result, stats.get("total", len(rows)))
        processed += 1
        PROGRESS.pop(user_id, None)
        return {"status": "done", "pending": 0, "processed": processed}
    except Exception as exc:
        PROGRESS.pop(user_id, None)
        await asyncio.to_thread(storage.save_profile_error, user_id, f"{type(exc).__name__}: {exc}")
        raise
    finally:
        INFLIGHT.discard(user_id)  # PROGRESS stays after a partial run so the panel can show "read X of Y"


async def profile_loop(storage: Storage, me_id: int, owner_name: str, gate=None):
    """Keeps profiles fresh and DEEP. New people get a profile once they have enough messages; existing
    profiles are rebuilt after enough new messages. Work is resumable and rate-limited:
    PROFILE_CHUNKS_PER_CYCLE LLM calls per cycle. `gate()` returns True when the history import is complete."""
    await asyncio.sleep(45)
    started = time.time()
    while True:
        busy = False
        try:
            if gate and not gate() and time.time() - started < settings.profile_wait_backfill_hours * 3600:
                await asyncio.sleep(120)  # wait for the full history to be imported, then read it once
                continue
            ids = await asyncio.to_thread(
                storage.people_needing_analysis,
                settings.profile_min_messages, settings.profile_refresh_messages,
                settings.profile_max_per_cycle, me_id,
            )
            budget = settings.profile_chunks_per_cycle
            for uid in ids:
                if budget <= 0:
                    busy = True
                    break
                try:
                    res = await run_analysis(storage, uid, me_id, owner_name, chunk_budget=budget)
                    budget -= max(1, res["processed"])
                    busy = busy or res["status"] == "partial"
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"Анализ профиля {uid} не удался: {exc}")
                await asyncio.sleep(3)
            if ids:
                print(f"Глубокий анализ: обработано людей {len(ids)}, осталось работы: {'да' if busy else 'нет'}")
                request_backup()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Ошибка цикла анализа профилей: {type(exc).__name__}: {exc}")
        await asyncio.sleep(60 if busy else max(1, settings.profile_interval_min) * 60)