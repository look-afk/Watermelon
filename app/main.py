import asyncio
import json
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from .analyzer import analyze_messages
from .ai_providers import PRESETS, add_provider, analyze_with_provider, get_providers, public_providers, remove_provider
from .diagnostics import recent_errors, uptime_seconds
from .config import settings, validate_settings
from .storage import Storage
from .userbot import SYNC_STATE, SYNC_WAKE, get_client


dp = Dispatcher()
storage = Storage(settings.database_path)
AI_SETUP = {}


async def is_owner_id(user_id: int) -> bool:
    client = get_client()
    if not client or not client.is_connected():
        return False
    try:
        me = await client.get_me()
        return user_id == me.id
    except Exception:
        return False


async def ensure_owner(message: Message) -> bool:
    if message.chat.type != "private" or not message.from_user:
        return False
    if await is_owner_id(message.from_user.id):
        return True
    await message.answer("Доступ закрыт: Watermelon предназначен только для владельца подключённого Telegram-аккаунта.")
    return False


@dp.message(Command("diag"))
async def diagnostics_command(message: Message):
    if not await ensure_owner(message):
        return
    client = get_client()
    connected = bool(client and client.is_connected())
    state = dict(SYNC_STATE)
    try:
        totals = await asyncio.to_thread(storage.get_totals)
    except Exception as exc:
        totals = None
        print(f"Диагностика базы не удалась: {type(exc).__name__}")
    providers = await asyncio.to_thread(public_providers)
    uptime = uptime_seconds()
    lines = [
        "Watermelon — диагностика",
        f"Веб-панель: процесс работает (uptime {uptime // 3600} ч {(uptime % 3600) // 60} мин)",
        "Telegram Bot: процесс запущен; polling проверяется по журналу ошибок",
        f"Telethon: {'подключён' if connected else 'НЕ подключён'}",
        f"Синхронизация: {'идёт' if state.get('running') else 'ожидает'}; чаты {state.get('done', 0)}/{state.get('total', 0)}; сейчас: {state.get('current_chat') or '—'}",
        f"Резервная копия: {'включена' if settings.backup_enabled else 'выключена'}",
        f"Основной ИИ: {settings.llm_model if settings.llm_api_key else 'не настроен'}",
        f"Дополнительные ИИ: {len(providers)}",
    ]
    if totals:
        lines.append(f"База: {totals.get('messages', 0)} сообщений, {totals.get('chats', 0)} чатов, {totals.get('people', 0)} людей")
    errors = recent_errors()
    lines.append("\nПоследние ошибки:")
    lines.extend(f"• {item[:260]}" for item in errors[-6:])
    if not errors:
        lines.append("За время работы ошибок в журнале пока нет.")
    await message.answer("\n".join(lines)[:3900])


@dp.message(Command("ai_add"))
async def ai_add_command(message: Message):
    if not await ensure_owner(message):
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Google Gemini", callback_data="ai_provider:gemini")],
        [InlineKeyboardButton(text="OpenRouter", callback_data="ai_provider:openrouter")],
        [InlineKeyboardButton(text="OpenAI", callback_data="ai_provider:openai")],
        [InlineKeyboardButton(text="Другой OpenAI-compatible API", callback_data="ai_provider:custom")],
    ])
    await message.answer("Выбери провайдера. API-ключ будет удалён из чата после получения и сохранён в зашифрованном виде.", reply_markup=keyboard)


@dp.callback_query(F.data.startswith("ai_provider:"))
async def ai_provider_choice(callback: CallbackQuery):
    if not callback.from_user or not await is_owner_id(callback.from_user.id):
        await callback.answer("Доступ закрыт", show_alert=True)
        return
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Открой личный чат с ботом", show_alert=True)
        return
    choice = callback.data.split(":", 1)[1]
    if choice == "custom":
        AI_SETUP[callback.from_user.id] = {"step": "base", "name": "Custom API"}
        await callback.message.answer("Отправь HTTPS Base URL, например https://example.com/v1/")
    elif choice in PRESETS:
        preset = PRESETS[choice]
        AI_SETUP[callback.from_user.id] = {
            "step": "key", "name": preset["label"],
            "base_url": preset["base_url"], "model": preset["model"],
        }
        await callback.message.answer(
            f"Провайдер: {preset['label']}\nМодель по умолчанию: {preset['model']}\n"
            "Теперь отправь API-ключ отдельным сообщением. Бот постарается удалить это сообщение сразу."
        )
    else:
        await callback.answer("Неизвестный провайдер", show_alert=True)
        return
    await callback.answer()


