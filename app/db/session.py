from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

from sqlalchemy import text, Integer
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.models import Base
from app.db.json_db import json_db_manager

# Адаптируем первичные ключи моделей для совместимости с SQLite / JSON
for table in Base.metadata.sorted_tables:
    for col in table.columns:
        if col.name == "id" and col.primary_key:
            col.type = Integer().with_variant(Integer, "sqlite")
            col.autoincrement = True
        elif col.name in ("telegram_id", "chat_id") and col.primary_key:
            col.autoincrement = False


def _build_async_database_url(raw_url: str) -> str:
    """Определяет корректный асинхронный URL подключения."""
    if not raw_url:
        return "sqlite+aiosqlite:///data/bot_database.db"

    # Если указан sqlite файл или память
    if "sqlite" in raw_url:
        if not raw_url.startswith("sqlite+aiosqlite://"):
            return raw_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return raw_url

    # Если указан postgresql
    if "postgresql" in raw_url:
        if raw_url.startswith("postgresql://"):
            return raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return raw_url

    return "sqlite+aiosqlite:///data/bot_database.db"


_initialized_db = False
_db_transaction_lock = asyncio.Lock()


def create_database_engine() -> AsyncEngine:
    """Создаёт engine для асинхронной работы базы данных."""
    config = get_settings()
    database_url = _build_async_database_url(config.database_url_value)

    if "sqlite" in database_url and ":memory:" not in database_url:
        db_file = database_url.split("///")[-1]
        os.makedirs(os.path.dirname(os.path.abspath(db_file)), exist_ok=True)

    connect_args = {}
    if "postgresql" in database_url:
        connect_args = {
            "server_settings": {
                "application_name": "fable_bot",
                "timezone": "UTC",
            }
        }

    return create_async_engine(
        database_url,
        echo=config.log_level == "DEBUG",
        connect_args=connect_args,
    )


engine: Final[AsyncEngine] = create_database_engine()
SessionFactory: Final[async_sessionmaker[AsyncSession]] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


async def init_json_database() -> None:
    """Инициализирует таблицы и загружает сохраненные данные из JSON при старте."""
    global _initialized_db
    if _initialized_db:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Загружаем данные из JSON в SQLite
    async with SessionFactory() as session:
        await json_db_manager.restore_to_session(session)
    _initialized_db = True


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency для обработчиков: гарантирует закрытие сессии."""
    await init_json_database()
    async with _db_transaction_lock:
        async with SessionFactory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекст транзакции с автоматическим commit или rollback и синхронизацией с JSON."""
    await init_json_database()
    async with _db_transaction_lock:
        async with SessionFactory() as session:
            try:
                async with session.begin():
                    yield session
                # После успешного завершения транзакции сбрасываем состояние в JSON-файл
                await json_db_manager.dump_from_session(session)
            except BaseException:
                await session.rollback()
                raise


async def check_database_connection() -> bool:
    """Проверяет работоспособность базы данных."""
    try:
        await init_json_database()
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return result.scalar_one() == 1
    except SQLAlchemyError:
        return False


async def dispose_engine() -> None:
    """Закрывает соединения пула и сохраняет состояние в JSON."""
    async with SessionFactory() as session:
        await json_db_manager.dump_from_session(session)
    await engine.dispose()
