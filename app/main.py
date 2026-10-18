import asyncio
import json
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from .analyzer import analyze_messages
from .config import settings, validate_settings
from .storage import Storage


dp = Dispatcher()
storage = Storage(settings.database_path)


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Watermelon User Profiler\n"
        "Историю импортирует ваш авторизованный аккаунт через Telethon.\n"
        "1. В нужной группе или чате отправьте со своего аккаунта .sync\n"
        "2. Здесь отправьте /analyze USER_ID для анализа сообщений из синхронизированных чатов.\n"
        "3. Для одного чата используйте /analyze CHAT_ID USER_ID.\n"
        "Импортируйте только сообщения, которые разрешено обрабатывать."
    )


def get_panel_url() -> str:
    explicit = os.getenv("PANEL_URL", "").strip()
    if explicit:
        return explicit
    base = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    # Previously "" + "/panel" gave "/panel", which is truthy, so the
    # "not configured" check never fired and Telegram rejected the button.
    return base + "/panel" if base else ""


@dp.message(Command("panel"))
async def open_panel(message: Message):
    if message.chat.type != "private":
        await message.answer("Открой панель в личном чате с ботом: /panel")
        return
    panel_url = get_panel_url()
    if not panel_url:
        await message.answer("Панель ещё не настроена: добавь PANEL_URL в Environment Render.")
        return
    if not panel_url.startswith("https://"):
        await message.answer("Telegram Web App требует HTTPS. Проверь PANEL_URL (должен начинаться с https://).")
        return
    # Inline web_app button: Telegram passes signed initData (needed for auth).
    # Web Apps opened from a reply-keyboard button don't reliably provide it.
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть Watermelon", web_app=WebAppInfo(url=panel_url))]]
    )
    await message.answer("Открой панель Watermelon кнопкой ниже.", reply_markup=keyboard)


@dp.message(Command("analyze"))
async def analyze(message: Message):
    if not message.from_user:
        return

    parts = (message.text or "").split()
    if len(parts) == 2 and parts[1].lstrip("-").isdigit():
        chat_id = None
        target_id = int(parts[1])
    elif len(parts) == 3 and parts[1].lstrip("-").isdigit() and parts[2].isdigit():
        chat_id = int(parts[1])
        target_id = int(parts[2])
    else:
        await message.answer(
            "Формат команды:\n"
            "/analyze USER_ID — все синхронизированные чаты\n"
            "/analyze CHAT_ID USER_ID — один конкретный чат"
        )
        return

    if chat_id is None:
        messages = storage.get_messages_all(target_id, settings.max_messages_per_analysis)
    else:
        messages = storage.get_messages(chat_id, target_id, settings.max_messages_per_analysis)

    if len(messages) < 2:
        await message.answer(
            "Недостаточно сохранённых сообщений. Открой нужный чат со своего аккаунта "
            "и отправь там .sync, затем повтори /analyze USER_ID."
        )
        return

    await message.answer(
        f"Анализирую {len(messages)} сохранённых сообщений. Это может занять некоторое время."
    )
    try:
        result = await asyncio.to_thread(analyze_messages, messages)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if len(output) <= 4000:
            await message.answer(f"<pre>{escape_html(output)}</pre>", parse_mode="HTML")
        else:
            for start in range(0, len(output), 3500):
                await message.answer(
                    f"<pre>{escape_html(output[start:start + 3500])}</pre>",
                    parse_mode="HTML",
                )
    except Exception as exc:
        print(f"Ошибка анализа через бота: {type(exc).__name__}: {exc}")
        await message.answer("Не удалось выполнить анализ. Проверь настройки API и логи Render.")


@dp.message(F.text)
async def collect(message: Message):
    # Keep bot-delivered messages as a fallback; Telethon history sync is the main source.
    if not message.from_user or message.from_user.is_bot or not message.chat:
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    storage.save_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        message_text=text,
        telegram_message_id=message.message_id,
        created_at=message.date.isoformat() if message.date else None,
    )


def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def main():
    validate_settings()
    bot = Bot(token=settings.bot_token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
