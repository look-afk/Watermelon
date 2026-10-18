"""HTTP health endpoint plus the existing Telegram bot and userbot processes.

Render Web Services must bind to 0.0.0.0:$PORT. The health endpoint is deliberately
small; Telegram behavior remains in app.main and app.userbot.
"""
import asyncio
import os

from .main import main as bot_main
from .userbot import main as userbot_main


async def handle_health(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        request_line = await asyncio.wait_for(reader.readline(), timeout=5)
        parts = request_line.decode("latin-1", errors="replace").strip().split()
        path = parts[1] if len(parts) >= 2 else "/"
        # Consume request headers so the connection is handled cleanly.
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5)
            if line in (b"\r\n", b"\n", b""):
                break

        if path == "/health":
            status = "200 OK"
            body = b'{"status":"ok","service":"watermelon"}'
            content_type = "application/json; charset=utf-8"
        else:
            status = "200 OK"
            body = b"Watermelon is running. Health check: /health\n"
            content_type = "text/plain; charset=utf-8"

        response = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        writer.write(response)
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, BrokenPipeError):
            pass


async def main() -> None:
    port = int(os.getenv("PORT", "10000"))
    server = await asyncio.start_server(handle_health, "0.0.0.0", port)
    print(f"Watermelon health server listening on 0.0.0.0:{port}; GET /health")

    async with server:
        # Run both existing Telegram components so neither loses functionality.
        await asyncio.gather(bot_main(), userbot_main())


if __name__ == "__main__":
    asyncio.run(main())
