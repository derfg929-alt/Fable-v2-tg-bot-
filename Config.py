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

    bot_token: SecretStr = Field(alias="BOT_TOKEN")
    owner_id: int = Field(default=8039111975, alias="OWNER_ID", gt=0)
    database_url: SecretStr = Field(
        default=SecretStr("postgresql+asyncpg://fable:fable@localhost:5432/fable"),
        alias="DATABASE_URL",
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
    grok_model: str = Field(default="grok-beta", alias="GROK_MODEL")

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
        if not cleaned:
            return None
        return SecretStr(cleaned)

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
        """Возвращает рабочую модель (openai/gpt-oss-120b для Groq, grok-beta для xAI)."""
        if self.groq_api_key and self.groq_api_key.get_secret_value().strip().strip("\"'").strip():
            return self.groq_model
        return self.grok_model


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единый экземпляр настроек на весь процесс."""
    return Settings()


settings = get_settings()
