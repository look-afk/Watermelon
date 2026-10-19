import asyncio
import hashlib
import hmac
import json
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from telethon.errors import FloodWaitError
from telethon.tl.types import Channel, Chat

from .backup import request_backup
from .config import settings
from .profiles import INFLIGHT, PROGRESS, person_stats, run_analysis
from .storage import Storage

HTML = Path(__file__).with_name("panel.html").read_text(encoding="utf-8")
_storage = None
_tasks: set = set()


_cache: dict = {}


async def cached(key: str, ttl: float, fn, *args):
    """Run a heavy DB read in a thread and reuse the result for a few seconds (the panel polls while syncing)."""
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = await asyncio.to_thread(fn, *args)
    _cache[key] = (now, value)
    return value


def storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage(settings.database_path)
    return _storage


def json_error(status: int, message: str, **extra) -> web.HTTPException:
    cls = {
        400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
        404: web.HTTPNotFound, 409: web.HTTPConflict, 429: web.HTTPTooManyRequests,
        500: web.HTTPInternalServerError, 502: web.HTTPBadGateway, 503: web.HTTPServiceUnavailable,
    }.get(status, web.HTTPInternalServerError)
    return cls(text=json.dumps({"error": message, **extra}, ensure_ascii=False), content_type="application/json")


@web.middleware
async def json_errors(request, handler):
    try:
        return await handler(request)
    except web.HTTPException as exc:
        if request.path.startswith("/api/") and exc.content_type != "application/json":
            return web.json_response({"error": exc.reason or "Ошибка"}, status=exc.status)
        raise
    except asyncio.CancelledError:
        raise
    except FloodWaitError as exc:
        return web.json_response({"error": f"Telegram просит подождать {exc.seconds} сек. Попробуй позже."}, status=429)
    except Exception as exc:
        print(f"Panel {request.path} failed: {type(exc).__name__}: {exc}")
        return web.json_response({"error": f"Внутренняя ошибка ({type(exc).__name__}). Проверь логи Render."}, status=500)


def validate_init_data(init_data: str) -> dict:
    if not init_data or not settings.bot_token:
        raise json_error(401, "Откройте панель через команду /panel в Telegram")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise json_error(401, "Нет подписи Telegram Web App")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise json_error(401, "Не удалось проверить Telegram Web App")
    try:
        auth_date = int(values.get("auth_date", "0"))
    except ValueError:
        auth_date = 0
    if time.time() - auth_date > 86400:
        raise json_error(401, "Сессия панели устарела. Закройте и откройте /panel снова.")
    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError:
        user = {}
    if not user.get("id"):
        raise json_error(401, "Не удалось определить пользователя")
    return user


def require_auth(request):
    return validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))


_me = None


async def get_me_cached(client):
    global _me
    if _me is None:
        _me = await client.get_me()
    return _me


