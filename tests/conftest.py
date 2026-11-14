import os
import tempfile
import pytest
import asyncio

# Для тестов в песочнице изолируем БД в памяти SQLite, а JSON-файл уводим во
# временную папку, чтобы тесты не трогали реальную базу data/bot_database.json.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault(
    "JSON_DATABASE_PATH", os.path.join(tempfile.gettempdir(), "fable_test_db.json")
)

from app.db.session import session_scope, engine
from app.db.json_db import json_db_manager

@pytest.fixture(autouse=True)
async def cleanup_db():
    yield
    async with session_scope() as session:
        # Очищаем данные между тестами в SQLite и JSON
        await json_db_manager.clear_database(session)
