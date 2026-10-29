"""Run the Telegram bot, personal-account client, and authenticated Web App.

Every component is SUPERVISED: if one of them crashes or ends (e.g. Telethon loses the connection and
run_until_disconnected() returns), it is restarted with a back-off while the others keep running.
Previously ANY ending component stopped the whole process, Render restarted the container, the disk was
wiped and all sync progress was lost.
"""
import asyncio
import logging
import os
import signal
import time
import traceback
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

from . import backup
from .diagnostics import install_error_capture
from .config import settings
from .main import dp, main as bot_main
from .panel import create_app
from .userbot import main as userbot_main
from . import syncguard


async def run_http_server():
    runner = web.AppRunner(create_app())
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Watermelon panel listening on 0.0.0.0:{port}; GET /health")
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


async def supervise(name: str, factory, min_delay: float = 5.0, max_delay: float = 120.0):
    """Run factory() forever; restart it after a crash or a normal return."""
    delay = min_delay
    while True:
        started = time.monotonic()
        try:
            await factory()
            print(f"[{name}] завершился сам — перезапускаю через {delay:.0f} с (процесс продолжает работать)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"[{name}] упал: {type(exc).__name__}: {str(exc)[:300]} — перезапускаю через {delay:.0f} с")
            traceback.print_exc()
        if time.monotonic() - started > 300:
            delay = min_delay  # it ran healthily for a while: start the back-off over
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


async def keepalive_loop():
    """Render Free puts a service to sleep after 15 minutes without incoming HTTP requests, which kills the
    userbot mid-sync. A request to our OWN public URL counts as incoming traffic."""
    if os.getenv("KEEPALIVE", "1").strip().lower() in ("0", "false", "no", ""):
        await asyncio.Event().wait()
    origin = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    if not origin:
        panel = os.getenv("PANEL_URL", "").strip()
        if panel.startswith("https://"):
            origin = "https://" + urlparse(panel).netloc
    if not origin:
        print("Keep-alive: не найден RENDER_EXTERNAL_URL/PANEL_URL — пропускаю (настрой внешний пинг на /health).")
        await asyncio.Event().wait()
    print(f"Keep-alive включён: {origin}/health каждые 4 минуты")
    await asyncio.sleep(60)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
        while True:
            try:
                async with session.get(origin + "/health") as response:
                    await response.read()
            except Exception:
                pass  # a failed ping is harmless; try again next time
            await asyncio.sleep(240)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_error_capture()  # feeds the "last errors" list of /diag
    syncguard.install(dp)    # /sync: check what we have, continue the import, progress from the database
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass

    tasks = [
        asyncio.create_task(supervise("бот", bot_main)),
        asyncio.create_task(supervise("личный аккаунт", userbot_main)),
        asyncio.create_task(supervise("веб-панель", run_http_server)),
        asyncio.create_task(keepalive_loop()),
    ]
    try:
        await stop.wait()
        # Render sent SIGTERM (redeploy / restart): save the database to Telegram first.
        print("Получен сигнал остановки — сохраняю базу в «Избранное»…")
        await backup.final_backup(settings.database_path)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