def _extra_allowed_ids() -> set[int]:
    ids = set()
    for part in (settings.panel_allowed_user_ids or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.add(int(part))
    return ids


async def authorize(request):
    from .userbot import get_client
    user = require_auth(request)
    client = get_client()
    if not client or not client.is_connected():
        raise json_error(503, "Личный аккаунт Telegram ещё подключается")
    me = await get_me_cached(client)
    if user["id"] not in (_extra_allowed_ids() | {me.id}):
        raise json_error(403, "Доступ только для владельца аккаунта. Чтобы разрешить другой ID, добавь его в PANEL_ALLOWED_USER_IDS.")
    return client, me


async def read_json(request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise json_error(400, "Ожидался JSON")
    return data if isinstance(data, dict) else {}


async def resolve_entity(client, chat_id: int):
    try:
        return await client.get_entity(chat_id)
    except ValueError:
        async for _ in client.iter_dialogs(limit=500):
            pass
        return await client.get_entity(chat_id)


def has_photo(entity) -> bool:
    photo = getattr(entity, "photo", None)
    return photo is not None and not type(photo).__name__.endswith("Empty")


def full_name(entity) -> str:
    first = getattr(entity, "first_name", None) or ""
    last = getattr(entity, "last_name", None) or ""
    return (first + " " + last).strip() or getattr(entity, "title", None) or ""


def get_sync_state() -> dict:
    from .userbot import SYNC_STATE
    return dict(SYNC_STATE)


def wake_sync():
    from .userbot import SYNC_WAKE
    SYNC_WAKE.set()


def headline_fields(h: dict | None) -> dict:
    h = h or {}
    return {"summary": h.get("summary"), "attitude": h.get("attitude"), "hostility": h.get("hostility"),
            "usefulness": h.get("usefulness"), "relation": h.get("relation"), "analyzed_at": h.get("updated_at")}


# ---------------------------------------------------------------- pages / api

async def index(request):
    return web.Response(text=HTML, content_type="text/html", headers={"Cache-Control": "no-store"})


async def chats(request):
    """Tab «Чаты»: groups only (no contacts, no channels)."""
    client, me = await authorize(request)
    try:
        dialogs = [d async for d in client.iter_dialogs(limit=300)]
    except FloodWaitError:
        raise
    except Exception as exc:
        print(f"Panel chats failed: {type(exc).__name__}: {exc}")
        raise json_error(502, f"Не удалось загрузить диалоги Telegram ({type(exc).__name__}). Проверь логи Render.") from exc

    st = storage()
    counts = await cached("counts", 8, st.get_chat_counts)
    totals = await cached("totals", 8, st.get_totals)
    excluded = st.get_excluded_chats()
    items = []
    for d in dialogs:
        entity = d.entity
        is_group = (isinstance(entity, Channel) and getattr(entity, "megagroup", False)) or isinstance(entity, Chat)
        if not is_group:
            continue
        stored, last_stored = counts.get(d.id, (0, None))
        items.append({
            "id": d.id,
            "title": d.name or "Без названия",
            "username": getattr(entity, "username", None),
            "has_photo": has_photo(entity),
            "members": getattr(entity, "participants_count", None),
            "last_date": d.date.isoformat() if d.date else None,
            "stored": stored,
            "last_stored": last_stored,
            "excluded": d.id in excluded,
        })
    return web.json_response({
        "items": items,
        "totals": totals,
        "sync": get_sync_state(),
        "me": {"id": me.id, "name": full_name(me)},
        "llm": bool(settings.llm_api_key and settings.profile_auto),
    })


_photo_cache: "OrderedDict[int, bytes]" = OrderedDict()
_photo_sem = asyncio.Semaphore(3)


async def photo(request):
    client, _ = await authorize(request)
    try:
        peer_id = int(request.match_info["peer_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID")
    data = _photo_cache.get(peer_id)
    if data is None:
        async with _photo_sem:
            try:
                entity = await client.get_entity(peer_id)
            except Exception:
                raise web.HTTPNotFound(text="unknown peer")  # not cached: may resolve later
            try:
                data = await client.download_profile_photo(entity, file=bytes, download_big=False) or b""
            except FloodWaitError:
                raise
            except Exception:
                data = b""
        _photo_cache[peer_id] = data
        if len(_photo_cache) > 600:
            _photo_cache.popitem(last=False)
    if not data:
        raise web.HTTPNotFound(text="no photo")
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def chat_members(request):
    client, me = await authorize(request)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID чата")
    st = storage()
    if st.is_chat_excluded(chat_id):
        return web.json_response({"items": [], "excluded": True, "summary": None,
                                  "note": "Чат исключён из анализа: сообщения не собираются, участники не анализируются."})

    db_users, summary, headlines = await asyncio.gather(
        asyncio.to_thread(st.get_chat_senders, chat_id),
        asyncio.to_thread(st.get_chat_summary, chat_id),
        asyncio.to_thread(st.get_headlines),
    )
    rules_ignored = st.rules()[1]
    participants, note = {}, None
    try:
        entity = await resolve_entity(client, chat_id)
        async for p in client.iter_participants(entity, limit=500):
            if getattr(p, "bot", False):
                continue
            participants[p.id] = p
    except FloodWaitError:
        raise
    except ValueError:
        raise json_error(400, "Чат не найден среди диалогов этого аккаунта.")
    except Exception as exc:
        print(f"Panel members {chat_id}: {type(exc).__name__}: {exc}")
        note = "Telegram не отдаёт полный список участников — показаны авторы сохранённых сообщений."

    items = []
    for uid in set(participants) | set(db_users):
        if uid in rules_ignored:
            continue
        p = participants.get(uid)
        s = db_users.get(uid) or {}
        count = s.get("message_count", 0)
        items.append({
            "id": uid,
            "title": (full_name(p) if p else "") or s.get("name") or f"ID {uid}",
            "username": (getattr(p, "username", None) if p else None) or s.get("username"),
            "has_photo": has_photo(p) if p else True,
            "message_count": count,
            "share": round(count / summary["messages"], 4) if summary["messages"] else 0,
            "last_seen": s.get("last_seen"),
            "in_chat": p is not None,
            "is_me": uid == me.id,
            **headline_fields(headlines.get(uid)),
        })
    items.sort(key=lambda x: (-x["message_count"], x["title"].lower()))
    return web.json_response({"items": items, "summary": summary, "note": note, "excluded": False})


async def chat_rule(request):
    await authorize(request)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID чата")
    body = await read_json(request)
    if "excluded" not in body:
        raise json_error(400, "Нужно поле excluded")
    purged = await asyncio.to_thread(storage().set_chat_excluded, chat_id, bool(body["excluded"]))
    wake_sync()
    request_backup()
    _cache.clear()
    return web.json_response({"excluded": bool(body["excluded"]), "purged": purged})


async def people(request):
    """Tab «Люди»: everybody, without chat grouping."""
    client, me = await authorize(request)
    st = storage()
    rows = await cached("people", 8, st.get_people_list)
    headlines = await cached("headlines", 8, st.get_headlines)
    totals = await cached("totals", 8, st.get_totals)
    items = []
    for r in rows:
        if r["id"] == me.id:
            continue
        items.append({
            "id": r["id"], "title": r["name"] or (f"@{r['username']}" if r["username"] else f"ID {r['id']}"),
            "username": r["username"], "is_contact": r["is_contact"], "messages": r["messages"], "chats": r["chats"],
            "last_seen": r["last_seen"], "ignored": r["ignored"], "tracked": r["tracked"], "has_photo": True,
            **headline_fields(headlines.get(r["id"])),
        })
    items.sort(key=lambda x: (-x["messages"], x["title"].lower()))
    return web.json_response({"items": items[:2000], "totals": totals, "sync": get_sync_state(),
                              "llm": bool(settings.llm_api_key and settings.profile_auto)})


async def person(request):
    client, me = await authorize(request)
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID")
    st = storage()
    info = await asyncio.to_thread(st.get_person, uid)
    if not info:
        raise json_error(404, "Человек не найден в базе")
    stats, chats_, profile, connections, delays = await asyncio.gather(
        asyncio.to_thread(person_stats, st, uid),
        asyncio.to_thread(st.get_person_chats, uid),
        asyncio.to_thread(st.get_profile, uid),
        asyncio.to_thread(st.get_connections, uid, me.id, getattr(me, "username", None)),
        asyncio.to_thread(st.get_reply_delays, uid, me.id),
    )
    total = stats.get("total", 0)
    analysis = None
    if profile and profile.get("data"):
        analysis = {"data": profile["data"], "updated_at": profile["updated_at"],
                    "msg_count": profile["msg_count"], "new_since": max(0, total - profile["msg_count"])}
    return web.json_response({
        "person": {"id": uid, "title": info["name"] or (f"@{info['username']}" if info["username"] else f"ID {uid}"),
                   "username": info["username"], "is_contact": info["is_contact"], "is_me": uid == me.id, "has_photo": True},
        "rules": st.get_person_rule(uid),
        "stats": stats,
        "chats": chats_,
        "connections": connections,
        "reply_delay": delays,
        "analysis": analysis,
        "analysis_error": (profile or {}).get("error"),
        "analyzing": uid in INFLIGHT or uid in PROGRESS,
        "progress": PROGRESS.get(uid),
        "llm": bool(settings.llm_api_key),
        "min_messages": settings.profile_min_messages,
    })


async def person_activity(request):
    await authorize(request)
    try:
        uid = int(request.match_info["user_id"])
        limit = max(1, min(int(request.query.get("limit", "20")), 50))
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        raise json_error(400, "Некорректные параметры")
    items, more = await asyncio.to_thread(storage().get_person_activity, uid, limit, offset)
    return web.json_response({"items": items, "has_more": more, "offset": offset + len(items)})


async def person_analyze(request):
    """Starts (re)analysis in the background; the panel polls GET /api/people/{id} until it finishes."""
    client, me = await authorize(request)
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID")
    if not settings.llm_api_key:
        raise json_error(400, "Не задан LLM_API_KEY")
    if uid in INFLIGHT:
        return web.json_response({"started": False, "analyzing": True})

    async def job():
        try:
            await run_analysis(storage(), uid, me.id, full_name(me) or "Владелец")
        except Exception as exc:
            print(f"Ручной анализ {uid} не удался: {exc}")

    task = asyncio.create_task(job())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    await asyncio.sleep(0)  # let the job mark the person as in-flight
    return web.json_response({"started": True, "analyzing": True})


async def person_rule(request):
    await authorize(request)
    try:
        uid = int(request.match_info["user_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID")
    body = await read_json(request)
    ignored = bool(body["ignored"]) if "ignored" in body else None
    tracked = bool(body["tracked"]) if "tracked" in body else None
    purged = await asyncio.to_thread(storage().set_person_rule, uid, ignored, tracked)
    wake_sync()
    request_backup()
    _cache.clear()
    return web.json_response({"rules": storage().get_person_rule(uid), "purged": purged})


async def health(request):
    """Public, unauthenticated, instant. Built for uptime monitors (UptimeRobot, Render health checks):
    always answers 200 while the process is alive, on GET and HEAD. The `userbot` field lets a
    Keyword monitor ("connected") also alert when the Telegram account is disconnected."""
    from .userbot import get_client
    client = get_client()
    connected = bool(client and client.is_connected())
    return web.json_response(
        {"status": "ok", "service": "watermelon", "userbot": "connected" if connected else "connecting"},
        headers={"Cache-Control": "no-store"},
    )


def create_app():
    app = web.Application(middlewares=[json_errors])
    app.router.add_route("*", "/health", health)  # GET and HEAD (UptimeRobot may use either)
    app.router.add_get("/", index)
    app.router.add_get("/panel", index)
    app.router.add_get("/api/chats", chats)
    app.router.add_get("/api/photo/{peer_id}", photo)
    app.router.add_get("/api/chats/{chat_id}/members", chat_members)
    app.router.add_post("/api/chats/{chat_id}/rule", chat_rule)
    app.router.add_get("/api/people", people)
    app.router.add_get("/api/people/{user_id}", person)
    app.router.add_get("/api/people/{user_id}/activity", person_activity)
    app.router.add_post("/api/people/{user_id}/analyze", person_analyze)
    app.router.add_post("/api/people/{user_id}/rule", person_rule)
    return app
