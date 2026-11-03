from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


def _build_async_database_url(raw_url: str) -> str:
    """Приводит PostgreSQL URL к драйверу asyncpg и проверяет его схему."""
    try:
        url: URL = make_url(raw_url)
    except Exception as exc:
        raise ValueError("Некорректный DATABASE_URL") from exc

    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+asyncpg")
    elif url.drivername != "postgresql+asyncpg":
        raise ValueError(
            "Для асинхронной работы требуется PostgreSQL URL "
            "с драйвером postgresql+asyncpg"
        )

    if not url.database:
        raise ValueError("В DATABASE_URL не указано имя базы данных")
    if not url.host:
        raise ValueError("В DATABASE_URL не указан адрес PostgreSQL")

    return url.render_as_string(hide_password=False)


def create_database_engine() -> AsyncEngine:
    """Создаёт engine из конфигурации приложения."""
    config = get_settings()
    database_url = _build_async_database_url(config.database_url_value)

    return create_async_engine(
        database_url,
        echo=config.log_level == "DEBUG",
        pool_pre_ping=True,
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_timeout=config.db_pool_timeout,
        pool_recycle=1800,
        connect_args={
            "server_settings": {
                "application_name": "fable_bot",
                "timezone": "UTC",
            }
        },
    )


# Engine и фабрика общие для процесса. Сессия при этом создаётся отдельно
# для каждого обновления/операции и никогда не разделяется между задачами.
engine: Final[AsyncEngine] = create_database_engine()
SessionFactory: Final[async_sessionmaker[AsyncSession]] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency для обработчиков: гарантирует закрытие сессии.

    Изменения фиксируются явно в сервисе через session.commit(). Если
    обработчик завершился с исключением, незавершённая транзакция откатится
    при закрытии сессии.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекст транзакции с автоматическим commit или rollback.

    Использование:
        async with session_scope() as session:
            session.add(...)
    """
    async with SessionFactory() as session:
        try:
            async with session.begin():
                yield session
        except BaseException:
            # session.begin() выполняет rollback самостоятельно; явный вызов
            # также безопасен и гарантирует чистое состояние при повторном
            # использовании сессии внутри внешнего кода.
            await session.rollback()
            raise


async def check_database_connection() -> bool:
    """Проверяет соединение коротким SELECT 1; ошибки SQLAlchemy не скрываются."""
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return result.scalar_one() == 1
    except SQLAlchemyError:
        return False


async def dispose_engine() -> None:
    """Закрывает все соединения пула при остановке приложения."""
    await engine.dispose()
