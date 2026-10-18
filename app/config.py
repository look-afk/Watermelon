import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    database_path: str = os.getenv("DATABASE_PATH", "messages.db")
    max_messages_per_analysis: int = int(os.getenv("MAX_MESSAGES_PER_ANALYSIS", "100"))
    # Automatic background history sync (no commands needed).
    auto_sync_dialogs: int = int(os.getenv("AUTO_SYNC_DIALOGS", "150"))
    auto_sync_messages_per_chat: int = int(
        os.getenv("AUTO_SYNC_MESSAGES_PER_CHAT", os.getenv("MAX_MESSAGES_PER_SYNC", "2000"))
    )
    auto_sync_interval_min: int = int(os.getenv("AUTO_SYNC_INTERVAL_MIN", "30"))
    # Automatic people profiles (LLM). Needs LLM_API_KEY.
    profile_auto: bool = os.getenv("PROFILE_AUTO", "1").strip().lower() not in ("0", "false", "no", "")
    profile_min_messages: int = int(os.getenv("PROFILE_MIN_MESSAGES", "15"))
    profile_refresh_messages: int = int(os.getenv("PROFILE_REFRESH_MESSAGES", "25"))
    profile_max_per_cycle: int = int(os.getenv("PROFILE_MAX_PER_CYCLE", "6"))
    profile_interval_min: int = int(os.getenv("PROFILE_INTERVAL_MIN", "10"))
    # Keep the database alive on hosts without a persistent disk (e.g. Render free):
    # snapshots are stored in your Telegram "Saved Messages" and restored on start.
    backup_enabled: bool = os.getenv("BACKUP_TO_TELEGRAM", "1").strip().lower() not in ("0", "false", "no", "")
    backup_interval_min: int = int(os.getenv("BACKUP_INTERVAL_MIN", "15"))
    # Extra Telegram user IDs (comma separated) allowed to open the panel.
    panel_allowed_user_ids: str = os.getenv("PANEL_ALLOWED_USER_IDS", "")


settings = Settings()


def validate_settings() -> None:
    missing = [
        name
        for name, value in {
            "BOT_TOKEN": settings.bot_token,
            "LLM_API_KEY": settings.llm_api_key,
        }.items()
        if not value or value == "replace_me"
    ]
    if missing:
        raise RuntimeError("Заполните переменные окружения: " + ", ".join(missing))
