"""Сервис настроек чата, правил, капчи, репортов и админ-панели Fable.

Правила из ТЗ:
- 7.9. Заморозка ("заморозка вкл 10м", "заморозка выкл"): запрет обычным участникам писать, сохранение срока в БД.
- 7.10. Слоумод ("слоумод 10", "слоумод выкл").
- 7.11. Репорты ("репорт @user причина" или кнопка «🚩 Пожаловаться»):
  * Приватный тикет ModerationReport.
  * Обработка модераторами ур. 2+ (принять, отклонить, закрыть).
- 9. Правила чата ("правила", "правило добавить", "правило удалить", "правило изменить"):
  * Обязательное принятие правил: таймер, ограничение общения до нажатия «Принять правила».
- 10. Капча:
  * Включение/выключение, таймер.
  * Индивидуальная проверка (кнопка с токеном), защита от прохождения другим пользователем.
- 11. Приветствие и вход:
  * Текст по умолчанию: «Я Хранитель, настроить — настройки, правила — правила, команды — команды».
  * Поддержка имени участника: «Добро пожаловать, Иван! Ознакомься с правилами чата».
- 12. Локальная админ-панель ("настройки") и 3 пресета:
  * Строгий, Обычный, Казуальный (без антиспама/антифлуда/авто-мутов/авто-банов).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Sequence

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    CaptchaChallenge,
    Chat,
    ChatRule,
    ModerationReport,
    RuleAcceptance,
)
from app.services.moderation_service import get_user_admin_level, PermissionDeniedError


class ChatSettingsError(Exception):
    """Базовая ошибка настроек чата."""


DEFAULT_CHAT_SETTINGS = {
    "welcome_enabled": True,
    "welcome_text": "Добро пожаловать, {name}! Ознакомься с правилами чата.\nЯ Хранитель, настроить — настройки, правила — правила, команды — команды.",
    "links_restricted": True,
    "captcha_enabled": False,
    "captcha_timeout_seconds": 120,
    "rules_acceptance_required": False,
    "rules_timeout_seconds": 300,
    "frozen_until": None,
    "slowmode_seconds": 0,
    "log_channel_id": None,
    "preset": "normal",
}


async def get_or_create_chat(
    session: AsyncSession,
    *,
    chat_id: int,
    title: str = "",
    chat_type: str = "supergroup",
) -> Chat:
    """Возвращает или создает запись чата с настройками по умолчанию атомарно."""
    chat = await session.get(Chat, chat_id)
    if not chat:
        stmt = insert(Chat).values(
            telegram_id=chat_id,
            title=title,
            chat_type=str(chat_type),
            is_active=True,
            settings=DEFAULT_CHAT_SETTINGS.copy(),
        ).on_conflict_do_nothing(index_elements=[Chat.telegram_id])
        await session.execute(stmt)
        chat = await session.get(Chat, chat_id)
        if not chat:
            raise RuntimeError(f"Не удалось получить чат {chat_id}")
    return chat


async def update_chat_setting(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    key: str,
    value: any,
    min_level: int = 5,
) -> dict:
    """Обновляет параметр в настройках чата с проверкой прав."""
    lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if lvl < min_level:
        raise PermissionDeniedError(f"Для изменения этой настройки требуется уровень {min_level}")

    chat = await get_or_create_chat(session, chat_id=chat_id)
    current_settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)
    current_settings[key] = value
    chat.settings = current_settings
    await session.flush()
    return current_settings


async def apply_chat_preset(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    preset_name: str,  # 'strict', 'normal', 'casual'
) -> dict:
    """Применяет один из 3 согласованных пресетов (Строгий, Обычный, Казуальный)."""
    lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if lvl < 5:
        raise PermissionDeniedError("Применять пресеты может только Создатель (ур. 5)")

    chat = await get_or_create_chat(session, chat_id=chat_id)
    settings = dict(chat.settings or DEFAULT_CHAT_SETTINGS)

    p = preset_name.strip().lower()
    if p in ("строгий", "strict"):
        settings["preset"] = "strict"
        settings["links_restricted"] = True
        settings["captcha_enabled"] = True
        settings["rules_acceptance_required"] = True
    elif p in ("обычный", "normal"):
        settings["preset"] = "normal"
        settings["links_restricted"] = True
        settings["captcha_enabled"] = False
        settings["rules_acceptance_required"] = False
    elif p in ("казуальный", "casual"):
        settings["preset"] = "casual"
        settings["links_restricted"] = False
        settings["captcha_enabled"] = False
        settings["rules_acceptance_required"] = False
    else:
        raise ChatSettingsError("Доступные пресеты: строгий, обычный, казуальный")

    chat.settings = settings
    await session.flush()
    return settings


# ===================== ПРАВИЛА ЧАТА =====================

async def get_chat_rules(session: AsyncSession, *, chat_id: int) -> list[ChatRule]:
    """Возвращает активные правила чата по порядку."""
    res = await session.scalars(
        select(ChatRule)
        .where(ChatRule.chat_id == chat_id, ChatRule.is_enabled.is_(True))
        .order_by(ChatRule.position.asc())
    )
    return list(res.all())


async def add_chat_rule(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    text: str,
) -> ChatRule:
    """Добавляет правило чата (ур. 3+)."""
    lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if lvl < 3:
        raise PermissionDeniedError("Добавлять правила может Админ (ур. 3+)")

    max_pos = (
        await session.scalar(
            select(func.coalesce(func.max(ChatRule.position), 0)).where(
                ChatRule.chat_id == chat_id
            )
        )
        or 0
    )
    rule = ChatRule(
        chat_id=chat_id,
        text=text.strip(),
        position=max_pos + 1,
        is_enabled=True,
    )
    session.add(rule)
    await session.flush()
    return rule


async def remove_chat_rule(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    position: int,
) -> bool:
    """Удаляет правило по номеру позиции (ур. 3+)."""
    lvl = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if lvl < 3:
        raise PermissionDeniedError("Удалять правила может Админ (ур. 3+)")

    res = await session.execute(
        delete(ChatRule).where(
            ChatRule.chat_id == chat_id, ChatRule.position == position
        )
    )
    await session.flush()
    return (res.rowcount or 0) > 0


async def accept_rules(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> RuleAcceptance:
    """Фиксирует факт принятия правил пользователем."""
    acc = RuleAcceptance(
        chat_id=chat_id,
        user_id=user_id,
    )
    session.add(acc)
    await session.flush()
    return acc


async def has_accepted_rules(
    session: AsyncSession, *, chat_id: int, user_id: int
) -> bool:
    chat = await get_or_create_chat(session, chat_id=chat_id)
    if not (chat.settings or {}).get("rules_acceptance_required", False):
        return True
    acc = await session.scalar(
        select(RuleAcceptance).where(
            RuleAcceptance.chat_id == chat_id, RuleAcceptance.user_id == user_id
        )
    )
    return acc is not None


# ===================== КАПЧА =====================

async def create_captcha(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
) -> CaptchaChallenge:
    """Создает индивидуальную капчу при входе."""
    chat = await get_or_create_chat(session, chat_id=chat_id)
    timeout = (chat.settings or {}).get("captcha_timeout_seconds", 120)
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)

    challenge = CaptchaChallenge(
        chat_id=chat_id,
        user_id=user_id,
        challenge_token=token,
        expected_answer="pass",
        status="pending",
        expires_at=now + timedelta(seconds=timeout),
    )
    session.add(challenge)
    await session.flush()
    return challenge


async def solve_captcha(
    session: AsyncSession,
    *,
    challenge_token: str,
    actor_id: int,
) -> bool:
    """Проверяет ответ на капчу. Защита от прохождения другим пользователем."""
    challenge = await session.scalar(
        select(CaptchaChallenge).where(
            CaptchaChallenge.challenge_token == challenge_token
        ).with_for_update()
    )
    if not challenge or challenge.status != "pending":
        return False

    # Только тот пользователь, для которого создана капча, может ее пройти
    if challenge.user_id != actor_id:
        return False

    now = datetime.now(timezone.utc)
    if now > challenge.expires_at:
        challenge.status = "expired"
        await session.flush()
        return False

    challenge.status = "completed"
    challenge.completed_at = now
    await session.flush()
    return True


# ===================== РЕПОРТЫ =====================

async def create_report(
    session: AsyncSession,
    *,
    chat_id: int,
    reporter_id: int,
    target_id: int | None,
    reason: str,
    message_id: int | None = None,
    message_text: str | None = None,
) -> ModerationReport:
    """Создает приватный тикет жалобы."""
    report = ModerationReport(
        chat_id=chat_id,
        reporter_id=reporter_id,
        target_id=target_id,
        message_id=message_id,
        message_text=message_text,
        reason=reason.strip(),
        status="open",
    )
    session.add(report)
    await session.flush()
    return report


async def handle_report(
    session: AsyncSession,
    *,
    report_id: int,
    moderator_id: int,
    resolution: str,  # 'accepted', 'rejected', 'closed'
) -> ModerationReport:
    """Обработка репорта модератором ур. 2+."""
    report = await session.scalar(
        select(ModerationReport).where(ModerationReport.id == report_id).with_for_update()
    )
    if not report:
        raise ChatSettingsError("Жалоба не найдена")

    lvl = await get_user_admin_level(session, chat_id=report.chat_id, user_id=moderator_id)
    if lvl < 2:
        raise PermissionDeniedError("Обрабатывать жалобы может только модератор (ур. 2+)")

    report.status = resolution
    report.handled_by = moderator_id
    report.resolution = resolution
    report.closed_at = datetime.now(timezone.utc)
    await session.flush()
    return report
