"""JSON-хранилище бота розыгрышей.

Вся база — один файл (по умолчанию /app/data/database.json). Запись атомарная:
сначала пишем во временный файл, затем os.replace. Все операции
«прочитать → изменить → записать» выполняются под глобальным asyncio.Lock,
иначе при одновременных кликах участник может потеряться.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from . import config

# Глобальная блокировка на все операции чтения-изменения-записи.
_LOCK = asyncio.Lock()

# Кэш топ-приглашающих: contest_id -> (monotonic_ts, result)
_LEADERBOARD_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_LEADERBOARD_TTL = 60.0


def _template() -> dict[str, Any]:
    return {
        "users": {},
        "contests": {},
        "admin_state": {},
        "code_word_attempts": {},
        "pending_referrals": {},
    }


def db_path() -> Path:
    return Path(config.DB_PATH)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _read() -> dict[str, Any]:
    path = db_path()
    if not path.exists():
        data = _template()
        _atomic_write(path, data)
        return data
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        # Повреждённый файл не должен валить бота: начинаем с пустого шаблона.
        data = _template()
    if not isinstance(data, dict):
        data = _template()
    base = _template()
    base.update(data)
    for key, default in _template().items():
        if not isinstance(base.get(key), type(default)):
            base[key] = default
    return base


async def load_data() -> dict[str, Any]:
    """Прочитать базу (создаёт файл из шаблона, если его нет)."""
    async with _LOCK:
        return _read()


async def save_data(data: dict[str, Any]) -> None:
    """Атомарно записать базу."""
    async with _LOCK:
        _atomic_write(db_path(), data)


async def _mutate(fn):
    """Выполнить fn(data) под блокировкой и сохранить результат."""
    async with _LOCK:
        data = _read()
        result = fn(data)
        _atomic_write(db_path(), data)
        return result


# --- Пользователи ---------------------------------------------------------

def _ensure_user(data: dict[str, Any], user_id: int) -> dict[str, Any]:
    uid = str(user_id)
    user = data["users"].get(uid)
    if user is None:
        user = {
            "username": None,
            "tickets": 0,
            "referrals": [],
            "contests_participated": [],
            "contest_tickets": {},
        }
        data["users"][uid] = user
    user.setdefault("username", None)
    user.setdefault("tickets", 0)
    user.setdefault("referrals", [])
    user.setdefault("contests_participated", [])
    user.setdefault("contest_tickets", {})
    return user


async def get_user(user_id: int, username: str | None = None) -> dict[str, Any]:
    """Вернуть пользователя, создав его при необходимости."""
    def op(data: dict[str, Any]) -> dict[str, Any]:
        user = _ensure_user(data, user_id)
        if username and user.get("username") != username:
            user["username"] = username
        return copy.deepcopy(user)

    return await _mutate(op)


async def update_user(user_id: int, **kwargs: Any) -> dict[str, Any]:
    def op(data: dict[str, Any]) -> dict[str, Any]:
        user = _ensure_user(data, user_id)
        user.update(kwargs)
        return copy.deepcopy(user)

    return await _mutate(op)


async def add_tickets(user_id: int, amount: int) -> int:
    """Начислить билеты пользователю. Возвращает новый баланс."""
    def op(data: dict[str, Any]) -> int:
        user = _ensure_user(data, user_id)
        user["tickets"] = int(user.get("tickets", 0)) + int(amount)
        return user["tickets"]

    return await _mutate(op)


async def add_contest_tickets(user_id: int, contest_id: str, amount: int) -> int:
    """Начислить билеты за конкретный конкурс (влияет на пул жеребьёвки)."""
    def op(data: dict[str, Any]) -> int:
        user = _ensure_user(data, user_id)
        user["tickets"] = int(user.get("tickets", 0)) + int(amount)
        per_contest = user.setdefault("contest_tickets", {})
        cid = str(contest_id)
        per_contest[cid] = int(per_contest.get(cid, 0)) + int(amount)
        return per_contest[cid]

    return await _mutate(op)


async def get_contest_tickets(user_id: int, contest_id: str) -> int:
    data = await load_data()
    user = data["users"].get(str(user_id), {})
    return int(user.get("contest_tickets", {}).get(str(contest_id), 0))


# --- Конкурсы -------------------------------------------------------------

def _ensure_contest(data: dict[str, Any], contest_id: str) -> dict[str, Any] | None:
    return data["contests"].get(str(contest_id))


async def get_contest(contest_id: str) -> dict[str, Any] | None:
    data = await load_data()
    contest = _ensure_contest(data, contest_id)
    return copy.deepcopy(contest) if contest else None


async def list_contests() -> dict[str, Any]:
    data = await load_data()
    return copy.deepcopy(data["contests"])


async def update_contest(contest_id: str, **kwargs: Any) -> dict[str, Any] | None:
    def op(data: dict[str, Any]) -> dict[str, Any] | None:
        contest = _ensure_contest(data, contest_id)
        if contest is None:
            return None
        contest.update(kwargs)
        return copy.deepcopy(contest)

    return await _mutate(op)


async def create_contest(contest: dict[str, Any]) -> dict[str, Any]:
    def op(data: dict[str, Any]) -> dict[str, Any]:
        contests = data["contests"]
        cid = str(contest["id"])
        contests[cid] = contest
        return copy.deepcopy(contest)

    return await _mutate(op)


def next_contest_id(data: dict[str, Any]) -> str:
    """Следующий свободный числовой id."""
    max_id = 0
    for key in data["contests"]:
        try:
            max_id = max(max_id, int(key))
        except (TypeError, ValueError):
            continue
    return str(max_id + 1)


async def add_participant(contest_id: str, user_id: int) -> bool:
    """Добавить участника. Возвращает False, если уже участвует."""
    def op(data: dict[str, Any]) -> bool:
        contest = _ensure_contest(data, contest_id)
        if contest is None:
            return False
        participants = contest.setdefault("participants", [])
        uid = str(user_id)
        if uid in participants:
            return False
        participants.append(uid)
        user = _ensure_user(data, user_id)
        if contest_id not in user["contests_participated"]:
            user["contests_participated"].append(contest_id)
        return True

    return await _mutate(op)


async def set_winners(
    contest_id: str,
    winners: Iterable[int | str],
    reserve: Iterable[int | str],
    transparency: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    def op(data: dict[str, Any]) -> dict[str, Any] | None:
        contest = _ensure_contest(data, contest_id)
        if contest is None:
            return None
        contest["winners"] = [str(w) for w in winners]
        contest["reserve_winners"] = [str(w) for w in reserve]
        contest["status"] = "finished"
        if transparency:
            contest["transparency"] = transparency
        return copy.deepcopy(contest)

    return await _mutate(op)


# --- Рефералы -------------------------------------------------------------

async def add_referral(referrer_id: int, referred_id: int, contest_id: str) -> bool:
    """Засчитать реферал: добавляет referred_id в список рефералов реферера.

    Возвращает False, если реферал уже был засчитан.
    """
    def op(data: dict[str, Any]) -> bool:
        referrer = _ensure_user(data, referrer_id)
        rid = str(referred_id)
        if rid in referrer["referrals"]:
            return False
        referrer["referrals"].append(rid)
        return True

    result = await _mutate(op)
    invalidate_leaderboard_cache(contest_id)
    return result


async def set_pending_referral(user_id: int, referrer_id: int) -> None:
    def op(data: dict[str, Any]) -> None:
        data["pending_referrals"][str(user_id)] = str(referrer_id)

    await _mutate(op)


async def pop_pending_referral(user_id: int) -> int | None:
    """Забрать и очистить pending-реферал пользователя."""
    def op(data: dict[str, Any]) -> int | None:
        ref = data["pending_referrals"].pop(str(user_id), None)
        try:
            return int(ref) if ref is not None else None
        except (TypeError, ValueError):
            return None

    return await _mutate(op)


# --- Кодовые слова --------------------------------------------------------

async def register_code_word(contest_id: str, user_id: int, word: str) -> bool:
    """Зафиксировать попытку ввода кодового слова.

    Возвращает False, если слово этим пользователем в этом конкурсе уже вводилось.
    """
    def op(data: dict[str, Any]) -> bool:
        attempts = data["code_word_attempts"].setdefault(str(contest_id), {})
        used = attempts.setdefault(str(user_id), [])
        if word in used:
            return False
        used.append(word)
        return True

    return await _mutate(op)


async def get_code_word_attempts(contest_id: str, user_id: int) -> list[str]:
    data = await load_data()
    attempts = data["code_word_attempts"].get(str(contest_id), {})
    return list(attempts.get(str(user_id), []))


# --- Панель администратора (FSM-состояние) --------------------------------

async def get_admin_state(user_id: int) -> dict[str, Any]:
    data = await load_data()
    return copy.deepcopy(data["admin_state"].get(str(user_id), {}))


async def set_admin_state(user_id: int, state: dict[str, Any]) -> None:
    def op(data: dict[str, Any]) -> None:
        data["admin_state"][str(user_id)] = state

    await _mutate(op)


async def clear_admin_state(user_id: int) -> None:
    def op(data: dict[str, Any]) -> None:
        data["admin_state"].pop(str(user_id), None)

    await _mutate(op)


# --- Рейтинг приглашающих -------------------------------------------------

async def get_leaderboard(contest_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Топ участников конкурса по числу приглашённых (кэш 60 секунд)."""
    cached = _LEADERBOARD_CACHE.get(str(contest_id))
    now = time.monotonic()
    if cached and now - cached[0] < _LEADERBOARD_TTL:
        return cached[1][:limit]

    data = await load_data()
    contest = data["contests"].get(str(contest_id), {})
    participants = set(contest.get("participants", []))
    board: list[dict[str, Any]] = []
    for uid in participants:
        user = data["users"].get(uid, {})
        invited = [r for r in user.get("referrals", []) if r in participants]
        if invited:
            board.append(
                {
                    "user_id": int(uid),
                    "username": user.get("username"),
                    "invited": len(invited),
                }
            )
    board.sort(key=lambda item: (-item["invited"], item["user_id"]))
    _LEADERBOARD_CACHE[str(contest_id)] = (now, board)
    return board[:limit]


def invalidate_leaderboard_cache(contest_id: str | None = None) -> None:
    if contest_id is None:
        _LEADERBOARD_CACHE.clear()
    else:
        _LEADERBOARD_CACHE.pop(str(contest_id), None)
