import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telethon.errors import ChatAdminRequiredError, ChannelPrivateError
from telethon.tl.types import Channel, Chat, User

from .config import settings
from .storage import Storage

HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Watermelon · Панель</title>
<style>
:root{color-scheme:light;--blue:#168cff;--line:#d9e5ef;--muted:#7b8794;--bg:#f6f8fa}
*{box-sizing:border-box}body{margin:0;background:var(--bg);font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#18212b}
header{padding:18px 18px 10px;position:sticky;top:0;background:#f6f8faf2;backdrop-filter:blur(12px);z-index:2}
h1{font-size:20px;margin:0 0 3px;font-weight:700}.sub{color:var(--muted);font-size:12px}
.search{display:flex;gap:8px;margin-top:14px}.search input{width:100%;border:1px solid var(--line);background:white;border-radius:10px;padding:11px 12px;font:inherit;outline-color:var(--blue)}
.back{border:1px solid var(--line);background:white;border-radius:10px;padding:0 13px;font:inherit;display:none}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:18px;padding:5px 18px 22px}
.card{min-width:0;text-align:left;border:0;background:transparent;padding:0;cursor:pointer;color:inherit}
.avatar{width:100%;aspect-ratio:1/1;border-radius:3px;background:#1b450d;color:white;display:flex;align-items:center;justify-content:center;overflow:hidden;font-size:22px;font-weight:600}
.avatar img{width:100%;height:100%;object-fit:cover}.info{padding:7px 1px 0}.name{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.desc{font-size:11px;color:var(--muted);margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:30px}
.state{padding:20px 18px;color:var(--muted)}.badge{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:3px 7px;font-size:10px;margin-top:5px;color:#56616d;background:white}
@media(max-width:560px){.grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:13px;padding:4px 12px 18px}header{padding:15px 12px 9px}.avatar{font-size:18px}}
</style>
</head>
<body>
<header><h1 id="title">Сообщения</h1><div class="sub" id="subtitle">Чаты, доступные вашему аккаунту Telegram</div>
<div class="search"><button class="back" id="back">← Назад</button><input id="query" placeholder="Поиск по чатам и участникам…"></div></header>
<div class="state" id="state">Загружаю…</div><main class="grid" id="grid"></main>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<script>
const grid=document.querySelector('#grid'),state=document.querySelector('#state'),title=document.querySelector('#title'),subtitle=document.querySelector('#subtitle'),query=document.querySelector('#query'),back=document.querySelector('#back');
let initData='',chatId=null,items=[];
try{initData=window.Telegram?.WebApp?.initData||'';window.Telegram?.WebApp?.ready();window.Telegram?.WebApp?.expand()}catch{}
function initials(s){return (s||'?').trim().split(/\s+/).slice(0,2).map(x=>x[0]||'').join('').toUpperCase()}
async function api(path){const r=await fetch(path,{headers:{'X-Telegram-Init-Data':initData}});const j=await r.json();if(!r.ok)throw Error(j.error||'Ошибка запроса');return j}
function render(){grid.innerHTML='';const q=query.value.trim().toLowerCase();const filtered=items.filter(x=>(x.title+' '+(x.description||'')+' '+(x.username||'')).toLowerCase().includes(q));state.hidden=filtered.length>0;state.textContent=filtered.length?'':(q?'Ничего не найдено':'Список пуст или нет доступа к данным.');for(const x of filtered){const b=document.createElement('button');b.className='card';b.innerHTML='<div class="avatar"></div><div class="info"><div class="name"></div><div class="desc"></div><span class="badge"></span></div>';const a=b.querySelector('.avatar');if(x.photo_url){const im=document.createElement('img');im.src=x.photo_url;im.alt='';im.onerror=()=>{im.remove();a.textContent=initials(x.title)};a.append(im)}else a.textContent=initials(x.title);b.querySelector('.name').textContent=x.title||'Без названия';b.querySelector('.desc').textContent=x.description||'Описание пока недоступно';b.querySelector('.badge').textContent=x.badge||'';b.onclick=()=>x.kind==='chat'?openChat(x):showMember(x);grid.append(b)}}
async function loadChats(){chatId=null;back.style.display='none';title.textContent='Сообщения';subtitle.textContent='Чаты, доступные вашему аккаунту Telegram';query.placeholder='Поиск по чатам…';query.value='';state.hidden=false;state.textContent='Загружаю чаты…';grid.innerHTML='';try{const d=await api('/api/chats');items=d.items||[];if(!items.length&&d.debug){state.textContent=`Диалогов найдено: ${d.debug.dialogs_found}; карточек: ${d.debug.items_returned}; аккаунт: ${d.debug.account_name||d.debug.account_id}. Если это не ваш аккаунт — проверьте TG_SESSION.`;state.hidden=false;grid.innerHTML='';return}render()}catch(e){state.textContent=e.message}}
async function openChat(chat){chatId=chat.id;title.textContent=chat.title;subtitle.textContent='Участники чата';back.style.display='block';query.placeholder='Поиск по участникам…';query.value='';state.hidden=false;state.textContent='Загружаю участников…';grid.innerHTML='';try{const d=await api('/api/chats/'+encodeURIComponent(chat.id)+'/members');items=d.items||[];if(!items.length){state.textContent=`Участники не получены. Загружено: ${d.total_loaded??0}. Telegram может ограничивать список участников.`;state.hidden=false;return}render()}catch(e){state.textContent=e.message}}
function showMember(m){const box=document.createElement('div');box.style.cssText='position:fixed;inset:0;background:#0006;z-index:5;display:grid;place-items:center;padding:18px';const panel=document.createElement('section');panel.style.cssText='background:white;border-radius:14px;padding:20px;max-width:440px;width:100%;max-height:85vh;overflow:auto';const close=document.createElement('button');close.textContent='Закрыть';close.style.cssText='float:right;padding:7px 10px';close.onclick=()=>box.remove();const h=document.createElement('h2');h.textContent=m.title;const p=document.createElement('p');p.textContent=m.description||'Недостаточно данных для краткого описания.';const details=document.createElement('pre');details.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 inherit';details.textContent='ID: '+m.id+'\nUsername: '+(m.username?'@'+m.username:'не указан')+'\nСообщений в базе: '+(m.message_count??'нет данных')+'\nПоследнее сообщение: '+(m.last_seen||'неизвестно')+'\n\nДанные основаны на доступном профиле Telegram и сохранённых сообщениях. Неизвестные сведения не предполагаются.';panel.append(close,h,p,details);box.append(panel);box.onclick=e=>{if(e.target===box)box.remove()};document.body.append(box)}
back.onclick=loadChats;query.oninput=render;loadChats();
</script>
</body></html>"""


def validate_init_data(init_data: str) -> dict:
    if not init_data or not settings.bot_token:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Откройте панель через команду /panel в Telegram"}), content_type="application/json")
    values = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)
    if not received_hash:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Нет подписи Telegram Web App"}), content_type="application/json")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
    secret_key = hmac.new(b"WebAppData", settings.bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Не удалось проверить Telegram Web App"}), content_type="application/json")
    auth_date = int(values.get("auth_date", "0"))
    if time.time() - auth_date > 86400:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Сессия панели устарела. Закройте и откройте /panel снова."}), content_type="application/json")
    try:
        user = json.loads(values.get("user", "{}"))
    except json.JSONDecodeError:
        user = {}
    if not user.get("id"):
        raise web.HTTPUnauthorized(text=json.dumps({"error": "Не удалось определить пользователя"}), content_type="application/json")
    return user


def require_auth(request):
    return validate_init_data(request.headers.get("X-Telegram-Init-Data", ""))


async def index(request):
    return web.Response(text=HTML, content_type="text/html")


async def chats(request):
    require_auth(request)
    from .userbot import get_client
    client = get_client()
    if not client or not client.is_connected():
        raise web.HTTPServiceUnavailable(
            text=json.dumps({"error": "Личный аккаунт Telegram ещё подключается"}),
            content_type="application/json",
        )
    try:
        me = await client.get_me()
        dialogs = [dialog async for dialog in client.iter_dialogs(limit=300)]
        items = []
        for dialog in dialogs:
            entity = dialog.entity
            if getattr(entity, "bot", False):
                continue
            if isinstance(entity, User):
                kind = "Личный чат"
            elif isinstance(entity, Channel) and getattr(entity, "megagroup", False):
                kind = "Группа"
            elif isinstance(entity, Channel):
                kind = "Канал"
            elif isinstance(entity, Chat):
                kind = "Группа"
            else:
                kind = "Чат"
            last_text = ""
            if dialog.message and getattr(dialog.message, "message", None):
                last_text = str(dialog.message.message).replace("\\n", " ")[:75]
            items.append({
                "id": dialog.id,
                "kind": "chat",
                "title": dialog.name or "Без названия",
                "description": kind + (" · " + last_text if last_text else ""),
                "badge": kind,
                "username": getattr(entity, "username", None),
                "photo_url": None,
            })
        print(
            f"Panel chats: account_id={getattr(me, 'id', None)}, "
            f"dialogs={len(dialogs)}, visible_items={len(items)}"
        )
        return web.json_response({
            "items": items,
            "debug": {
                "account_id": getattr(me, "id", None),
                "account_name": " ".join(filter(None, [
                    getattr(me, "first_name", None),
                    getattr(me, "last_name", None),
                ])),
                "dialogs_found": len(dialogs),
                "items_returned": len(items),
            },
        })
    except Exception as exc:
        print(f"Panel chats failed: {type(exc).__name__}: {exc}")
        raise web.HTTPBadGateway(
            text=json.dumps({
                "error": f"Не удалось загрузить диалоги Telegram ({type(exc).__name__}). Проверь логи Render."
            }),
            content_type="application/json",
        ) from exc

async def members(request):
    require_auth(request)
    from .userbot import get_client
    client = get_client()
    if not client or not client.is_connected():
        raise web.HTTPServiceUnavailable(text=json.dumps({"error": "Личный аккаунт Telegram ещё подключается"}), content_type="application/json")
    try:
        chat_id = int(request.match_info["chat_id"])
        entity = await client.get_entity(chat_id)
        storage = Storage(settings.database_path)
        items = []
        count = 0
        async for participant in client.iter_participants(entity, limit=500):
            if getattr(participant, "bot", False):
                continue
            count += 1
            first = getattr(participant, "first_name", None) or ""
            last = getattr(participant, "last_name", None) or ""
            title = (first + " " + last).strip() or getattr(participant, "title", None) or "Без имени"
            username = getattr(participant, "username", None)
            stats = storage.get_user_stats(chat_id, participant.id)
            items.append({
                "id": participant.id,
                "kind": "member",
                "title": title,
                "username": username,
                "description": ("@" + username if username else "Username не указан") + " · сообщений в базе: " + str(stats["message_count"]),
                "message_count": stats["message_count"],
                "last_seen": stats["last_seen"],
                "badge": "Участник",
                "photo_url": None,
            })
        return web.json_response({"items": items, "total_loaded": count, "limit": 500})
    except (ValueError, ChatAdminRequiredError, ChannelPrivateError) as exc:
        raise web.HTTPForbidden(text=json.dumps({"error": "Не удалось получить участников. Возможно, Telegram ограничивает доступ к списку участников этого чата."}), content_type="application/json") from exc


async def health(request):
    return web.json_response({"status": "ok", "service": "watermelon"})


def create_app():
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/panel", index)
    app.router.add_get("/api/chats", chats)
    app.router.add_get("/api/chats/{chat_id}/members", members)
    return app