@dp.message(Command("ai_list"))
async def ai_list_command(message: Message):
    if not await ensure_owner(message):
        return
    providers = await asyncio.to_thread(public_providers)
    if not providers:
        await message.answer("Дополнительные ИИ не подключены. Используй /ai_add.")
        return
    text = "Подключённые дополнительные ИИ:\n" + "\n".join(
        f"ID {p['id']}: {p['name']} — {p['model']} ({p['base_url']})" for p in providers
    )
    await message.answer(text[:3900] + "\n\nУдалить: /ai_remove ID")


@dp.message(Command("ai_remove"))
async def ai_remove_command(message: Message):
    if not await ensure_owner(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /ai_remove ID. Список: /ai_list")
        return
    removed = await asyncio.to_thread(remove_provider, int(parts[1]))
    await message.answer("Провайдер удалён." if removed else "Провайдер с таким ID не найден.")


@dp.message(F.text, lambda message: bool(message.from_user and message.from_user.id in AI_SETUP))
async def ai_setup_input(message: Message):
    if not message.from_user or not await is_owner_id(message.from_user.id):
        AI_SETUP.pop(message.from_user.id if message.from_user else 0, None)
        return
    state = AI_SETUP.get(message.from_user.id)
    if not state:
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        AI_SETUP.pop(message.from_user.id, None)
        await message.answer("Настройка дополнительного ИИ отменена.")
        return
    if state["step"] == "base":
        if not value.startswith("https://"):
            await message.answer("Нужен HTTPS Base URL. Попробуй ещё раз или /cancel.")
            return
        state["base_url"] = value
        state["step"] = "model"
        await message.answer("Теперь отправь точное название модели, например provider/model.")
        return
    if state["step"] == "model":
        if not value or len(value) > 160:
            await message.answer("Название модели пустое или слишком длинное. Попробуй ещё раз.")
            return
        state["model"] = value
        state["step"] = "key"
        await message.answer("Теперь отправь API-ключ отдельным сообщением. Бот удалит сообщение и не покажет ключ в ответе.")
        return
    if state["step"] == "key":
        try:
            await message.delete()
        except Exception:
            pass
        try:
            provider_id = await asyncio.to_thread(
                add_provider, state["name"], state["base_url"], state["model"], value
            )
            AI_SETUP.pop(message.from_user.id, None)
            await message.answer(
                f"ИИ подключён и проверен. ID: {provider_id}; модель: {state['model']}.\n"
                "Он будет участвовать в командах /analyze. Управление: /ai_list, /ai_remove ID."
            )
        except Exception as exc:
            print(f"Не удалось подключить дополнительный ИИ: {type(exc).__name__}: {str(exc)[:180]}")
            await message.answer(
                f"Не удалось проверить API: {type(exc).__name__}: {str(exc)[:220]}\n"
                "Отправь корректный API-ключ ещё раз или /cancel."
            )


@dp.message(CommandStart())
async def start(message: Message):
    if not await ensure_owner(message):
        return
    await message.answer(
        "Watermelon User Profiler\n"
        "Бот-аналитик: чаты читаются автоматически через ваш аккаунт.\n"
        "/panel — открыть панель: вкладки «Чаты» и «Люди», профили, связи, статистика.\n"
        "/sync — запустить синхронизацию всех доступных чатов и смотреть прогресс.\n"
        "/analyze USER_ID — анализ сообщений пользователя из всех чатов.\n"
        "/analyze CHAT_ID USER_ID — анализ в одном чате.\n"
        "/diag — диагностика.\n"
        "/ai_add, /ai_list — подключить и управлять дополнительными ИИ."
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
    if not await ensure_owner(message):
        return
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


@dp.message(Command("sync"))
async def sync_chats(message: Message):
    if message.chat.type != "private" or not message.from_user:
        await message.answer("Используй /sync в личном чате с ботом.")
        return

    client = get_client()
    if not client or not client.is_connected():
        await message.answer("Личный аккаунт Telegram ещё подключается. Попробуй позже.")
        return

    me = await client.get_me()
    if message.from_user.id != me.id:
        await message.answer("Команда доступна только владельцу подключённого Telegram-аккаунта.")
        return

    if SYNC_STATE.get("running"):
        await message.answer(format_sync_status(SYNC_STATE, prefix="Синхронизация уже идёт."))
        return

    before_cycles = SYNC_STATE.get("cycles", 0)
    SYNC_WAKE.set()
    status_message = await message.answer("Запускаю синхронизацию чатов в фоне…")
    last_text = ""
    try:
        while True:
            state = dict(SYNC_STATE)
            text = format_sync_status(state, prefix="Синхронизация запущена.")
            if text != last_text:
                try:
                    await status_message.edit_text(text)
                    last_text = text
                except Exception:
                    pass
            if state.get("cycles", 0) > before_cycles and not state.get("running") and state.get("backlog", 0) == 0:
                await status_message.edit_text(format_sync_status(state, prefix="Синхронизация истории завершена."))
                return
            await asyncio.sleep(5)
    except asyncio.CancelledError:
        raise


def format_sync_status(state: dict, prefix: str = "Состояние синхронизации.") -> str:
    total = int(state.get("total", 0) or 0)
    done = int(state.get("done", 0) or 0)
    percent = min(100, int(done * 100 / total)) if total else 0
    current = state.get("current_chat") or ("Подготовка списка чатов…" if state.get("running") else "Ожидание запуска")
    status = "работает" if state.get("running") else "ожидает следующего прохода"
    return (
        f"{prefix}\n"
        f"Состояние: {status}\n"
        f"Сейчас: {current}\n"
        f"Прогресс прохода: {percent}% ({done}/{total} чатов)\n"
        f"Новых сообщений сохранено: {state.get('new_messages', 0)}\n"
        f"Чатов с ещё не загруженной историей: {state.get('backlog', 0)}"
    )


@dp.message(Command("analyze"))
async def analyze(message: Message):
    if not await ensure_owner(message):
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
            "Недостаточно сохранённых сообщений. История синхронизируется автоматически — "
            "проверь прогресс в /panel и повтори позже."
        )
        return

    await message.answer(
        f"Анализирую {len(messages)} сохранённых сообщений. Это может занять некоторое время."
    )
    providers = await asyncio.to_thread(get_providers)
    jobs = [("Основной ИИ", asyncio.to_thread(analyze_messages, messages))]
    jobs.extend((f"{p['name']} · {p['model']}", asyncio.to_thread(analyze_with_provider, p, messages)) for p in providers)
    for label, job in jobs:
        try:
            result = await job
            output = json.dumps(result, ensure_ascii=False, indent=2)
            await message.answer(f"<b>{escape_html(label)}</b>\n<pre>{escape_html(output[:3400])}</pre>", parse_mode="HTML")
        except Exception as exc:
            print(f"Анализ через {label} не удался: {type(exc).__name__}: {str(exc)[:180]}")
            await message.answer(f"{escape_html(label)}: анализ не удался ({type(exc).__name__}). Проверь лимит API и /diag.")


@dp.message(F.text)
async def collect(message: Message):
    if not message.from_user or not await is_owner_id(message.from_user.id):
        return
    if message.chat.type != "private":
        return
    # Keep bot-delivered messages as a fallback; Telethon history sync is the main source.
    if not message.from_user or message.from_user.is_bot or not message.chat:
        return
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if not storage.should_store(message.chat.id, message.from_user.id):
        return  # excluded chat or ignored person
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