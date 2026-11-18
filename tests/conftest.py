import os
import pytest

# Для тестов в песочнице изолируем БД в памяти SQLite, чтобы тесты не трогали
# реальную базу data/bot_database.db.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from app.db.session import session_scope
from app.db.models import Base

@pytest.fixture(autouse=True)
async def cleanup_db():
    yield
    async with session_scope() as session:
        # Очищаем данные между тестами в SQLite.
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())
