import asyncio
import html
import json
import os

from telethon import TelegramClient, events
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import StringSession
from telethon.tl.types import User

from .analyzer import analyze_messages
from .config import settings
from .storage import Storage

_client = None


def get_client():
    return _client


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "replace_me":
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def split_text(text: str, limit: int = 3500) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or ["{}"]


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

    @client.on(events.NewMessage(incoming=True))
    async def collect_message(event):
        try:
            message_text = (event.raw_text or "").strip()
            sender = await event.get_sender()
            if not message_text or not sender or getattr(sender, "bot", False):
                return
            sender_id = getattr(sender, "id", None)
            if sender_id is None or event.chat_id is None:
                return
            storage.save_message(
                chat_id=event.chat_id,
                user_id=sender_id,
                username=getattr(sender, "username", None),
                message_text=message_text,
                telegram_message_id=event.message.id,
                created_at=event.message.date.isoformat() if event.message.date else None,
            )
        except Exception as exc:
            print(f"Не удалось сохранить сообщение: {type(exc).__name__}: {exc}")

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.sync$"))
    async def sync_current_chat(event):
        """Import recent accessible text history only for the chat where .sync was sent."""
        status = None
        try:
            chat_id = event.chat_id
            if chat_id is None:
                await event.respond("Не удалось определить текущий чат.")
                return

            await event.delete()
            status = await client.send_message(
                chat_id,
                f"Синхронизирую доступную историю этого чата (до {settings.max_messages_per_sync} сообщений)…",
            )

            history = [
                message async for message in client.iter_messages(
                    chat_id,
                    limit=settings.max_messages_per_sync,
                )
            ]
            rows = []
            skipped = 0
            for message in reversed(history):
                message_text = (message.message or "").strip()
                sender_id = message.sender_id
                if not message_text or not sender_id or message_text.startswith(".sync"):
                    skipped += 1
                    continue
                sender = message.sender
                if sender is not None and getattr(sender, "bot", False):
                    skipped += 1
                    continue
                rows.append((
                    chat_id,
                    sender_id,
                    getattr(sender, "username", None) if sender else None,
                    message_text,
                    message.id,
                    message.date.isoformat() if message.date else None,
                ))

            saved = await asyncio.to_thread(storage.save_messages_bulk, rows)
            duplicates = len(rows) - saved
            await status.edit(
                f"Синхронизация завершена. Обработано сообщений: {len(history)}. "
                f"Добавлено новых текстовых сообщений: {saved}. "
                f"Уже были в базе: {duplicates}. Пропущено: {skipped}.\n"
                "Теперь открой бота и отправь /analyze USER_ID. "
                "Для другого чата отдельно выполни .sync в том чате."
            )
        except Exception as exc:
            print(f"Ошибка синхронизации истории: {type(exc).__name__}: {exc}")
            try:
                if status:
                    await status.edit("Не удалось синхронизировать историю. Проверь логи Render.")
                else:
                    await event.respond("Не удалось синхронизировать историю. Проверь логи Render.")
            except Exception:
                pass

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
                event.chat_id,
                limit=settings.max_messages_per_analysis,
                from_user=target_id,
            ):
                text = (message.message or "").strip()
                if text and not text.startswith(".analyze"):
                    rows.append(text)
            rows.reverse()
            if len(rows) < 2:
                await client.send_message(
                    event.chat_id,
                    "Недостаточно текстовых сообщений этого пользователя в доступной истории чата.",
                )
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
                    "Недостаточно сохранённых сообщений этого пользователя. "
                    "Выполни .sync в нужных чатах или дождись новых сообщений.",
                )
                return
            await client.send_message(event.chat_id, f"Анализирую {len(rows)} сообщений из синхронизированных чатов…")
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
            "Watermelon Userbot\n"
            ".sync — импортировать доступную историю текущего чата в базу Watermelon.\n"
            ".analyze USER_ID — анализ истории пользователя в текущем чате.\n"
            ".analyze — в личном чате анализ собеседника.\n"
            ".analyzeall USER_ID — анализ сохранённых сообщений пользователя из синхронизированных чатов.\n"
            ".helpwatermelon — эта справка.\n\n"
            "Синхронизируй только чаты, в которых у тебя есть право обрабатывать сообщения."
        )

    # Avoid an interactive login prompt on Render when StringSession is invalid.
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "TG_SESSION недействителен или отозван. Создай новую строку: python -m app.create_session"
            )
    except AuthKeyDuplicatedError as exc:
        raise RuntimeError(
            "Одна и та же TG_SESSION используется из двух мест одновременно (например, локально и на Render). "
            "Останови второй процесс и создай новую сессию: python -m app.create_session"
        ) from exc

    _client = client
    me = await client.get_me()
    print(f"Watermelon Userbot запущен для аккаунта ID {me.id}.")
    print(
        f"Сбор новых входящих текстовых сообщений включён. "
        f"Для импорта истории конкретного чата отправь .sync (лимит: {settings.max_messages_per_sync})."
    )
    try:
        await client.run_until_disconnected()
    finally:
        _client = None


if __name__ == "__main__":
    asyncio.run(main())
