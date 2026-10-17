import asyncio
import os
from getpass import getpass
from telethon import TelegramClient
from telethon.sessions import StringSession

async def main():
    api_id_text = os.getenv("TG_API_ID") or input("TG_API_ID: ").strip()
    api_hash = os.getenv("TG_API_HASH") or input("TG_API_HASH: ").strip()
    if not api_id_text.isdigit() or not api_hash:
        raise SystemExit("Укажи корректные TG_API_ID и TG_API_HASH из https://my.telegram.org")
    client = TelegramClient(StringSession(), int(api_id_text), api_hash)
    await client.start(
        phone=lambda: input("Номер телефона Telegram с кодом страны: ").strip(),
        code_callback=lambda: input("Код из Telegram: ").strip(),
        password=lambda: getpass("Пароль двухэтапной аутентификации (если включён): "),
    )
    me = await client.get_me()
    print("\nАвторизация успешна.")
    print(f"Аккаунт ID: {me.id}")
    print("\nСкопируй строку ниже в Render как TG_SESSION. Не публикуй её и не отправляй другим людям.\n")
    print(client.session.save())
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
