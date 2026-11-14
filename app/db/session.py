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


def create_database_engine(custom_url: str | None = None) -> AsyncEngine:
    """Создаёт engine для асинхронной работы базы данных (PostgreSQL / SQLite)."""
    config = get_settings()
    database_url = _build_async_database_url(custom_url or config.database_url_value)

    if "sqlite" in database_url and ":memory:" not in database_url:
        db_file = database_url.split("///")[-1]
        os.makedirs(os.path.dirname(os.path.abspath(db_file)), exist_ok=True)

    if "postgresql" in database_url:
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

    return create_async_engine(
        database_url,
        echo=config.log_level == "DEBUG",
    )


engine: Final[AsyncEngine] = create_database_engine()
SessionFactory: Final[async_sessionmaker[AsyncSession]] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,
)


def _is_sqlite_backend() -> bool:
    """Проверяет, используется ли SQLite backend (в локальных тестах)."""
    return "sqlite" in str(engine.url)


async def _migrate_legacy_schema(conn) -> None:
    """Синхронизирует схему БД для поддержки обновленных моделей (удаление/релаксация NOT NULL для players.level и missing columns)."""
    # 1. PostgreSQL миграция
    try:
        await conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM information_schema.tables
                    WHERE table_name = 'players'
                ) THEN
                    -- Удаляем ограничение ck_players_level_positive если есть
                    BEGIN
                        ALTER TABLE players DROP CONSTRAINT IF EXISTS ck_players_level_positive;
                    EXCEPTION WHEN OTHERS THEN
                        NULL;
                    END;

                    -- Если колонка level есть в таблице players, снимаем NOT NULL и удаляем её
                    IF EXISTS (
                        SELECT 1
                        FROM information_schema.columns
                        WHERE table_name = 'players' AND column_name = 'level'
                    ) THEN
                        BEGIN
                            ALTER TABLE players ALTER COLUMN level DROP NOT NULL;
                            ALTER TABLE players ALTER COLUMN level SET DEFAULT 1;
                        EXCEPTION WHEN OTHERS THEN
                            NULL;
                        END;
                        BEGIN
                            ALTER TABLE players DROP COLUMN IF EXISTS level;
                        EXCEPTION WHEN OTHERS THEN
                            NULL;
                        END;
                    END IF;

                    -- Проверяем наличие всех актуальных колонок модели Player
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_name = 'players' AND column_name = 'shares'
                    ) THEN
                        BEGIN
                            ALTER TABLE players ADD COLUMN shares BIGINT NOT NULL DEFAULT 0;
                        EXCEPTION WHEN OTHERS THEN NULL;
                        END;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_name = 'players' AND column_name = 'rating'
                    ) THEN
                        BEGIN
                            ALTER TABLE players ADD COLUMN rating BIGINT NOT NULL DEFAULT 0;
                        EXCEPTION WHEN OTHERS THEN NULL;
                        END;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_name = 'players' AND column_name = 'custom_nickname'
                    ) THEN
                        BEGIN
                            ALTER TABLE players ADD COLUMN custom_nickname VARCHAR(64);
                        EXCEPTION WHEN OTHERS THEN NULL;
                        END;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_name = 'players' AND column_name = 'married_to'
                    ) THEN
                        BEGIN
                            ALTER TABLE players ADD COLUMN married_to BIGINT;
                        EXCEPTION WHEN OTHERS THEN NULL;
                        END;
                    END IF;

                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_name = 'players' AND column_name = 'referred_by'
                    ) THEN
                        BEGIN
                            ALTER TABLE players ADD COLUMN referred_by BIGINT;
                        EXCEPTION WHEN OTHERS THEN NULL;
                        END;
                    END IF;
                END IF;
            END $$;
        """))
    except Exception:
        # Для SQLite или иных СУБД (например, в тестовой среде)
        try:
            check_table = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='players'"))
            if check_table.scalar_one_or_none():
                cols_res = await conn.execute(text("PRAGMA table_info(players)"))
                cols = [row[1] for row in cols_res.fetchall()]
                if "level" in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players DROP COLUMN level"))
                    except Exception:
                        pass
                if "shares" not in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players ADD COLUMN shares BIGINT NOT NULL DEFAULT 0"))
                    except Exception:
                        pass
                if "rating" not in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players ADD COLUMN rating BIGINT NOT NULL DEFAULT 0"))
                    except Exception:
                        pass
                if "custom_nickname" not in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players ADD COLUMN custom_nickname VARCHAR(64)"))
                    except Exception:
                        pass
                if "married_to" not in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players ADD COLUMN married_to BIGINT"))
                    except Exception:
                        pass
                if "referred_by" not in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players ADD COLUMN referred_by BIGINT"))
                    except Exception:
                        pass
        except Exception:
            pass

    # 2. Дополнительные колонки Randomizer (пользовательские конкурсы каналов).
    try:
        check_contests = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='contests'")
        )
        if check_contests.scalar_one_or_none():
            cols_res = await conn.execute(text("PRAGMA table_info(contests)"))
            cols = [row[1] for row in cols_res.fetchall()]
            if "owner_id" not in cols:
                try:
                    await conn.execute(text("ALTER TABLE contests ADD COLUMN owner_id BIGINT"))
                except Exception:
                    pass
            if "source_channel_id" not in cols:
                try:
                    await conn.execute(text("ALTER TABLE contests ADD COLUMN source_channel_id BIGINT"))
                except Exception:
                    pass
        check_channels = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
        )
        if check_channels.scalar_one_or_none():
            cols_res = await conn.execute(text("PRAGMA table_info(channels)"))
            cols = [row[1] for row in cols_res.fetchall()]
            if "owner_id" not in cols:
                try:
                    await conn.execute(text("ALTER TABLE channels ADD COLUMN owner_id BIGINT"))
                except Exception:
                    pass
    except Exception:
        pass


async def init_database() -> None:
    """Инициализирует таблицы и схему базы данных при старте."""
    global _initialized_db
    if _initialized_db:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_legacy_schema(conn)

    # Если работает SQLite (тестовая среда), восстанавливаем сохраненные данные при наличии
    if _is_sqlite_backend():
        async with SessionFactory() as session:
            await json_db_manager.restore_to_session(session)
    _initialized_db = True


# Для обратной совместимости
init_json_database = init_database


async def get_session() -> AsyncIterator[AsyncSession]:
    """Dependency для обработчиков: гарантирует закрытие сессии."""
    await init_database()
    if _is_sqlite_backend():
        async with _db_transaction_lock:
            async with SessionFactory() as session:
                try:
                    yield session
                except BaseException:
                    await session.rollback()
                    raise
    else:
        async with SessionFactory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Контекст транзакции с автоматическим commit или rollback."""
    await init_database()
    if _is_sqlite_backend():
        async with _db_transaction_lock:
            async with SessionFactory() as session:
                try:
                    async with session.begin():
                        yield session
                    await json_db_manager.dump_from_session(session)
                except BaseException:
                    await session.rollback()
                    raise
    else:
        async with SessionFactory() as session:
            try:
                async with session.begin():
                    yield session
            except BaseException:
                await session.rollback()
                raise


async def check_database_connection() -> bool:
    """Проверяет работоспособность базы данных."""
    try:
        await init_database()
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return result.scalar_one() == 1
    except SQLAlchemyError:
        return False


async def dispose_engine() -> None:
    """Закрывает соединения пула."""
    if _is_sqlite_backend():
        async with SessionFactory() as session:
            await json_db_manager.dump_from_session(session)
    await engine.dispose()
