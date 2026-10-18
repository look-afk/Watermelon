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
from telethon.tl.types import Channel, Chat, User

from .config import settings
from .storage import Storage

# The page lives in its own file so it is easy to edit.
HTML = Path(__file__).with_name("panel.html").read_text(encoding="utf-8")


def json_error(status: int, message: str, **extra) -> web.HTTPException:
    cls = {
        400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
        404: web.HTTPNotFound, 429: web.HTTPTooManyRequests, 500: web.HTTPInternalServerError,
        502: web.HTTPBadGateway, 503: web.HTTPServiceUnavailable,
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
    allowed = _extra_allowed_ids() | {me.id}
    if user["id"] not in allowed:
        raise json_error(403, "Доступ только для владельца аккаунта. Чтобы разрешить другой ID, добавь его в PANEL_ALLOWED_USER_IDS.")
    return client, me


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


# ---------------------------------------------------------------- pages / api

async def index(request):
    return web.Response(text=HTML, content_type="text/html", headers={"Cache-Control": "no-store"})


async def chats(request):
    client, me = await authorize(request)
    try:
        dialogs = [dialog async for dialog in client.iter_dialogs(limit=300)]
    except FloodWaitError:
        raise
    except Exception as exc:
        print(f"Panel chats failed: {type(exc).__name__}: {exc}")
        raise json_error(502, f"Не удалось загрузить диалоги Telegram ({type(exc).__name__}). Проверь логи Render.") from exc

    storage = Storage(settings.database_path)
    counts = storage.get_chat_counts()
    items = []
    for dialog in dialogs:
        entity = dialog.entity
        if getattr(entity, "bot", False):
            continue
        if isinstance(entity, User):
            ctype, kind = "private", "Личный чат"
        elif isinstance(entity, Channel) and getattr(entity, "megagroup", False):
            ctype, kind = "group", "Группа"
        elif isinstance(entity, Channel):
            ctype, kind = "channel", "Канал"
        elif isinstance(entity, Chat):
            ctype, kind = "group", "Группа"
        else:
            ctype, kind = "group", "Чат"
        message = dialog.message
        last_text = ""
        if message is not None:
            text = getattr(message, "message", None)
            if text:
                last_text = " ".join(str(text).split())[:120]
            elif getattr(message, "media", None):
                last_text = "[медиа]"
        stored, last_stored = counts.get(dialog.id, (0, None))
        items.append({
            "id": dialog.id,
            "type": ctype,
            "kind": kind,
            "title": dialog.name or "Без названия",
            "username": getattr(entity, "username", None),
            "has_photo": has_photo(entity),
            "last_text": last_text,
            "last_date": dialog.date.isoformat() if dialog.date else None,
            "unread": dialog.unread_count or 0,
            "pinned": bool(dialog.pinned),
            "members": getattr(entity, "participants_count", None),
            "stored": stored,
            "last_stored": last_stored,
        })
    print(f"Panel chats: account_id={me.id}, dialogs={len(dialogs)}, visible_items={len(items)}")
    return web.json_response({
        "items": items,
        "totals": storage.get_totals(),
        "sync": get_sync_state(),
        "me": {"id": me.id, "name": full_name(me)},
    })


_photo_cache: "OrderedDict[int, bytes]" = OrderedDict()
_photo_sem = asyncio.Semaphore(3)
_PHOTO_CACHE_MAX = 500


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
                data = await client.download_profile_photo(entity, file=bytes, download_big=False) or b""
            except FloodWaitError:
                raise
            except Exception:
                data = b""
        _photo_cache[peer_id] = data
        if len(_photo_cache) > _PHOTO_CACHE_MAX:
            _photo_cache.popitem(last=False)
    if not data:
        raise web.HTTPNotFound(text="no photo")
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def members(request):
    client, me = await authorize(request)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID чата")

    storage = Storage(settings.database_path)
    db_users = storage.get_chat_senders(chat_id)
    summary = storage.get_chat_summary(chat_id)

    participants = {}
    note = None
    try:
        entity = await resolve_entity(client, chat_id)
        async for participant in client.iter_participants(entity, limit=500):
            if getattr(participant, "bot", False):
                continue
            participants[participant.id] = participant
    except FloodWaitError:
        raise
    except ValueError:
        raise json_error(400, "Чат не найден среди диалогов этого аккаунта.")
    except Exception as exc:
        # Broadcast channels / hidden member lists: fall back to people seen in stored messages.
        print(f"Panel members {chat_id}: {type(exc).__name__}: {exc}")
        note = "Telegram не отдаёт полный список участников — показаны авторы сохранённых сообщений."

    items = []
    for uid in set(participants) | set(db_users):
        participant = participants.get(uid)
        stats = db_users.get(uid) or {}
        name = (full_name(participant) if participant else "") or stats.get("name") or f"ID {uid}"
        username = (getattr(participant, "username", None) if participant else None) or stats.get("username")
        count = stats.get("message_count", 0)
        items.append({
            "id": uid,
            "title": name,
            "username": username,
            "has_photo": has_photo(participant) if participant else False,
            "message_count": count,
            "share": round(count / summary["messages"], 4) if summary["messages"] else 0,
            "first_seen": stats.get("first_seen"),
            "last_seen": stats.get("last_seen"),
            "in_chat": participant is not None,
            "is_me": uid == me.id,
        })
    items.sort(key=lambda x: (-x["message_count"], x["title"].lower()))
    return web.json_response({"items": items, "summary": summary, "note": note, "total_loaded": len(participants)})


async def messages(request):
    await authorize(request)
    try:
        chat_id = int(request.match_info["chat_id"])
        user_id = request.query.get("user_id")
        user_id = int(user_id) if user_id else None
        limit = max(1, min(int(request.query.get("limit", "40")), 100))
        offset = max(0, int(request.query.get("offset", "0")))
    except ValueError:
        raise json_error(400, "Некорректные параметры")
    query = (request.query.get("q") or "").strip()[:100] or None
    rows = Storage(settings.database_path).get_chat_messages(chat_id, user_id, limit + 1, offset, query)
    return web.json_response({"items": rows[:limit], "has_more": len(rows) > limit, "offset": offset + min(len(rows), limit)})


async def health(request):
    return web.json_response({"status": "ok", "service": "watermelon"})


def create_app():
    app = web.Application(middlewares=[json_errors])
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/panel", index)
    app.router.add_get("/api/chats", chats)
    app.router.add_get("/api/photo/{peer_id}", photo)
    app.router.add_get("/api/chats/{chat_id}/members", members)
    app.router.add_get("/api/chats/{chat_id}/messages", messages)
    return app
