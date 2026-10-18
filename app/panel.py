import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from aiohttp import web
from telethon.errors import ChannelPrivateError, ChatAdminRequiredError, FloodWaitError
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
const $=s=>document.querySelector(s);
const grid=$('#grid'),state=$('#state'),title=$('#title'),subtitle=$('#subtitle'),query=$('#query'),back=$('#back');
let initData='',items=[],reqId=0;
try{const tg=window.Telegram&&window.Telegram.WebApp;if(tg){initData=tg.initData||'';tg.ready();tg.expand()}}catch(e){}
function initials(s){return (s||'?').trim().split(/\s+/).slice(0,2).map(x=>x[0]||'').join('').toUpperCase()||'?'}
function sleep(ms){return new Promise(r=>setTimeout(r,ms))}
function showState(t){state.textContent=t;state.hidden=!t}
async function api(path){
  const r=await fetch(path,{headers:{'X-Telegram-Init-Data':initData}});
  let j={};try{j=await r.json()}catch(e){}
  if(!r.ok){const err=new Error(j.error||('Ошибка запроса ('+r.status+')'));err.status=r.status;throw err}
  return j;
}
async function apiRetry(path,id){
  for(let i=0;;i++){
    try{return await api(path)}
    catch(e){
      if(e.status!==503||i>=8||id!==reqId)throw e;
      showState('Подключаюсь к Telegram… попытка '+(i+2));
      await sleep(2500);
      if(id!==reqId)throw e;
    }
  }
}
function render(){
  grid.innerHTML='';
  const q=query.value.trim().toLowerCase();
  const list=items.filter(x=>((x.title||'')+' '+(x.description||'')+' '+(x.username||'')).toLowerCase().includes(q));
  showState(list.length?'':(q?'Ничего не найдено':'Список пуст или нет доступа к данным.'));
  const frag=document.createDocumentFragment();
  for(const x of list){
    const b=document.createElement('button');b.className='card';
    b.innerHTML='<div class="avatar"></div><div class="info"><div class="name"></div><div class="desc"></div><span class="badge"></span></div>';
    const a=b.querySelector('.avatar');
    if(x.photo_url){const im=document.createElement('img');im.src=x.photo_url;im.alt='';im.onerror=()=>{im.remove();a.textContent=initials(x.title)};a.append(im)}
    else a.textContent=initials(x.title);
    b.querySelector('.name').textContent=x.title||'Без названия';
    b.querySelector('.desc').textContent=x.description||'Описание пока недоступно';
    b.querySelector('.badge').textContent=x.badge||'';
    b.onclick=()=>x.kind==='chat'?openChat(x):showMember(x);
    frag.append(b);
  }
  grid.append(frag);
}
async function loadChats(){
  const id=++reqId;
  back.style.display='none';title.textContent='Сообщения';subtitle.textContent='Чаты, доступные вашему аккаунту Telegram';
  query.placeholder='Поиск по чатам…';query.value='';items=[];grid.innerHTML='';showState('Загружаю чаты…');
  try{
    const d=await apiRetry('/api/chats',id);
    if(id!==reqId)return;
    items=d.items||[];
    if(!items.length){
      showState(d.debug? `Диалогов найдено: ${d.debug.dialogs_found}; карточек: ${d.debug.items_returned}; аккаунт: ${d.debug.account_name||d.debug.account_id}. Если это не ваш аккаунт — проверьте TG_SESSION.`:'Список пуст.');
      return;
    }
    render();
  }catch(e){if(id===reqId)showState(e.message)}
}
async function openChat(chat){
  const id=++reqId;
  title.textContent=chat.title;subtitle.textContent='Участники чата';back.style.display='block';
  query.placeholder='Поиск по участникам…';query.value='';items=[];grid.innerHTML='';showState('Загружаю участников…');
  try{
    const d=await apiRetry('/api/chats/'+encodeURIComponent(chat.id)+'/members',id);
    if(id!==reqId)return;
    items=d.items||[];
    if(!items.length){showState(`Участники не получены. Загружено: ${d.total_loaded??0}. Telegram может ограничивать список участников.`);return}
    render();
  }catch(e){if(id===reqId)showState(e.message)}
}
function showMember(m){const box=document.createElement('div');box.style.cssText='position:fixed;inset:0;background:#0006;z-index:5;display:grid;place-items:center;padding:18px';const panel=document.createElement('section');panel.style.cssText='background:white;border-radius:14px;padding:20px;max-width:440px;width:100%;max-height:85vh;overflow:auto';const close=document.createElement('button');close.textContent='Закрыть';close.style.cssText='float:right;padding:7px 10px';close.onclick=()=>box.remove();const h=document.createElement('h2');h.textContent=m.title;const p=document.createElement('p');p.textContent=m.description||'Недостаточно данных для краткого описания.';const details=document.createElement('pre');details.style.cssText='white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.5 inherit';details.textContent='ID: '+m.id+'\nUsername: '+(m.username?'@'+m.username:'не указан')+'\nСообщений в базе: '+(m.message_count??'нет данных')+'\nПоследнее сообщение: '+(m.last_seen||'неизвестно')+'\n\nДанные основаны на доступном профиле Telegram и сохранённых сообщениях. Неизвестные сведения не предполагаются.';panel.append(close,h,p,details);box.append(panel);box.onclick=e=>{if(e.target===box)box.remove()};document.body.append(box)}
back.onclick=loadChats;query.oninput=render;loadChats();
</script>
</body></html>"""


def json_error(status: int, message: str, **extra) -> web.HTTPException:
    cls = {
        400: web.HTTPBadRequest, 401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
        429: web.HTTPTooManyRequests, 500: web.HTTPInternalServerError,
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
            last_text = " ".join(str(dialog.message.message).split())[:75]
        items.append({
            "id": dialog.id, "kind": "chat", "title": dialog.name or "Без названия",
            "description": kind + (" · " + last_text if last_text else ""),
            "badge": kind, "username": getattr(entity, "username", None), "photo_url": None,
        })
    print(f"Panel chats: account_id={me.id}, dialogs={len(dialogs)}, visible_items={len(items)}")
    return web.json_response({"items": items, "debug": {
        "account_id": me.id, "account_name": " ".join(filter(None, [me.first_name, me.last_name])),
        "dialogs_found": len(dialogs), "items_returned": len(items),
    }})


async def members(request):
    client, _me_obj = await authorize(request)
    try:
        chat_id = int(request.match_info["chat_id"])
    except ValueError:
        raise json_error(400, "Некорректный ID чата")
    try:
        entity = await resolve_entity(client, chat_id)
        stats_by_user = Storage(settings.database_path).get_chat_stats(chat_id)
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
            stats = stats_by_user.get(participant.id, {"message_count": 0, "last_seen": None})
            items.append({
                "id": participant.id, "kind": "member", "title": title, "username": username,
                "description": ("@" + username if username else "Username не указан") + " · сообщений в базе: " + str(stats["message_count"]),
                "message_count": stats["message_count"], "last_seen": stats["last_seen"],
                "badge": "Участник", "photo_url": None,
            })
        items.sort(key=lambda x: (-x["message_count"], x["title"].lower()))
        return web.json_response({"items": items, "total_loaded": count, "limit": 500})
    except (ChatAdminRequiredError, ChannelPrivateError) as exc:
        raise json_error(403, "Не удалось получить участников. Возможно, Telegram ограничивает доступ к списку участников этого чата.") from exc
    except ValueError as exc:
        raise json_error(400, "Чат не найден среди диалогов этого аккаунта.") from exc


async def health(request):
    return web.json_response({"status": "ok", "service": "watermelon"})


def create_app():
    app = web.Application(middlewares=[json_errors])
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/panel", index)
    app.router.add_get("/api/chats", chats)
    app.router.add_get("/api/chats/{chat_id}/members", members)
    return app
