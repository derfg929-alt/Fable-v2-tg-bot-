import os
import pytest
import asyncio

# Для тестов в песочнице изолируем БД в памяти SQLite
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.db.session import session_scope, engine
from app.db.json_db import json_db_manager

@pytest.fixture(autouse=True)
async def cleanup_db():
    yield
    async with session_scope() as session:
        # Очищаем данные между тестами в SQLite и JSON
        await json_db_manager.clear_database(session)
