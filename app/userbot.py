import asyncio
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
SYNC_STATE = {"running": False, "done": 0, "total": 0, "new_messages": 0}
# Set by the panel after a rule change so the next sync cycle starts right away.
SYNC_WAKE = asyncio.Event()

# Own userbot commands must never be stored as chat messages.
CMD_RE = re.compile(r"^\.(analyze|analyzeall|helpwatermelon)\b")


def get_client():
    return _client


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


async def sync_chunk(client, storage: Storage, chat_id: int, me, limit: int, from_user: int | None = None) -> int:
    """Silently import text history. First run = backfill up to `limit`; later runs only newer messages.
    The marker is kept per (chat, user) so a tracked person's sync in an excluded chat is independent."""
    key = from_user or 0
    marker = await asyncio.to_thread(storage.get_sync_marker, chat_id, key)
    kwargs = {"limit": limit}
    if marker:
        kwargs["min_id"] = marker
    if from_user:
        kwargs["from_user"] = from_user
    rows = []
    max_id = marker or 0
    async for message in client.iter_messages(chat_id, **kwargs):
        max_id = max(max_id, message.id)
        text = (message.message or "").strip()
        sender_id = message.sender_id
        if not text or not sender_id or CMD_RE.match(text):
            continue
        if not storage.should_store(chat_id, sender_id):
            continue
        sender = me if message.out else message.sender
        if sender is not None and getattr(sender, "bot", False):
            continue
        rows.append((
            chat_id, sender_id, getattr(sender, "username", None), display_name(sender), text, message.id,
            message.date.isoformat() if message.date else None, reply_id(message),
        ))
    saved = await asyncio.to_thread(storage.save_messages_bulk, rows)
    await asyncio.to_thread(storage.set_sync_marker, chat_id, max_id, key)
    return saved


async def sync_dialog(client, storage: Storage, dialog, me, limit: int) -> int:
    chat_id = dialog.id
    if storage.is_chat_excluded(chat_id):
        # The chat is not analyzed, except people explicitly marked as tracked.
        saved = 0
        for uid in storage.tracked_users()[:10]:
            try:
                saved += await sync_chunk(client, storage, chat_id, me, limit, from_user=uid)
            except FloodWaitError:
                raise
            except Exception:
                pass  # the person is probably not in this chat
        return saved
    return await sync_chunk(client, storage, chat_id, me, limit)


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
    while client.is_connected():
        try:
            await refresh_contacts(client, storage)
            dialogs = [d async for d in client.iter_dialogs(limit=settings.auto_sync_dialogs)]
            for d in dialogs:  # chat/person directory used by the panel
                entity = d.entity
                await asyncio.to_thread(
                    storage.upsert_chat_meta, d.id, d.name or "Без названия", chat_type(entity), getattr(entity, "username", None))
                if isinstance(entity, User) and not entity.bot and not entity.deleted and not entity.is_self:
                    await asyncio.to_thread(storage.upsert_people, [(entity.id, display_name(entity), entity.username)])
            targets = [d for d in dialogs if is_syncable(d)]
            SYNC_STATE.update(running=True, done=0, total=len(targets), new_messages=0)
            for dialog in targets:
                try:
                    saved = await sync_dialog(client, storage, dialog, me, settings.auto_sync_messages_per_chat)
                    SYNC_STATE["new_messages"] += saved
                except FloodWaitError as exc:
                    await asyncio.sleep(min(exc.seconds + 1, 900))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    print(f"Автосинхронизация чата {dialog.id} не удалась: {type(exc).__name__}: {exc}")
                SYNC_STATE["done"] += 1
                await asyncio.sleep(0.4)
            print(f"Автосинхронизация завершена: чатов {len(targets)}, новых сообщений {SYNC_STATE['new_messages']}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Ошибка автосинхронизации: {type(exc).__name__}: {exc}")
        finally:
            SYNC_STATE["running"] = False
        try:
            await asyncio.wait_for(SYNC_WAKE.wait(), timeout=max(1, settings.auto_sync_interval_min) * 60)
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
            storage.save_message(
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

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.analyze(?:\s+(\d+))?$") )
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
                    "Недостаточно сохранённых сообщений этого человека. Они подтяги��аются автоматически — смотри панель (/panel).")
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

    # No persistent disk on Render free: bring the database back from the latest Saved-Messages snapshot
    # BEFORE the panel is unlocked and before any sync runs.
    if settings.backup_enabled:
        await backup.restore_if_fresh(client, settings.database_path)

    _client = client
    me = await client.get_me()
    print(f"Watermelon запущен для аккаунта ID {me.id}. Автосинхронизация и анализ профилей включены.")
    tasks = [asyncio.create_task(auto_sync_loop(client, storage))]
    if settings.backup_enabled:
        tasks.append(asyncio.create_task(backup.backup_loop(client, settings.database_path)))
    if settings.profile_auto:
        tasks.append(asyncio.create_task(profile_loop(storage, me.id, display_name(me) or "Владелец")))
    try:
        await client.run_until_disconnected()
    finally:
        for task in tasks:
            task.cancel()
        _client = None


if __name__ == "__main__":
    asyncio.run(main())
