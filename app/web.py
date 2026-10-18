"""Run the Telegram bot, personal-account client, and authenticated Web App."""
import asyncio
import os

from aiohttp import web

from .main import main as bot_main
from .panel import create_app
from .userbot import main as userbot_main


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
    await asyncio.gather(bot_main(), userbot_main(), run_http_server())


if __name__ == "__main__":
    asyncio.run(main())
