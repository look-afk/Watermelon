import asyncio
#"у"
import html
import json
import os
import re

from telethon import TelegramClient, events
from telethon.errors import AuthKeyDuplicatedError, FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import GetContactsRequest
from telethon.tl.types import Channel, Chat, User

from . import backup
from .analyzer import analyze_messages
from .config import settings
from .profiles import profile_loop
from .storage import Storage

_client = None

# Read by the panel to show a small sync indicator.
SYNC_STATE = {"running": False, "done": 0, "total": 0, "new_messages": 0, "backlog": 1, "cycles": 0, "current_chat": None}
# Set by the panel after a rule change so the next sync cycle starts right away.
SYNC_WAKE = asyncio.Event()

# Own userbot commands must never be stored as chat messages.
CMD_RE = re.compile(r"^\.(analyze|analyzeall|helpwatermelon)\b")


# Owner id is remembered as soon as the account is authorized, so the bot recognizes the owner even while
# the database is being restored or Telethon briefly reconnects (otherwise every command was "access denied").
OWNER = {"id": None}


def get_client():
    return _client


def get_owner_id():
    return OWNER["id"]


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "replace_me":
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def split_text(text: str, limit: int = 3500) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or ["{}"]


def display_name(sender) -> str | None:
    if sender is None:
        return None
    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    return (first + " " + last).strip() or getattr(sender, "title", None) or None


def chat_type(entity) -> str:
    if isinstance(entity, User):
        return "private"
    if isinstance(entity, Channel):
        return "group" if getattr(entity, "megagroup", False) else "channel"
    return "group"


def is_syncable(dialog) -> bool:
    """Private chats and groups. Broadcast channels have no useful per-user senders."""
    entity = dialog.entity
    if isinstance(entity, User):
        return not (entity.bot or entity.deleted)
    if isinstance(entity, Channel):
        return bool(getattr(entity, "megagroup", False))
    return isinstance(entity, Chat)


def reply_id(message) -> int | None:
    return getattr(getattr(message, "reply_to", None), "reply_to_msg_id", None)


def history_complete() -> bool:
    """True once at least one full cycle ran and no chat still has an unfinished history import."""
    return SYNC_STATE["cycles"] >= 1 and SYNC_STATE["backlog"] == 0


async def sync_chunk(client, storage: Storage, chat_id: int, me, from_user: int | None = None) -> tuple[int, bool]:
    """Silently import the text history of one chat, ALL of it.
      1. messages newer than the stored marker (cheap, every cycle);
      2. the backfill of older history, resumable: it walks from the newest message towards the oldest and
         remembers how far it got, so a restart or redeploy continues instead of starting over.
    A per-cycle cap (AUTO_SYNC_CHUNK) keeps one huge chat from starving the others.
    Returns (new messages saved, backfill still pending)."""
    key = from_user or 0
    state = await asyncio.to_thread(storage.get_sync_state, chat_id, key)
    saved_total = 0
    base = {"from_user": from_user} if from_user else {}

    def make_row(message):
        text = (message.message or "").strip()
        sender_id = message.sender_id
        if not text or not sender_id or CMD_RE.match(text):
            return None
        if not storage.should_store(chat_id, sender_id):
            return None
        sender = me if message.out else message.sender
        if sender is not None and getattr(sender, "bot", False):
            return None
        return (chat_id, sender_id, getattr(sender, "username", None), display_name(sender), text, message.id,
                message.date.isoformat() if message.date else None, reply_id(message))

    async def flush(rows):
        nonlocal saved_total
        if rows:
            saved_total += await asyncio.to_thread(storage.save_messages_bulk, rows)
            rows.clear()

    # 1) what is new since the last run
    if state and state["max_id"]:
        rows, newest = [], state["max_id"]
        async for message in client.iter_messages(chat_id, limit=None, min_id=state["max_id"], **base):
            newest = max(newest, message.id)
            row = make_row(message)
            if row:
                rows.append(row)
            if len(rows) >= 1000:
                await flush(rows)
        await flush(rows)
        await asyncio.to_thread(storage.set_sync_state, chat_id, key, max_id=newest)

    # 2) the rest of the history, newest -> oldest
    if state and state["bf_done"]:
        return saved_total, False
    first_pass_limit = settings.auto_sync_messages_per_chat or None  # 0 = unlimited
    capped_first_pass = bool(first_pass_limit and not state)
    cap = None if capped_first_pass else (settings.auto_sync_chunk or None)
    offset = (state or {}).get("bf_min_id") or 0
    rows, count, newest, oldest, finished = [], 0, 0, None, True
    kwargs = dict(base)
    if offset:
        kwargs["offset_id"] = offset
    if capped_first_pass:
        kwargs["limit"] = first_pass_limit
    else:
        kwargs["limit"] = None
    async for message in client.iter_messages(chat_id, **kwargs):
        count += 1
        newest = max(newest, message.id)
        oldest = message.id if oldest is None else min(oldest, message.id)
        row = make_row(message)
        if row:
            rows.append(row)
        if len(rows) >= 1000:
            await flush(rows)
            await asyncio.to_thread(storage.set_sync_state, chat_id, key,
                                    max_id=None if (state and state["max_id"]) else newest, bf_min_id=oldest)
        if cap and count >= cap:
            finished = False
            break
    await flush(rows)
    await asyncio.to_thread(
        storage.set_sync_state, chat_id, key,
        max_id=None if (state and state["max_id"]) else newest, bf_min_id=oldest, bf_done=finished)
    return saved_total, not finished


