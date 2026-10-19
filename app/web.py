"""Run the Telegram bot, personal-account client, and authenticated Web App."""
import asyncio
import logging
import os
import signal

from aiohttp import web

from . import backup
from .diagnostics import install_error_capture
from .config import settings
from .main import main as bot_main
from ..panel import create_app
from ..userbot import main as userbot_main


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


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    install_error_capture()  # feeds the "last errors" list of /diag
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. Windows
            pass

    tasks = [asyncio.create_task(c) for c in (bot_main(), userbot_main(), run_http_server())]
    stopper = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait([*tasks, stopper], return_when=asyncio.FIRST_COMPLETED)
        if stopper in done:
            # Render sent SIGTERM (redeploy / free-tier spin-down): save the database to Telegram first.
            print("Получен сигнал остановки — сохраняю базу в «Избранное»…")
            await backup.final_backup(settings.database_path)
        else:
            for task in done:
                task.result()  # re-raise the first failure, like gather() did
    finally:
        for task in (*tasks, stopper):
            task.cancel()
        await asyncio.gather(*tasks, stopper, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
