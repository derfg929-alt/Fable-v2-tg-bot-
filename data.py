"""JSON-хранилище для Telegram-бота на aiogram 3.

Вся база — один файл ``/app/data/database.json``. Запись атомарная: данные
сначала пишутся во временный файл ``database.json.tmp``, затем переименовываются
в основной через :func:`os.replace`. Все операции чтения/записи выполняются под
глобальным :class:`asyncio.Lock`, чтобы одновременные клики не затирали данные.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
DB_PATH = str(DATA_DIR / "database.json")

# Глобальная блокировка на все операции чтения/записи.
_lock = asyncio.Lock()


def _default_data() -> dict[str, Any]:
    return {"users": {}, "contests": {}, "admin_state": {}}


async def load_data() -> dict[str, Any]:
    """Прочитать базу. Если файла нет — создать с пустой структурой."""
    async with _lock:
        if not os.path.exists(DB_PATH):
            data = _default_data()
            _write(DB_PATH, data)
            return data
        with open(DB_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)


async def save_data(data: dict[str, Any]) -> None:
    """Атомарно сохранить базу."""
    async with _lock:
        _write(DB_PATH, data)


def _write(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


async def create_contest(contest: dict[str, Any]) -> dict[str, Any]:
    """Сохранить конкурс в JSON-базе.

    Если у конкурса нет ``id``, присваивается следующий свободный числовой id.
    Возвращает сохранённую запись конкурса.
    """
    async with _lock:
        db = _load_unlocked()
        contests = db["contests"]
        if not contest.get("id"):
            contest = {**contest, "id": _next_contest_id(contests)}
        contests[str(contest["id"])] = contest
        _write(DB_PATH, db)
        return contest


def _next_contest_id(contests: dict[str, Any]) -> str:
    """Следующий свободный числовой id конкурса."""
    max_id = 0
    for key in contests:
        try:
            max_id = max(max_id, int(key))
        except (TypeError, ValueError):
            continue
    return str(max_id + 1)


async def get_user(user_id: int) -> dict[str, Any]:
    """Вернуть пользователя, создав его при необходимости."""
    async with _lock:
        data = _load_unlocked()
        users = data["users"]
        uid = str(user_id)
        if uid not in users:
            users[uid] = {"tickets": 0, "referrals": []}
            _write(DB_PATH, data)
        return users[uid]


async def update_user(user_id: int, key: str, value: Any) -> dict[str, Any]:
    """Обновить поле пользователя (создаёт пользователя, если его нет)."""
    async with _lock:
        data = _load_unlocked()
        users = data["users"]
        uid = str(user_id)
        user = users.setdefault(uid, {"tickets": 0, "referrals": []})
        user[key] = value
        _write(DB_PATH, data)
        return user


def _load_unlocked() -> dict[str, Any]:
    """Прочитать базу без блокировки (вызывать только внутри ``_lock``)."""
    if not os.path.exists(DB_PATH):
        return _default_data()
    with open(DB_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)
