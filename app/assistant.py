"""Bot commands of the assistant: /ask /who /find /ties /todo /digest /alerts /watch /auto /autos /stop /autolog /menu.

Handlers are put in FRONT of the old catch-all text handler of main.py (aiogram uses the first match)."""
import asyncio
import os
import re
import secrets
import time

from aiogram import F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import assistdb, llm, memory, tasks
from .persona import MAX_CHATS, Persona
from .userbot import get_client, get_owner_id

PERSONA: Persona | None = None
PENDING: dict[str, dict] = {}
STATE = {"me": None, "alert_client": None}
STORAGE = None

MENU = (
    "Что я умею:\n"
    "• Просто напиши вопрос — отвечу по твоим перепискам («что мне Алекс говорил про цену?») или /ask вопрос\n"
    "• /who имя — досье перед разговором: кто это, что обещано, с чего начать\n"
    "• /todo — обещания и дедлайны из переписок (кто кому что должен)\n"
    "• /digest — сводка сейчас; /digest on 9 — каждый день в 9:00; /digest off\n"
    "• /ties — с кем связь остыла; /find кого-то — найти нужного человека среди контактов\n"
    "• /alerts on|off и /watch слово, слово — оповещения об упоминаниях и словах\n"
    "• /auto название группы — автоответчик в группе от твоего имени; /autos, /stop, /autolog\n"
    "• /panel — панель с профилями, /sync — синхронизация, /diag — диагностика"
)


def front(observer, callback, *filters):
    observer.register(callback, *filters)
    observer.handlers.insert(0, observer.handlers.pop())


def arg_of(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def send_long(message: Message, text: str, status: Message | None = None, markup=None):
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or ["—"]
    first = True
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        if first and status is not None:
            try:
                await status.edit_text(chunk, reply_markup=markup if last else None, disable_web_page_preview=True)
                first = False
                continue
            except Exception:
                pass
        await message.answer(chunk, reply_markup=markup if last else None, disable_web_page_preview=True)
        first = False


def _no_llm() -> str | None:
    return None if llm.available() else "ИИ не настроен: задай LLM_API_KEY в Render или добавь провайдера через /ai_add."


def todo_view(items: list[dict]):
    if not items:
        return "Открытых обещаний нет. Я ищу их в переписках автоматически каждые полчаса (/todo scan — проверить сейчас).", None
    mine = [c for c in items if c["direction"] == "i_owe"]
    theirs = [c for c in items if c["direction"] == "they_owe"]
    lines, rows, n = [], [], 0
    for title, group in (("Вы должны", mine), ("Вам должны", theirs)):
        if group:
            lines.append(f"{title}:")
            for c in group[:10]:
                n += 1
                lines.append(f"{n}. {tasks.format_commitment(c)}")
                rows.append([InlineKeyboardButton(text=f"✅ {n}", callback_data=f"todo:done:{c['id']}"),
                             InlineKeyboardButton(text=f"🗑 {n}", callback_data=f"todo:skip:{c['id']}")])
            lines.append("")
    lines.append("✅ — выполнено, 🗑 — неактуально.")
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows[:16])


