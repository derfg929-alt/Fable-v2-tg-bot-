"""Общие утилиты: имя бота, реферальные ссылки, проверка подписок."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger("fable_random_bot")

_bot_username: str | None = None


async def get_bot_username(bot: Bot) -> str:
    """Username бота без @ (кэшируется на процесс)."""
    global _bot_username
    if _bot_username:
        return _bot_username
    me = await bot.get_me()
    _bot_username = me.username or ""
    return _bot_username


def ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start=ref_{user_id}"


def display_name(user: dict[str, Any] | None, user_id: int | str) -> str:
    if user and user.get("username"):
        return f"@{user['username']}"
    return f"id{user_id}"


class SubscriptionChecker:
    """Очередь проверок подписки с ограничением параллелизма.

    При массовом притоке (реферальная ссылка в крупном чате) сотни кликов
    одновременно привели бы к 429 Too Many Requests. Проверки ставятся в
    очередь и обрабатываются небольшим числом воркеров.
    """

    def __init__(self, bot: Bot, workers: int = 4) -> None:
        self._bot = bot
        self._queue: asyncio.Queue = asyncio.Queue()
        self._workers = workers
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        for _ in range(self._workers):
            self._tasks.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    async def _worker(self) -> None:
        while True:
            channel, user_id, future = await self._queue.get()
            try:
                if future.done():
                    continue
                result = await self._check_one(channel, user_id)
                future.set_result(result)
            except Exception as exc:  # noqa: BLE001
                if not future.done():
                    future.set_exception(exc)
            finally:
                self._queue.task_done()

    async def _check_one(self, channel: str, user_id: int) -> bool:
        try:
            member = await self._bot.get_chat_member(chat_id=channel, user_id=user_id)
        except TelegramBadRequest as exc:
            logger.warning("Проверка подписки %s для %s: %s", channel, user_id, exc)
            return False
        return member.status not in ("left", "kicked")

    async def is_subscribed(self, channel: str, user_id: int) -> bool:
        if not self._tasks:
            # Воркеры не запущены (например, в тестах) — проверяем напрямую.
            return await self._check_one(channel, user_id)
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        await self._queue.put((channel, user_id, future))
        return await future


def contest_channels(contest: dict[str, Any]) -> list[str]:
    channels: list[str] = []
    if contest.get("channel"):
        channels.append(contest["channel"])
    for sponsor in contest.get("sponsors", []) or []:
        if sponsor not in channels:
            channels.append(sponsor)
    return channels


async def missing_subscriptions(
    checker: SubscriptionChecker, contest: dict[str, Any], user_id: int
) -> list[str]:
    channels = contest_channels(contest)
    if not channels:
        return []
    results = await asyncio.gather(
        *(checker.is_subscribed(channel, user_id) for channel in channels)
    )
    return [channel for channel, ok in zip(channels, results) if not ok]
