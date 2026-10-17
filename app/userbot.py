import asyncio
import html
import os
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from .config import settings
from .storage import Storage
from .analyzer import analyze_messages

def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value

def split_text(text: str, limit: int = 3500) -> list[str]:
    return [text[i:i + limit] for i in range(0, len(text), limit)] or ["{}"]

async def main():
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
            elif getattr(chat, "bot", False) or getattr(chat, "broadcast", False):
                await event.respond("Укажи ID пользователя: .analyze USER_ID")
                return
            elif getattr(chat, "megagroup", False) or getattr(chat, "gigagroup", False):
                await event.respond("В группе укажи ID пользователя: .analyze USER_ID")
                return
            else:
                target_id = getattr(chat, "id", None)
                if target_id is None:
                    await event.respond("Не удалось определить пользователя. Используй .analyze USER_ID")
                    return

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
                    "Недостаточно текстовых сообщений этого пользователя в доступной истории чата."
                )
                return
            await client.send_message(event.chat_id, f"Анализирую {len(rows)} сообщений…")
            result = await asyncio.to_thread(analyze_messages, rows)
            import json
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
                    "Пока недостаточно новых сообщений этого пользователя. Userbot сохраняет входящие сообщения после запуска."
                )
                return
            await client.send_message(event.chat_id, f"Анализирую {len(rows)} сообщений из разных чатов…")
            result = await asyncio.to_thread(analyze_messages, rows)
            import json
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
            ".analyze USER_ID — анализ доступной истории пользователя в текущем чате.\n"
            ".analyze — в личном чате анализ собеседника.\n"
            ".analyzeall USER_ID — анализ новых сообщений пользователя из разных чатов, полученных после запуска.\n"
            ".helpwatermelon — эта справка."
        )

    await client.start()
    me = await client.get_me()
    print(f"Watermelon Userbot запущен для аккаунта ID {me.id}.")
    print("Сбор входящих текстовых сообщений включён. Остановка — завершить процесс.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