async def sync_dialog(client, storage: Storage, dialog, me) -> tuple[int, bool]:
    chat_id = dialog.id
    if storage.is_chat_excluded(chat_id):
        # The chat is not analyzed, except people explicitly marked as tracked.
        saved, pending = 0, False
        for uid in storage.tracked_users()[:10]:
            try:
                n, p = await sync_chunk(client, storage, chat_id, me, from_user=uid)
                saved, pending = saved + n, pending or p
            except FloodWaitError:
                raise
            except Exception:
                pass  # the person is probably not in this chat
        return saved, pending
    return await sync_chunk(client, storage, chat_id, me)


async def refresh_contacts(client, storage: Storage):
    try:
        result = await client(GetContactsRequest(hash=0))
        users = getattr(result, "users", None) or []
        rows = [(u.id, display_name(u), u.username) for u in users
                if isinstance(u, User) and not u.bot and not u.deleted]
        await asyncio.to_thread(storage.set_contacts, rows)
    except FloodWaitError:
        raise
    except Exception as exc:
        print(f"Не удалось получить контакты: {type(exc).__name__}: {exc}")


async def auto_sync_loop(client, storage: Storage):
    await asyncio.sleep(3)
    me = await client.get_me()
    while True:
        if not client.is_connected():
            await asyncio.sleep(5)  # Telethon reconnects by itself; keep waiting instead of quitting for good
            continue
        backlog = 0
        try:
            await refresh_contacts(client, storage)
            dialogs = [d async for d in client.iter_dialogs(limit=settings.auto_sync_dialogs or None)]
            for d in dialogs:  # chat/person directory used by the panel
                entity = d.entity
                await asyncio.to_thread(
                    storage.upsert_chat_meta, d.id, d.name or "Без названия", chat_type(entity), getattr(entity, "username", None))
                if isinstance(entity, User) and not entity.bot and not entity.deleted and not entity.is_self:
                    await asyncio.to_thread(storage.upsert_people, [(entity.id, display_name(entity), entity.username)])
            targets = [d for d in dialogs if is_syncable(d)]
            SYNC_STATE.update(running=True, done=0, total=len(targets), new_messages=0, current_chat="Подготовка…")
            failed = 0
            for dialog in targets:
                SYNC_STATE["current_chat"] = dialog.name or "Без названия"
                try:
                    saved, pending = await sync_dialog(client, storage, dialog, me)
                    SYNC_STATE["new_messages"] += saved
                    backlog += 1 if pending else 0
                except FloodWaitError as exc:
                    backlog += 1
                    await asyncio.sleep(min(exc.seconds + 1, 900))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed += 1
                    print(f"Автосинхронизация чата {dialog.id} не удалась: {type(exc).__name__}: {exc}")
                SYNC_STATE["done"] += 1
                SYNC_STATE["backlog"] = backlog
                await asyncio.sleep(0.3)
            SYNC_STATE["cycles"] += 1
            print(f"Автосинхронизация: чатов {len(targets)}, новых сообщений {SYNC_STATE['new_messages']}, "
                  f"чатов с недогруженной историей {backlog}, ошибок {failed}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            backlog = max(backlog, 1)
            print(f"Ошибка автосинхронизации: {type(exc).__name__}: {exc}")
        finally:
            SYNC_STATE["running"] = False
            SYNC_STATE["backlog"] = backlog
            SYNC_STATE["current_chat"] = None
        # keep going quickly while some history is still being loaded, otherwise wait for the normal interval
        wait = 20 if backlog else max(1, settings.auto_sync_interval_min) * 60
        try:
            await asyncio.wait_for(SYNC_WAKE.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        SYNC_WAKE.clear()


async def main():
    global _client
    api_id = int(required_env("TG_API_ID"))
    api_hash = required_env("TG_API_HASH")
    session = required_env("TG_SESSION")
    if not settings.llm_api_key:
        raise RuntimeError("Не задан LLM_API_KEY")
    if not settings.llm_base_url:
        raise RuntimeError("Не задан LLM_BASE_URL")

    storage = Storage(settings.database_path)
    client = TelegramClient(StringSession(session), api_id, api_hash)

    # Live collection: incoming AND your own messages, silently, honoring chat/person rules.
    @client.on(events.NewMessage())
    async def collect_message(event):
        try:
            message_text = (event.raw_text or "").strip()
            if not message_text or CMD_RE.match(message_text) or event.chat_id is None:
                return
            if event.is_channel and not event.is_group:
                return
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False):
                return
            sender_id = getattr(sender, "id", None)
            if sender_id is None or not storage.should_store(event.chat_id, sender_id):
                return
            await asyncio.to_thread(
                storage.save_message,
                chat_id=event.chat_id,
                user_id=sender_id,
                username=getattr(sender, "username", None),
                message_text=message_text,
                telegram_message_id=event.message.id,
                created_at=event.message.date.isoformat() if event.message.date else None,
                sender_name=display_name(sender),
                reply_to_msg_id=reply_id(event.message),
            )
        except Exception as exc:
            print(f"Не удалось сохранить сообщение: {type(exc).__name__}: {exc}")

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.analyze(?:\s+(\d+))?$"))
    async def analyze_current_chat(event):
        try:
            target_text = event.pattern_match.group(1)
            chat = await event.get_chat()
            if target_text:
                target_id = int(target_text)
            elif not isinstance(chat, User) or getattr(chat, "bot", False):
                await event.respond("Укажи ID пользователя: .analyze USER_ID")
                return
            else:
                target_id = chat.id

            await event.delete()
            rows = []
            async for message in client.iter_messages(
                event.chat_id, limit=settings.max_messages_per_analysis, from_user=target_id,
            ):
                text = (message.message or "").strip()
                if text and not CMD_RE.match(text):
                    rows.append(text)
            rows.reverse()
            if len(rows) < 2:
                await client.send_message(event.chat_id, "Недостаточно текстовых сообщений этого пользователя в доступной истории чата.")
                return
            await client.send_message(event.chat_id, f"Анализирую {len(rows)} сообщений…")
            result = await asyncio.to_thread(analyze_messages, rows)
            output = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            for chunk in split_text(output):
                await client.send_message(event.chat_id, f"<pre>{chunk}</pre>", parse_mode="html")
        except Exception as exc:
            print(f"Ошибка анализа текущего чата: {type(exc).__name__}: {exc}")
            await event.respond("Не удалось выполнить анализ. Проверь настройки Gemini и логи Render.")

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.analyzeall\s+(\d+)$"))
    async def analyze_all_chats(event):
        try:
            target_id = int(event.pattern_match.group(1))
            await event.delete()
            rows = storage.get_messages_all(target_id, settings.max_messages_per_analysis)
            if len(rows) < 2:
                await client.send_message(
                    event.chat_id,
                    "Недостаточно сохранённых сообщений этого человека. Они подтягиваются автоматически — смотри панель (/panel).")
                return
            await client.send_message(event.chat_id, f"Анализирую {len(rows)} сохранённых сообщений…")
            result = await asyncio.to_thread(analyze_messages, rows)
            output = html.escape(json.dumps(result, ensure_ascii=False, indent=2))
            for chunk in split_text(output):
                await client.send_message(event.chat_id, f"<pre>{chunk}</pre>", parse_mode="html")
        except Exception as exc:
            print(f"Ошибка глобального анализа: {type(exc).__name__}: {exc}")
            await client.send_message(event.chat_id, "Не удалось выполнить анализ. Проверь настройки Gemini и логи Render.")

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.helpwatermelon$"))
    async def help_command(event):
        await event.delete()
        await client.send_message(
            event.chat_id,
            "Watermelon — бот-аналитик\n"
            "Чаты читаются и анализируются автоматически. Профили людей, связи и статистика — в панели бота (/panel).\n"
            ".analyze USER_ID — быстрый анализ человека в текущем чате.\n"
            ".analyzeall USER_ID — быстрый анализ по всем сохранённым чатам.\n"
            ".helpwatermelon — эта справка."
        )

    # client.start() with an invalid StringSession asks for a phone number, which hangs on Render.
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("TG_SESSION недействителен или отозван. Создай новую строку: python -m app.create_session")
    except AuthKeyDuplicatedError as exc:
        raise RuntimeError(
            "Одна и та же TG_SESSION используется из двух мест одновременно (например, локально и на Render). "
            "Останови второй процесс и создай новую сессию: python -m app.create_session"
        ) from exc

    me = await client.get_me()
    OWNER["id"] = me.id

    # No persistent disk on Render free: bring the database back from the latest Saved-Messages snapshot
    # BEFORE the panel is unlocked and before any sync runs.
    if settings.backup_enabled:
        await backup.restore_if_fresh(client, settings.database_path)

    _client = client
    print(f"Watermelon запущен для аккаунта ID {me.id}. Автосинхронизация и анализ профилей включены.")
    tasks = [asyncio.create_task(auto_sync_loop(client, storage))]
    if settings.backup_enabled:
        tasks.append(asyncio.create_task(backup.backup_loop(client, settings.database_path)))
    if settings.profile_auto:
        tasks.append(asyncio.create_task(profile_loop(storage, me.id, display_name(me) or "Владелец", gate=history_complete)))
    try:
        await client.run_until_disconnected()
    finally:
        for task in tasks:
            task.cancel()
        _client = None


if __name__ == "__main__":
    asyncio.run(main())
