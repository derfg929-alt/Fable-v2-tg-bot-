from functools import lru_cache
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    bot_token: SecretStr = Field(default=SecretStr("123456:dummy_token_default"), alias="BOT_TOKEN")
    owner_id: int = Field(default=8039111975, alias="OWNER_ID", gt=0)
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://fable:fable@localhost:5432/fable"),
        alias="DATABASE_URL",
    )
    json_database_path: str = Field(
        default="data/bot_database.json",
        alias="JSON_DATABASE_PATH",
    )
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", alias="LOG_LEVEL"
    )

    # Настройки подключения к PostgreSQL.
    db_pool_size: int = Field(default=10, alias="DB_POOL_SIZE", ge=1, le=50)
    db_max_overflow: int = Field(default=20, alias="DB_MAX_OVERFLOW", ge=0, le=100)
    db_pool_timeout: int = Field(default=30, alias="DB_POOL_TIMEOUT", ge=1, le=300)

    # Время используется для суточных лимитов, банка и игровых кулдаунов.
    timezone: str = Field(default="Europe/Moscow", alias="TIMEZONE")

    # Ограничения Telegram API и обработки обновлений.
    polling_timeout: int = Field(default=30, alias="POLLING_TIMEOUT", ge=5, le=60)
    update_concurrency: int = Field(default=32, alias="UPDATE_CONCURRENCY", ge=1, le=256)

    # Настройки интеграции с AI: Grok (xAI) и Groq Cloud API.
    grok_api_key: SecretStr | None = Field(default=None, alias="GROK_API_KEY")
    grok_api_url: str = Field(default="https://api.x.ai/v1/chat/completions", alias="GROK_API_URL")
    grok_model: str = Field(default="grok-4.7", alias="GROK_MODEL")

    groq_api_key: SecretStr | None = Field(default=None, alias="GROQ_API_KEY")
    groq_api_url: str = Field(default="https://api.groq.com/openai/v1/chat/completions", alias="GROQ_API_URL")
    groq_model: str = Field(default="openai/gpt-oss-120b", alias="GROQ_MODEL")

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token or ":" not in token:
            raise ValueError("BOT_TOKEN не задан или имеет неверный формат")
        return SecretStr(token)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value().strip()
        if not url.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError(
                "DATABASE_URL должен быть PostgreSQL URL "
                "(например, postgresql+asyncpg://user:password@localhost:5432/fable)"
            )
        return SecretStr(url)

    @field_validator("grok_api_key", "groq_api_key", mode="before")
    @classmethod
    def validate_ai_api_keys(cls, value: Any) -> Any:
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        cleaned = raw.strip().strip("\"'").strip()
        # Удаляем случайно добавленный префикс 'Bearer ' если ключ скопирован из заголовков
        if cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip().strip("\"'").strip()
        if not cleaned:
            return None
        return SecretStr(cleaned)

    @field_validator("grok_model", mode="before")
    @classmethod
    def validate_grok_model(cls, value: Any) -> str:
        """Автоматическая замена устаревших моделей xAI Grok на актуальные."""
        default_model = "grok-4.7"
        if not value:
            return default_model
        model_str = str(value).strip().strip("\"'").strip()
        if not model_str:
            return default_model

        deprecated_grok_models = {
            "grok-beta",
            "grok-vision-beta",
            "grok-2",
            "grok-2-1212",
            "grok-2-vision-1212",
            "grok-3",
            "grok-4-0709",
            "grok-code-fast-1",
            "grok-4-fast-reasoning",
            "grok-4-fast-non-reasoning",
            "grok-4-1-fast-reasoning",
            "grok-4-1-fast-non-reasoning",
        }
        if model_str.lower() in deprecated_grok_models:
            return default_model
        return model_str

    @field_validator("groq_model", mode="before")
    @classmethod
    def validate_groq_model(cls, value: Any) -> str:
        """Автоматическая замена устаревших/удаленных моделей Groq на актуальные."""
        default_model = "openai/gpt-oss-120b"
        if not value:
            return default_model
        model_str = str(value).strip().strip("\"'").strip()
        if not model_str:
            return default_model

        deprecated_groq_models = {
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
            "gemma2-9b-it",
            "llama3-70b-8192",
            "llama3-8b-8192",
            "llama-3.2-1b-preview",
            "llama-3.2-3b-preview",
            "llama-3.2-11b-vision-preview",
            "llama-3.2-90b-vision-preview",
            "llama-guard-3-8b",
        }
        if model_str.lower() in deprecated_groq_models:
            return default_model
        return model_str

    @property
    def bot_token_value(self) -> str:
        """Токен в открытом виде только для передачи Telegram-клиенту."""
        return self.bot_token.get_secret_value()

    @property
    def database_url_value(self) -> str:
        """URL подключения только для SQLAlchemy."""
        return self.database_url.get_secret_value()

    @property
    def grok_api_key_value(self) -> str | None:
        """Ключ API Grok в открытом виде для передачи в HTTP-клиент."""
        return self.effective_ai_api_key

    @property
    def groq_api_key_value(self) -> str | None:
        """Ключ API Groq в открытом виде для передачи в HTTP-клиент."""
        if not self.groq_api_key:
            return None
        val = self.groq_api_key.get_secret_value().strip().strip("\"'").strip()
        return val or None

    @property
    def effective_ai_api_key(self) -> str | None:
        """Возвращает активный API-ключ (Groq API либо Grok xAI)."""
        if self.groq_api_key:
            val = self.groq_api_key.get_secret_value().strip().strip("\"'").strip()
            if val:
                return val
        if self.grok_api_key:
            val = self.grok_api_key.get_secret_value().strip().strip("\"'").strip()
            if val:
                return val
        return None

    @property
    def effective_ai_api_url(self) -> str:
        """Возвращает подходящий OpenAI-совместимый URL (Groq или xAI Grok)."""
        if self.groq_api_key and self.groq_api_key.get_secret_value().strip().strip("\"'").strip():
            return self.groq_api_url
        return self.grok_api_url

    @property
    def effective_ai_model(self) -> str:
        """Возвращает рабочую модель (openai/gpt-oss-120b для Groq, grok-4.7 для xAI)."""
        if self.groq_api_key and self.groq_api_key.get_secret_value().strip().strip("\"'").strip():
            return self.groq_model
        return self.grok_model


import os

_LAST_ENV_MTIME: float | None = None


@lru_cache(maxsize=1)
def _get_cached_settings() -> Settings:
    return Settings()


def get_settings(*, reload: bool = False) -> Settings:
    """Возвращает настройки с авто-инвалидацией кэша при обновлении .env или переменных окружения."""
    global _LAST_ENV_MTIME

    env_mtime: float | None = None
    if os.path.exists(".env"):
        try:
            env_mtime = os.path.getmtime(".env")
        except OSError:
            pass

    if reload or (_LAST_ENV_MTIME is not None and env_mtime != _LAST_ENV_MTIME):
        _get_cached_settings.cache_clear()

    if _LAST_ENV_MTIME is None:
        _LAST_ENV_MTIME = env_mtime
    elif env_mtime != _LAST_ENV_MTIME:
        _LAST_ENV_MTIME = env_mtime

    inst = _get_cached_settings()

    if not inst.effective_ai_api_key:
        env_groq = os.environ.get("GROQ_API_KEY", "").strip()
        env_grok = os.environ.get("GROK_API_KEY", "").strip()
        if env_groq or env_grok:
            _get_cached_settings.cache_clear()
            inst = _get_cached_settings()

    return inst


get_settings.cache_clear = _get_cached_settings.cache_clear  # type: ignore[attr-defined]

settings = get_settings()
