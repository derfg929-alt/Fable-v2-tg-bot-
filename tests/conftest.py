import pytest
import asyncio
from app.db.session import session_scope, engine
from app.db.json_db import json_db_manager

@pytest.fixture(autouse=True)
async def cleanup_db():
    yield
    async with session_scope() as session:
        # Очищаем данные между тестами в SQLite и JSON
        await json_db_manager.clear_database(session)
