"""Инициализация базового каталога достижений Fable."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from app.services.achievement_service import AchievementDefinition, upsert_achievement_catalog

DEFAULT_ACHIEVEMENTS = [
    AchievementDefinition(code="first_message", title="Первый шаг", description="Отправить первое сообщение в чате", reward_coins=50),
    AchievementDefinition(code="first_transfer", title="Щедрая душа", description="Сделать первый перевод монет другому игроку", reward_coins=100),
    AchievementDefinition(code="banker", title="Инвестор", description="Положить монеты на банковский счет", reward_coins=150),
    AchievementDefinition(code="first_duel_win", title="Дуэлянт", description="Одержать первую победу в дуэли", reward_coins=300),
    AchievementDefinition(code="clan_member", title="Братство", description="Создать клан или вступить в клан", reward_coins=200),
    AchievementDefinition(code="trader", title="Биржевик", description="Совершить первую сделку на бирже", reward_coins=250),
    AchievementDefinition(code="fisherman", title="Рыболов", description="Поймать первую рыбу на рыбалке", reward_coins=50),
    AchievementDefinition(code="hunter", title="Следопыт", description="Добыть первый трофей на охоте", reward_coins=50),
]

async def seed_initial_achievements(session: AsyncSession) -> int:
    return await upsert_achievement_catalog(session, DEFAULT_ACHIEVEMENTS)
