"""JSON-хранилище базы данных Fable.

Полный отказ от внешнего SQL сервера:
Все таблицы сериализуются и хранятся в JSON файле (data/bot_database.json).
При запуске данные загружаются из JSON в базу aiosqlite (in-memory или file),
при транзакциях и изменениях автоматически синхронизируются обратно в JSON.
"""
from __future__ import annotations

import asyncio
import json
import os
import logging
from datetime import datetime, date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, delete, insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Base

logger = logging.getLogger("fable.json_db")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _default_json_path() -> str:
    """Путь к файлу JSON-базы. Переопределяется переменной JSON_DATABASE_PATH.

    Значение "-" или пустая строка полностью отключают JSON-синхронизацию.
    """
    raw = os.environ.get("JSON_DATABASE_PATH")
    if raw is None:
        raw = os.path.join("data", "bot_database.json")
    raw = raw.strip()
    if raw in ("", "-"):
        return ""
    if not os.path.isabs(raw):
        raw = os.path.join(_REPO_ROOT, raw)
    return raw


DEFAULT_JSON_PATH = _default_json_path()

def _json_serial(obj: Any) -> Any:
    """JSON serializer for objects not serializable by default json code"""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="ignore")
    raise TypeError(f"Type {type(obj)} not serializable")


class JsonDatabaseManager:
    def __init__(self, json_path: str = DEFAULT_JSON_PATH):
        self.json_path = json_path
        self.enabled = bool(json_path)
        self._lock = asyncio.Lock()
        if self.enabled:
            os.makedirs(os.path.dirname(self.json_path), exist_ok=True)

    def load_json(self) -> dict[str, list[dict[str, Any]]]:
        if not self.enabled or not os.path.exists(self.json_path):
            return {}
        try:
            with open(self.json_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except Exception as e:
            logger.error(f"Ошибка чтения базы JSON ({self.json_path}): {e}")
            return {}

    def save_json(self, data: dict[str, list[dict[str, Any]]]) -> None:
        if not self.enabled:
            return
        try:
            tmp_path = self.json_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=_json_serial)
            os.replace(tmp_path, self.json_path)
        except Exception as e:
            logger.error(f"Ошибка записи базы JSON ({self.json_path}): {e}")

    async def restore_to_session(self, session: AsyncSession) -> None:
        """Загружает все данные из JSON-файла в текущую активную базу SQLite."""
        if not self.enabled:
            return
        async with self._lock:
            data = self.load_json()
            if not data:
                return

            for table in Base.metadata.sorted_tables:
                table_name = table.name
                rows = data.get(table_name, [])
                if not rows:
                    continue

                for row in rows:
                    row_data = dict(row)
                    # Convert isoformat datetimes and Decimal if needed
                    for col in table.columns:
                        col_name = col.name
                        if col_name in row_data and row_data[col_name] is not None:
                            val = row_data[col_name]
                            if str(col.type).startswith("DATETIME") or str(col.type).startswith("TIMESTAMP"):
                                if isinstance(val, str):
                                    try:
                                        row_data[col_name] = datetime.fromisoformat(val)
                                    except Exception:
                                        pass
                            elif str(col.type).startswith("NUMERIC") or str(col.type).startswith("DECIMAL"):
                                if isinstance(val, (str, float, int)):
                                    try:
                                        row_data[col_name] = Decimal(str(val))
                                    except Exception:
                                        pass
                    try:
                        await session.execute(table.insert().values(**row_data))
                    except Exception as err:
                        logger.debug("Не удалось восстановить строку таблицы %s: %s", table_name, err)
            await session.commit()

    async def dump_from_session(self, session: AsyncSession) -> None:
        """Выгружает все таблицы из базы данных SQLite в JSON-файл."""
        if not self.enabled:
            return
        async with self._lock:
            tables_data = {}
            for table_name, table in Base.metadata.tables.items():
                try:
                    res = await session.execute(select(table))
                    rows = [dict(r._mapping) for r in res.fetchall()]
                    # Convert values to JSON-friendly
                    for row in rows:
                        for k, v in row.items():
                            if isinstance(v, (datetime, date)):
                                row[k] = v.isoformat()
                            elif isinstance(v, Decimal):
                                row[k] = str(v)
                    tables_data[table_name] = rows
                except Exception as e:
                    logger.debug(f"Ошибка выгрузки таблицы {table_name}: {e}")
            self.save_json(tables_data)

    async def clear_database(self, session: AsyncSession) -> None:
        """Очищает данные (используется для тестов)."""
        async with self._lock:
            for table in reversed(Base.metadata.sorted_tables):
                try:
                    await session.execute(table.delete())
                except Exception:
                    pass
            await session.commit()
            self.save_json({})


json_db_manager = JsonDatabaseManager()
