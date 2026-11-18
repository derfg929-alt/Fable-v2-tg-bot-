"""Конфигурация бота розыгрышей.

Токен читается из переменной окружения BOT_TOKEN (на Bothost задаётся в
панели переменных окружения). Путь к базе: на хостинге — /app/data/database.json
(папка /app/data сохраняется между перезапусками), локально — ./data/database.json.
"""
from __future__ import annotations

import os
from pathlib import Path

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")

# Telegram ID администраторов бота. Задаётся строкой через запятую:
# ADMIN_IDS="111,222,333". На создание конкурсов не влияет — их может создавать
# любой пользователь; оставлено для служебных проверок.
def _parse_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                ids.append(int(chunk))
            except ValueError:
                pass
    return ids


ADMIN_IDS: list[int] = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))

# Часовой пояс для планировщика и разбора дат.
TIMEZONE: str = os.getenv("TIMEZONE", "Europe/Moscow")

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "")

if not DATABASE_PATH:
    if os.path.isdir("/app/data"):
        DATABASE_PATH = "/app/data/database.json"
    else:
        DATABASE_PATH = str(Path(__file__).resolve().parent / "data" / "database.json")

DB_PATH: str = DATABASE_PATH


def is_admin(user_id: int) -> bool:
    """Является ли пользователь администратором Randomizer."""
    return user_id in ADMIN_IDS
