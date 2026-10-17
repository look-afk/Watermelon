import asyncio
import json
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from .config import settings, validate_settings
from .storage import Storage
from .analyzer import analyze_messages

dp = Dispatcher()
storage = Storage(settings.database_path)

@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Telegram User Profiler\n"
        "Сохраняю текстовые сообщения, которые Telegram доставляет боту.\n"
        "Команда: /analyze <user_id> — анализ сообщений пользователя в этом чате.\n"
        "Используйте бота только при наличии права обрабатывать сообщения участников."
    )

@dp.message(Command("analyze"))
async def analyze(message: Message):
    if not message.from_user or not message.chat:
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].strip().isdigit():
        await message.answer("Формат: /analyze <user_id>")
        return
    target_id = int(parts[1].strip())
    messages = storage.get_messages(message.chat.id, target_id, settings.max_messages_per_analysis)
    if len(messages) < 2:
        await message.answer("Недостаточно сохранённых сообщений. Бот анализирует только сообщения, которые получил после запуска.")
        return
    await message.answer(f"Анализирую {len(messages)} сообщений. Это может занять некоторое время.")
    try:
        result = await asyncio.to_thread(analyze_messages, messages)
        output = json.dumps(result, ensure_ascii=False, indent=2)
        # Telegram message limit is ~4096 characters.
        if len(output) <= 4000:
            await message.answer(f"<pre>{escape_html(output)}</pre>", parse_mode="HTML")
        else:
            for start in range(0, len(output), 3500):
                await message.answer(f"<pre>{escape_html(output[start:start+3500])}</pre>", parse_mode="HTML")
    except Exception as exc:
        await message.answer(f"Не удалось выполнить анализ: {type(exc).__name__}. Проверьте настройки API и логи.")

@dp.message(F.text)
async def collect(message: Message):
    if not message.from_user or message.from_user.is_bot or not message.chat:
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    storage.save_message(
        chat_id=message.chat.id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        message_text=text
    )

def escape_html(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

async def main():
    validate_settings()
    bot = Bot(token=settings.bot_token)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
