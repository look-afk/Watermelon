import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    llm_base_url: str = os.getenv(
        "LLM_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "gemini-2.5-flash")
    database_path: str = os.getenv("DATABASE_PATH", "messages.db")
    max_messages_per_analysis: int = int(os.getenv("MAX_MESSAGES_PER_ANALYSIS", "100"))

settings = Settings()

def validate_settings() -> None:
    missing = [name for name, value in {
        "BOT_TOKEN": settings.bot_token,
        "LLM_API_KEY": settings.llm_api_key,
    }.items() if not value or value == "replace_me"]
    if missing:
        raise RuntimeError("Заполните переменные окружения: " + ", ".join(missing))
