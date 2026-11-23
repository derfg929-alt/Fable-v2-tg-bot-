from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
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

logger = logging.getLogger("fable.db")

# Адаптируем первичные ключи моделей для совместимости с SQLite
for table in Base.metadata.sorted_tables:
    for col in table.columns:
        if col.name == "id" and col.primary_key:
            col.type = Integer().with_variant(Integer, "sqlite")
            col.autoincrement = True
        elif col.name in ("telegram_id", "chat_id") and col.primary_key:
            col.autoincrement = False


def _build_async_database_url(raw_url: str) -> str:
    """Определяет корректный асинхронный URL подключения."""
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raw_url = get_settings().effective_database_url

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

    return get_settings().effective_database_url


def _sqlite_file_path(database_url: str) -> Path | None:
    if "sqlite" not in database_url or ":memory:" in database_url:
        return None
    raw_path = database_url.split("///", 1)[-1].split("?", 1)[0]
    return Path(raw_path).expanduser() if raw_path else None


def _ensure_sqlite_directory(database_url: str) -> None:
    """Создаёт папку под файл SQLite (на Bothost — /app/data)."""
    path = _sqlite_file_path(database_url)
    if path is not None:
        path.resolve().parent.mkdir(parents=True, exist_ok=True)


def _adopt_legacy_sqlite(database_url: str) -> None:
    """Подхватывает старую базу bot_database.db, если новой ещё нет.

    До перехода на DATA_DIR путь по умолчанию был ``data/bot_database.db``.
    Без этого переноса бот после обновления стартовал бы с пустой базой.
    """
    path = _sqlite_file_path(database_url)
    if path is None or path.exists():
        return
    legacy = path.parent / "bot_database.db"
    if legacy.exists():
        legacy.replace(path)
        logger.warning("Старая база %s перенесена в %s", legacy, path)


_initialized_db = False
_db_transaction_lock = asyncio.Lock()


def create_database_engine(custom_url: str | None = None) -> AsyncEngine:
    """Создаёт engine для асинхронной работы базы данных (PostgreSQL / SQLite)."""
    config = get_settings()
    explicit_url = (custom_url or "").strip() or (
        config.database_url.get_secret_value().strip() if config.database_url else ""
    )
    database_url = _build_async_database_url(explicit_url or config.database_url_value)
    _ensure_sqlite_directory(database_url)
    if not explicit_url:
        _adopt_legacy_sqlite(database_url)

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

                    -- Опыт (XP) полностью убран из игры — удаляем устаревшую колонку.
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns WHERE table_name = 'players' AND column_name = 'experience'
                    ) THEN
                        BEGIN
                            ALTER TABLE players DROP COLUMN IF EXISTS experience;
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
                if "experience" in cols:
                    try:
                        await conn.execute(text("ALTER TABLE players DROP COLUMN experience"))
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

    # 3. Гибкие настройки конкурса, билеты, рефералы конкурса и прозрачность
    #    розыгрыша (SQLite-совместимая синхронизация существующих баз).
    async def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
        try:
            exists = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
                {"t": table},
            )
            if not exists.scalar_one_or_none():
                return
            cols_res = await conn.execute(text(f"PRAGMA table_info({table})"))
            cols = [row[1] for row in cols_res.fetchall()]
            if column not in cols:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        except Exception:
            pass

    await _add_column_if_missing("contests", "settings_json", "settings_json JSON")
    await _add_column_if_missing("entries", "tickets", "tickets INTEGER NOT NULL DEFAULT 1")
    await _add_column_if_missing("entries", "conditions_met", "conditions_met BOOLEAN NOT NULL DEFAULT 0")
    await _add_column_if_missing("entries", "conditions_met_at", "conditions_met_at DATETIME")
    await _add_column_if_missing("winners", "is_reserve", "is_reserve BOOLEAN NOT NULL DEFAULT 0")
    await _add_column_if_missing("winners", "status", "status VARCHAR(16) NOT NULL DEFAULT 'pending'")
    await _add_column_if_missing("winners", "notified_at", "notified_at DATETIME")
    await _add_column_if_missing("draw_logs", "salt", "salt VARCHAR(64)")
    await _add_column_if_missing("draw_logs", "participants_hash", "participants_hash VARCHAR(64)")
    await _add_column_if_missing("activity_records", "message_id", "message_id BIGINT")

    # create_all не создаёт индексы для уже существующих таблиц, поэтому на
    # обновлённой базе индекс дедупликации сообщений нужно доставить отдельно.
    async def _create_index_if_missing(index: str, table: str, columns: str) -> None:
        try:
            exists = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND name=:i"),
                {"i": index},
            )
            if exists.scalar_one_or_none():
                return
            await conn.execute(text(f"CREATE INDEX IF NOT EXISTS {index} ON {table} ({columns})"))
        except Exception:
            pass

    await _create_index_if_missing(
        "ix_activity_chat_user_message", "activity_records", "chat_id, user_id, message_id"
    )


async def init_database() -> None:
    """Инициализирует таблицы и схему базы данных при старте."""
    global _initialized_db
    if _initialized_db:
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_legacy_schema(conn)
    _initialized_db = True
    logger.info(
        "База данных готова: %s",
        engine.url.render_as_string(hide_password=True),
    )


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
    await engine.dispose()