def install(dp):
    """Register everything on the dispatcher. Called once from web.py."""
    global PERSONA, STORAGE
    from .main import AI_SETUP, ensure_owner, is_owner_id, storage
    STORAGE = storage
    assistdb.init(storage)
    PERSONA = Persona(storage, get_owner_id)
    PERSONA.load()

    # ---------- free text = a question about the chats (registered first -> ends up AFTER the commands)
    async def plain_question(message: Message):
        if not await ensure_owner(message):
            return
        await answer_question(message, (message.text or "").strip())

    async def answer_question(message: Message, question: str):
        if not question:
            await message.answer("Напиши вопрос, например: что мне говорил Алекс про цену?")
            return
        if (err := _no_llm()):
            await message.answer(err)
            return
        status = await message.answer("Ищу по твоим перепискам…")
        try:
            answer, sources = await memory.ask(storage, question)
        except Exception as exc:
            print(f"/ask: {type(exc).__name__}: {exc}")
            await status.edit_text(f"Не получилось ответить ({type(exc).__name__}). Загляни в /diag.")
            return
        text = answer + ("\n\nИсточники:\n" + "\n".join(sources) if sources else "")
        await send_long(message, text, status)

    front(dp.message, plain_question, F.chat.type == "private", F.text, ~F.text.startswith("/"),
          lambda m: bool(m.from_user and m.from_user.id not in AI_SETUP))

    async def cmd_ask(message: Message):
        if await ensure_owner(message):
            await answer_question(message, arg_of(message))

    async def cmd_who(message: Message):
        if not await ensure_owner(message):
            return
        if not arg_of(message):
            await message.answer("Формат: /who @username или часть имени")
            return
        status = await message.answer("Собираю досье…")
        await send_long(message, await memory.who(storage, arg_of(message), get_owner_id() or 0), status)

    async def cmd_find(message: Message):
        if not await ensure_owner(message):
            return
        if not arg_of(message):
            await message.answer("Опиши, кого ищешь: /find дизайнер интерфейсов")
            return
        if (err := _no_llm()):
            await message.answer(err)
            return
        status = await message.answer("Ищу среди людей из твоих чатов…")
        await send_long(message, await memory.find(storage, arg_of(message), get_owner_id() or 0), status)

    async def cmd_ties(message: Message):
        if await ensure_owner(message):
            await send_long(message, await asyncio.to_thread(memory.ties, storage, get_owner_id() or 0))

    async def cmd_todo(message: Message):
        if not await ensure_owner(message):
            return
        if arg_of(message).lower() == "scan":
            status = await message.answer("Ищу обещания в последних сообщениях…")
            storage_marker = await asyncio.to_thread(assistdb.kv_get, storage, "commit_since")
            await asyncio.to_thread(assistdb.kv_set, storage, "commit_since", assistdb.iso(assistdb.now() - __import__("datetime").timedelta(days=7)))
            added = await tasks.scan_commitments(storage, get_owner_id() or 0)
            await status.edit_text(f"Готово, найдено новых обязательств: {added}. Открой /todo.")
            return
        text, markup = todo_view(await asyncio.to_thread(tasks.list_commitments, storage))
        await message.answer(text, reply_markup=markup)

    async def todo_callback(callback: CallbackQuery):
        if not callback.from_user or not await is_owner_id(callback.from_user.id):
            await callback.answer("Доступ закрыт", show_alert=True)
            return
        _, action, cid = callback.data.split(":")
        await asyncio.to_thread(tasks.set_commitment, storage, int(cid), "done" if action == "done" else "dismissed")
        text, markup = todo_view(await asyncio.to_thread(tasks.list_commitments, storage))
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        await callback.answer("Готово")

    async def cmd_digest(message: Message):
        if not await ensure_owner(message):
            return
        arg = arg_of(message).lower().split()
        if arg and arg[0] in ("on", "off"):
            cfg = {"on": arg[0] == "on", "hour": int(arg[1]) if len(arg) > 1 and arg[1].isdigit() and 0 <= int(arg[1]) < 24 else 9}
            await asyncio.to_thread(assistdb.kv_set, storage, "digest", cfg)
            await message.answer(f"Ежедневная сводка включена: каждый день в {cfg['hour']}:00." if cfg["on"] else "Ежедневная сводка выключена.")
            return
        status = await message.answer("Собираю сводку…")
        text = await tasks.build_digest(storage, get_owner_id() or 0, getattr(STATE["me"], "username", None))
        await send_long(message, text, status)

    async def cmd_alerts(message: Message):
        if not await ensure_owner(message):
            return
        cfg = await asyncio.to_thread(assistdb.kv_get, storage, "alerts") or {"on": True, "words": []}
        arg = arg_of(message).lower()
        if arg in ("on", "off"):
            cfg["on"] = arg == "on"
            await asyncio.to_thread(assistdb.kv_set, storage, "alerts", cfg)
        await message.answer(f"Оповещения: {'включены' if cfg['on'] else 'выключены'} (упоминания и ответы вам в группах)."
                             f"\nСлова: {', '.join(cfg['words']) or 'не заданы'}\nВкл/выкл: /alerts on|off, слова: /watch слово, слово")

    async def cmd_watch(message: Message):
        if not await ensure_owner(message):
            return
        cfg = await asyncio.to_thread(assistdb.kv_get, storage, "alerts") or {"on": True, "words": []}
        arg = arg_of(message)
        if arg.lower() in ("off", "clear"):
            cfg["words"] = []
        elif arg:
            cfg["words"] = [w.strip().lower() for w in re.split(r"[,;]", arg) if w.strip()][:20]
        else:
            await message.answer("Формат: /watch слово, другое слово (или /watch off). Сейчас: " + (", ".join(cfg["words"]) or "—"))
            return
        await asyncio.to_thread(assistdb.kv_set, storage, "alerts", cfg)
        await message.answer("Слежу за словами: " + (", ".join(cfg["words"]) or "ничего"))

    # ---------- /auto
    async def cmd_auto(message: Message):
        if not await ensure_owner(message):
            return
        arg = arg_of(message)
        if arg.lower() in ("off", "stop"):
            n = await asyncio.to_thread(PERSONA.disable)
            await message.answer(f"Автоответчик выключен везде ({n}).")
            return
        if not arg:
            await message.answer("Формат: /auto название группы\nИли для вступления: /auto https://t.me/… или /auto @username\n"
                                 "Можно добавить заметку после «|»: /auto Мемы | мне 22, люблю футбол\nСписок: /autos, выключить: /stop")
            return
        if (err := _no_llm()):
            await message.answer(err)
            return
        client = get_client()
        if not client or not client.is_connected():
            await message.answer("Личный аккаунт ещё подключается. Повтори через минуту.")
            return
        query, _, note = arg.partition("|")
        query, note = query.strip(), note.strip()[:300]
        if re.match(r"(https?://)?t\.me/|@\w{4,}", query):
            token = secrets.token_hex(4)
            PENDING[token] = {"kind": "join", "query": query, "note": note, "at": time.time()}
            await message.answer(f"Вступить в группу {query} и включить там автоответчик?\n\n" + RULES,
                                 reply_markup=confirm_markup(token))
            return
        dialogs = [d for d in await client.get_dialogs(limit=500) if d.is_group and query.lower() in (d.name or "").lower()]
        if not dialogs:
            await message.answer("Не нашёл такую группу среди твоих чатов. Если в неё нужно вступить — пришли ссылку или @username.")
            return
        if len(dialogs) > 1:
            rows = []
            for d in dialogs[:6]:
                token = secrets.token_hex(4)
                PENDING[token] = {"kind": "chat", "chat_id": d.id, "title": d.name, "note": note, "at": time.time()}
                rows.append([InlineKeyboardButton(text=(d.name or "?")[:40], callback_data=f"auto:ask:{token}")])
            await message.answer("Нашлось несколько групп, выбери:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
            return
        d = dialogs[0]
        token = secrets.token_hex(4)
        PENDING[token] = {"kind": "chat", "chat_id": d.id, "title": d.name, "note": note, "at": time.time()}
        await message.answer(f"Включить автоответчик в «{d.name}»?\n\n" + RULES, reply_markup=confirm_markup(token))

    def confirm_markup(token: str):
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Включить", callback_data=f"auto:ok:{token}"),
                                                      InlineKeyboardButton(text="Отмена", callback_data=f"auto:no:{token}")]])

    async def auto_callback(callback: CallbackQuery):
        if not callback.from_user or not await is_owner_id(callback.from_user.id):
            await callback.answer("Доступ закрыт", show_alert=True)
            return
        _, action, token = callback.data.split(":", 2)
        if action == "stop":
            await asyncio.to_thread(PERSONA.disable, int(token))
            await callback.message.answer("Выключено.")
            await callback.answer()
            return
        item = PENDING.get(token)
        if not item or time.time() - item["at"] > 900:
            await callback.answer("Запрос устарел, повтори /auto", show_alert=True)
            return
        if action == "no":
            PENDING.pop(token, None)
            await callback.message.edit_text("Отменено.")
            await callback.answer()
            return
        if action == "ask":  # picked one of several groups
            await callback.message.edit_text(f"Включить автоответчик в «{item['title']}»?\n\n" + RULES, reply_markup=confirm_markup(token))
            await callback.answer()
            return
        PENDING.pop(token, None)
        try:
            chat_id, title = item.get("chat_id"), item.get("title")
            if item["kind"] == "join":
                chat_id, title = await join_group(get_client(), item["query"])
            problem = await asyncio.to_thread(PERSONA.enable, chat_id, title or str(chat_id), item["note"])
        except Exception as exc:
            await callback.message.edit_text(f"Не получилось: {type(exc).__name__}: {str(exc)[:160]}")
            await callback.answer()
            return
        await callback.message.edit_text(problem or f"Автоответчик включён в «{title}». Он молчит, пока ты сам пишешь в чате, "
                                                    f"и зовёт тебя, когда нужен ты. Выключить: /stop. Что писал: /autolog.")
        await callback.answer()

    async def cmd_autos(message: Message):
        if not await ensure_owner(message):
            return
        if not PERSONA.chats:
            await message.answer("Автоответчик нигде не включён. /auto название группы")
            return
        rows = [[InlineKeyboardButton(text=f"⏹ {st.title[:35]}", callback_data=f"auto:stop:{cid}")] for cid, st in PERSONA.chats.items()]
        await message.answer("Автоответчик включён в:\n" + "\n".join(f"• {st.title}" for st in PERSONA.chats.values())
                             + f"\n(максимум {MAX_CHATS})", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def cmd_stop(message: Message):
        if not await ensure_owner(message):
            return
        arg = arg_of(message).lower()
        targets = [cid for cid, st in PERSONA.chats.items() if not arg or arg in st.title.lower()]
        for cid in targets:
            await asyncio.to_thread(PERSONA.disable, cid)
        await message.answer(f"Выключил автоответчик в чатах: {len(targets)}." if targets else "Нечего выключать.")

    async def cmd_autolog(message: Message):
        if not await ensure_owner(message):
            return
        with assistdb.db(storage) as con:
            rows = con.execute("SELECT l.at, COALESCE(a.title,l.chat_id), l.kind, l.text FROM auto_log l LEFT JOIN auto_chats a "
                               "ON a.chat_id=l.chat_id ORDER BY l.id DESC LIMIT 15").fetchall()
        icon = {"sent": "💬", "handoff": "🤝", "silent": "🤐", "blocked": "⛔", "duplicate": "♻️"}
        await message.answer("\n".join(f"{icon.get(k, '•')} {t[5:16].replace('T', ' ')} · {c}: {x[:120]}" for t, c, k, x in rows) or "Пока пусто.")

    async def cmd_menu(message: Message):
        if await ensure_owner(message):
            await message.answer(MENU)

    for name, fn in (("ask", cmd_ask), ("who", cmd_who), ("find", cmd_find), ("ties", cmd_ties), ("todo", cmd_todo),
                     ("digest", cmd_digest), ("alerts", cmd_alerts), ("watch", cmd_watch), ("auto", cmd_auto),
                     ("autos", cmd_autos), ("stop", cmd_stop), ("autolog", cmd_autolog), ("menu", cmd_menu), ("help", cmd_menu)):
        front(dp.message, fn, Command(name))
    dp.callback_query.register(todo_callback, F.data.startswith("todo:"))
    dp.callback_query.register(auto_callback, F.data.startswith("auto:"))
    print("Ассистент: команды /ask /who /todo /digest /ties /find /alerts /watch /auto /menu подключены.")


RULES = ("Как это работает: бот пишет в группе от твоего имени в твоём стиле, но только когда ты сам не пишешь там, "
         "не ночью и не слишком часто. Если спросят, бот ли это, или попросят деньги/данные/обещание — он промолчит и позовёт тебя. "
         "В других чатах ничего не читает и не использует.")


async def join_group(client, query: str):
    from telethon import utils
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest
    invite = re.search(r"t\.me/(?:\+|joinchat/)([\w-]+)", query)
    if invite:
        updates = await client(ImportChatInviteRequest(invite.group(1)))
        chat = updates.chats[0]
    else:
        name = re.sub(r"^(https?://)?t\.me/", "", query).lstrip("@").split("/")[0]
        chat = await client.get_entity(name)
        await client(JoinChannelRequest(chat))
    return utils.get_peer_id(chat), getattr(chat, "title", None) or str(chat.id)


# ---------------------------------------------------------------- background part
async def alert_handler_factory():
    from telethon import events

    async def on_alert(event):
        try:
            cfg = assistdb.kv_get(STORAGE, "alerts") or {"on": True, "words": []}
            if not cfg.get("on") or (event.is_channel and not event.is_group):
                return
            sender = await event.get_sender()
            if not sender or getattr(sender, "bot", False):
                return
            if not STORAGE.should_store(event.chat_id, sender.id):
                return
            text = event.raw_text or ""
            why = None
            if event.chat_id < 0:
                replied_to_me = False
                if event.is_reply:
                    reply = await event.get_reply_message()
                    replied_to_me = bool(reply and reply.out)
                if event.message.mentioned or replied_to_me:
                    why = "Вас упомянули"
            if not why:
                low = text.lower()
                hit = next((w for w in cfg.get("words", []) if w and w in low), None)
                why = f"Слово «{hit}»" if hit else None
            if why and tasks.alert_due(event.chat_id):
                chat = await event.get_chat()
                title = getattr(chat, "title", None) or "личный чат"
                name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or "кто-то"
                await tasks.bot_send(tasks.mention_alert_text(title, name, text, why), get_owner_id())
        except Exception as exc:
            print(f"Оповещения: {type(exc).__name__}: {exc}")

    return events.NewMessage(incoming=True), on_alert


async def alert_loop():
    while True:
        try:
            client = get_client()
            if client is not None and client.is_connected() and client is not STATE["alert_client"]:
                pattern, handler = await alert_handler_factory()
                client.add_event_handler(handler, pattern)
                STATE["alert_client"] = client
                STATE["me"] = await client.get_me()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Оповещения: {type(exc).__name__}: {exc}")
        await asyncio.sleep(15)


async def run():
    """Background loops (supervised from web.py)."""
    if PERSONA is None:
        raise RuntimeError("assistant.install(dp) не был вызван")
    await asyncio.gather(
        tasks.scheduler_loop(STORAGE, get_owner_id, lambda: getattr(STATE["me"], "username", None)),
        PERSONA.run(get_client),
        alert_loop(),
    )
