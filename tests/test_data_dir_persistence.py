"""Постоянное хранение данных: DATA_DIR, SQLite-файл и история выпадений."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import Base, RandomizerHistory
from app.db.session import (
    _adopt_legacy_sqlite,
    _ensure_sqlite_directory,
    session_scope,
)
from app.services import contest_service as cs
from app.services import draw_service as ds


def test_database_url_uses_data_dir(tmp_path):
    # DATABASE_URL="" перекрывает тестовую переменную окружения из conftest.
    settings = Settings(
        _env_file=None, BOT_TOKEN="1:a", DATA_DIR=str(tmp_path), DATABASE_URL=""
    )
    assert settings.effective_database_url == f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    assert settings.sqlite_db_path == tmp_path / "bot.db"


def test_database_url_custom_filename(tmp_path):
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="1:a",
        DATA_DIR=str(tmp_path),
        DB_FILENAME="randomizer.db",
        DATABASE_URL="",
    )
    assert settings.sqlite_db_path == tmp_path / "randomizer.db"


def test_explicit_database_url_wins():
    settings = Settings(
        _env_file=None,
        BOT_TOKEN="1:a",
        DATA_DIR="/tmp/ignored",
        DATABASE_URL="sqlite+aiosqlite:///:memory:",
    )
    assert settings.effective_database_url == "sqlite+aiosqlite:///:memory:"
    assert settings.sqlite_db_path is None


def test_sqlite_directory_is_created(tmp_path):
    db_file = tmp_path / "nested" / "dir" / "bot.db"
    _ensure_sqlite_directory(f"sqlite+aiosqlite:///{db_file}")
    assert db_file.parent.is_dir()


def test_legacy_database_is_adopted(tmp_path):
    """Старый data/bot_database.db переносится на новое имя, а не теряется."""
    legacy = tmp_path / "bot_database.db"
    legacy.write_bytes(b"legacy-db")
    target_url = f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}"
    _ensure_sqlite_directory(target_url)

    _adopt_legacy_sqlite(target_url)

    assert (tmp_path / "bot.db").read_bytes() == b"legacy-db"
    assert not legacy.exists()


@pytest.mark.asyncio
async def test_history_survives_engine_restart(tmp_path):
    """Строка истории, записанная с commit, читается новым engine (перезапуск)."""
    db_file = tmp_path / "data" / "bot.db"
    url = f"sqlite+aiosqlite:///{db_file}"
    _ensure_sqlite_directory(url)

    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        async with session.begin():
            session.add(
                RandomizerHistory(
                    user_id=42,
                    contest_id=1,
                    result="Победитель, место 1: Кубок",
                )
            )
    await engine.dispose()

    # Имитация перезапуска бота: новый engine на том же файле.
    engine2 = create_async_engine(url)
    factory2 = async_sessionmaker(engine2, expire_on_commit=False)
    async with factory2() as session:
        rows = list((await session.scalars(select(RandomizerHistory))).all())
    await engine2.dispose()

    assert len(rows) == 1
    assert rows[0].user_id == 42
    assert rows[0].result == "Победитель, место 1: Кубок"
    assert rows[0].created_at is not None


@pytest.mark.asyncio
async def test_draw_records_randomizer_history():
    bot = AsyncMock()
    async with session_scope() as session:
        contest = await cs.create_contest(
            session,
            title="HistoryDraw",
            status=cs.STATUS_ACTIVE,
            winners_count=2,
            prize="Кубок",
        )
        cid = contest.id
        for i in range(3):
            await cs.join_contest(
                session, bot, contest_id=cid, user_id=990000 + i, first_name=f"H{i}"
            )

    async with session_scope() as session:
        result = await ds.run_draw(session, contest_id=cid)

    async with session_scope() as session:
        rows = await ds.get_randomizer_history(session, contest_id=cid)
        assert len(rows) == 2
        assert {row.user_id for row in rows} == set(result.winners)
        assert all("Кубок" in row.result for row in rows)
        assert all(row.is_reserve is False for row in rows)
