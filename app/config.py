from functools import lru_cache
from typing import Literal

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

    # Настройки ИИ (OpenAI-совместимый API / OpenRouter / Groq / etc.)
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    ai_base_url: str = Field(default="https://api.openai.com/v1", alias="AI_BASE_URL")
    ai_model: str = Field(default="gpt-4o-mini", alias="AI_MODEL")

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

    @property
    def bot_token_value(self) -> str:
        """Токен в открытом виде только для передачи Telegram-клиенту."""
        return self.bot_token.get_secret_value()

    @property
    def database_url_value(self) -> str:
        """URL подключения только для SQLAlchemy."""
        return self.database_url.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Единый экземпляр настроек на весь процесс."""
    return Settings()


settings = get_settings()
