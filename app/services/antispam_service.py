"""Автоматический антиспам Fable.

Сервис отслеживает частоту сообщений и повторы текста в групповых чатах и
автоматически ограничивает нарушителя на заданное время. По умолчанию антиспам
выключен — администратор включает его командой "антиспам вкл".

Поведение:
- Скользящее окно: не более N сообщений за W секунд.
- Детекция флуда повторами: одно и то же сообщение подряд M раз.
- Первое срабатывание — автоматический мут на D секунд.
- Повторные срабатывания (>= escalate_after) — автоматический временный бан на D секунд.
- Наказание снимается автоматически по истечении срока (см. expire_moderation_cases).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FloodTracker
from app.services.moderation_service import (
    ensure_player_and_chat_exist,
    get_user_admin_level,
    issue_mute,
    record_ban_or_kick,
)

DEFAULT_ANTISPAM = {
    "antispam_enabled": False,
    "antispam_window_seconds": 10,
    "antispam_max_messages": 6,
    "antispam_duplicate_threshold": 4,
    "antispam_duration_seconds": 1800,  # 30 минут
    "antispam_action": "mute",  # mute | ban
    "antispam_escalate_after": 3,  # после N срабатываний — временный бан
    "antispam_caps_ratio": 0.7,
    "antispam_caps_min_length": 12,
    "antispam_emoji_limit": 15,
}


async def check_flood_pause(
    session: AsyncSession,
    *,
    chat_id: int,
    user_id: int,
    min_interval_seconds: int,
) -> bool:
    """Анти-флуд «пауза»: True, если игрок пишет чаще, чем раз в N секунд.

    Использует то же хранилище FloodTracker, но работает независимо от антиспама:
    хранит время последнего сообщения и сравнивает интервал. Возвращает True, если
    сообщение нужно заблокировать (слишком часто), иначе запоминает время и False.
    """
    if min_interval_seconds <= 0:
        return False
    tracker = await _get_tracker(session, chat_id=chat_id, user_id=user_id)
    now = _now()
    times: list[str] = []
    for t in (tracker.message_times or []):
        try:
            parsed = datetime.fromisoformat(t)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        # храним только последние 20 отметок времени
        if (now - parsed).total_seconds() <= 600:
            times.append(parsed.isoformat())
    times = times[-20:]

    last_ts = datetime.fromisoformat(times[-1]) if times else None
    too_fast = last_ts is not None and (now - last_ts).total_seconds() < min_interval_seconds
    if not too_fast:
        times.append(now.isoformat())
    tracker.message_times = times
    await session.flush()
    return too_fast


@dataclass(frozen=True, slots=True)
class AntispamResult:
    action: str  # mute | ban
    duration: timedelta
    reason: str
    trigger_count: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _get_tracker(session: AsyncSession, *, chat_id: int, user_id: int) -> FloodTracker:
    tracker = await session.scalar(
        select(FloodTracker).where(
            FloodTracker.chat_id == chat_id,
            FloodTracker.user_id == user_id,
        )
    )
    if tracker:
        return tracker
    await ensure_player_and_chat_exist(session, chat_id=chat_id, user_ids=[user_id])
    tracker = FloodTracker(
        chat_id=chat_id,
        user_id=user_id,
        message_times=[],
        duplicate_count=0,
        auto_mute_count=0,
    )
    session.add(tracker)
    await session.flush()
    return tracker


async def check_and_apply_antispam(
    session: AsyncSession,
    *,
    bot=None,
    chat_id: int,
    user_id: int,
    text: str | None,
    settings: dict | None = None,
) -> AntispamResult | None:
    """Проверяет сообщение на флуд. При нарушении выдаёт авто-мут/бан и возвращает результат."""
    settings = settings or {}
    if not settings.get("antispam_enabled", DEFAULT_ANTISPAM["antispam_enabled"]):
        return None

    # Администраторы чата не ограничиваются антиспамом.
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=user_id, bot=bot)
    if level >= 2:
        return None

    window = int(settings.get("antispam_window_seconds", DEFAULT_ANTISPAM["antispam_window_seconds"]))
    max_messages = int(settings.get("antispam_max_messages", DEFAULT_ANTISPAM["antispam_max_messages"]))
    dup_threshold = int(
        settings.get("antispam_duplicate_threshold", DEFAULT_ANTISPAM["antispam_duplicate_threshold"])
    )
    duration_sec = int(settings.get("antispam_duration_seconds", DEFAULT_ANTISPAM["antispam_duration_seconds"]))
    base_action = str(settings.get("antispam_action", DEFAULT_ANTISPAM["antispam_action"]))
    escalate_after = int(settings.get("antispam_escalate_after", DEFAULT_ANTISPAM["antispam_escalate_after"]))

    tracker = await _get_tracker(session, chat_id=chat_id, user_id=user_id)
    now = _now()
    cutoff = now - timedelta(seconds=window)

    times: list[str] = []
    for t in (tracker.message_times or []):
        try:
            parsed = datetime.fromisoformat(t)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if parsed > cutoff:
            times.append(parsed.isoformat())
    times.append(now.isoformat())
    tracker.message_times = times

    normalized = (text or "").strip().lower()
    if normalized and normalized == (tracker.last_content or ""):
        tracker.duplicate_count = (tracker.duplicate_count or 0) + 1
    else:
        tracker.duplicate_count = 0
    tracker.last_content = normalized

    flood_by_count = len(times) > max_messages
    flood_by_duplicates = tracker.duplicate_count >= dup_threshold
    flood_by_caps = False
    flood_by_emoji = False

    # Капс: длинное сообщение, где доля заглавных букв превышает порог.
    caps_ratio = float(settings.get("antispam_caps_ratio", DEFAULT_ANTISPAM.get("antispam_caps_ratio", 0.7)) or 0.7)
    caps_min_len = int(settings.get("antispam_caps_min_length", DEFAULT_ANTISPAM.get("antispam_caps_min_length", 12)) or 12)
    raw_text = (text or "").strip()
    letters = [c for c in raw_text if c.isalpha()]
    if caps_min_len > 0 and len(letters) >= caps_min_len:
        upper = sum(1 for c in letters if c.isupper())
        if upper / len(letters) >= caps_ratio:
            flood_by_caps = True

    # Спам эмодзи: слишком много эмодзи в одном сообщении.
    emoji_limit = int(settings.get("antispam_emoji_limit", DEFAULT_ANTISPAM.get("antispam_emoji_limit", 15)) or 0)
    if emoji_limit > 0:
        emoji_count = sum(1 for c in raw_text if ord(c) >= 0x1F000 or (0x2600 <= ord(c) <= 0x27BF))
        if emoji_count > emoji_limit:
            flood_by_emoji = True

    if not (flood_by_count or flood_by_duplicates or flood_by_caps or flood_by_emoji):
        await session.flush()
        return None

    # Наказание: эскалация до бана после повторных срабатываний.
    tracker.auto_mute_count = (tracker.auto_mute_count or 0) + 1
    trigger_count = tracker.auto_mute_count
    action = base_action
    if trigger_count >= escalate_after:
        action = "ban"

    duration = timedelta(seconds=max(30, duration_sec))
    if flood_by_caps:
        reason = "Авто-антиспам: капс"
    elif flood_by_emoji:
        reason = "Авто-антиспам: спам эмодзи"
    else:
        reason = "Авто-антиспам: флуд/повтор сообщений"

    if action == "ban":
        await record_ban_or_kick(
            session,
            chat_id=chat_id,
            moderator_id=bot.id if bot else 0,
            target_id=user_id,
            action="ban",
            reason=reason,
            duration=duration,
            bot=bot,
            system=True,
        )
    else:
        await issue_mute(
            session,
            chat_id=chat_id,
            moderator_id=bot.id if bot else 0,
            target_id=user_id,
            duration=duration,
            reason=reason,
            bot=bot,
            system=True,
        )

    # Сбрасываем окно, чтобы не наказывать повторно за те же сообщения.
    tracker.message_times = []
    tracker.duplicate_count = 0
    await session.flush()
    return AntispamResult(action=action, duration=duration, reason=reason, trigger_count=trigger_count)


def antispam_status_line(settings: dict | None) -> str:
    """Формирует строку состояния антиспама для панели настроек."""
    settings = settings or {}
    enabled = settings.get("antispam_enabled", DEFAULT_ANTISPAM["antispam_enabled"])
    if not enabled:
        return "🛑 Антиспам: <b>Выкл</b>"
    action = settings.get("antispam_action", DEFAULT_ANTISPAM["antispam_action"])
    dur = int(settings.get("antispam_duration_seconds", DEFAULT_ANTISPAM["antispam_duration_seconds"]))
    return f"🛡 Антиспам: <b>Вкл</b> ({action}, {dur // 60} мин.)"
