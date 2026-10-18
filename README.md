# Watermelon

Watermelon combines a Telegram userbot (Telethon, running as your Telegram account) with a separate BotFather bot (aiogram) and a Telegram Web App panel. The userbot silently imports text history of your chats in the background, stores new messages live, and the panel shows chats, members and messages. The bot can analyze stored messages with an LLM.

## Privacy and security

- Never publish `TG_SESSION`, `TG_API_HASH`, `LLM_API_KEY`, or `BOT_TOKEN`. The session string grants access to your Telegram account.
- Create the userbot session on your own computer and add it to Render only through **Environment**.
- The panel is available only to the account behind `TG_SESSION` (plus IDs in `PANEL_ALLOWED_USER_IDS`).
- Only text messages and text captions are stored; broadcast channels are not synced.
- Use this only with chats where you have permission to process the messages.
- Model output may be inaccurate and should not be treated as a diagnosis or definitive judgment about a person.

## 1. Telegram API credentials and session

1. Open https://my.telegram.org and sign in.
2. Open **API development tools** and get `api_id` and `api_hash`.
3. Install Python 3.12+ and dependencies locally, then create a session:

```bash
pip install -r requirements.txt
python -m app.create_session
```

Keep the generated session string private. Do not run the bot locally with the same session while it runs on Render.

## 2. Render Web Service

**Build Command**
```bash
pip install -r requirements.txt
```

**Start Command**
```bash
python -m app.web
```

## 3. Environment variables

- `BOT_TOKEN` — token of the BotFather bot.
- `TG_API_ID`, `TG_API_HASH`, `TG_SESSION` — userbot credentials.
- `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` — LLM endpoint (defaults to Gemini).
- `DATABASE_PATH` — defaults to `messages.db`.
- `MAX_MESSAGES_PER_ANALYSIS` — defaults to `100`.
- `AUTO_SYNC_DIALOGS` — most recent chats to sync, default `150`.
- `AUTO_SYNC_MESSAGES_PER_CHAT` — first-time history depth per chat, default `2000`.
- `AUTO_SYNC_INTERVAL_MIN` — how often missed messages are checked, default `30`.
- `PANEL_URL` — public HTTPS URL of `/panel` (optional on Render).
- `PANEL_ALLOWED_USER_IDS` — extra Telegram IDs allowed in the panel (optional).

## 4. Usage

1. Start the service. History of your chats is imported automatically in the background (a small indicator in the panel shows progress). New messages are stored live.
2. Send `/panel` to the bot and open the panel: chats with photos and last activity, members with message statistics, and a searchable message feed.
3. Optional analysis: `/analyze USER_ID` in the bot, or `.analyze USER_ID`, `.analyzeall USER_ID` from your account.

## 5. Health check and storage

- Health endpoint: `/health`.
- Render's local filesystem may not persist across redeploys; after a redeploy the history is simply re-imported automatically. For durable storage configure a persistent disk or an external database.
