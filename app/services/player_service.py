# Сервис глобальных профилей игроков Fable.

# Один Telegram ID соответствует одной записи Player во всей системе,
# независимо от того, в каком чате пользователь взаимодействует с ботом.

# Сервис не открывает и не фиксирует транзакции самостоятельно: управление
# транзакцией остаётся у вызывающего слоя (обработчика или use-case).
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player


@dataclass(frozen=True, slots=True)
class PlayerRegistration:
    """Результат получения профиля."""

    player: Player
    created: bool


def format_time_in_bot(registered_at) -> str:
    """Возвращает строку времени нахождения игрока в боте."""
    if not registered_at:
        return "недавно"
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    if registered_at.tzinfo is None:
        registered_at = registered_at.replace(tzinfo=timezone.utc)
    delta = now - registered_at
    days = delta.days
    seconds = delta.seconds
    if days < 0:
        return "только что"
    if days == 0:
        hours = seconds // 3600
        if hours == 0:
            minutes = max(1, seconds // 60)
            return f"{minutes} мин. назад"
        return f"{hours} ч. назад"
    if days == 1:
        return "вчера"
    if days < 7:
        return f"{days} дн. назад"
    if days < 30:
        weeks = max(1, days // 7)
        return f"{weeks} нед. назад"
    if days < 365:
        months = max(1, days // 30)
        return f"{months} мес. назад"
    years = max(1, days // 365)
    return f"{years} г. назад"


def _clean_optional_name(value: str | None, *, limit: int) -> str | None:
    """Нормализует необязательные поля Telegram-профиля."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned[:limit] or None


async def get_player(
    session: AsyncSession,
    telegram_id: int,
    *,
    for_update: bool = False,
) -> Player | None:
    """Возвращает глобальный профиль либо None, если игрок ещё не зарегистрирован."""
    if telegram_id <= 0:
        raise ValueError("telegram_id должен быть положительным числом")

    statement = select(Player).where(Player.telegram_id == telegram_id)
    if for_update:
        statement = statement.with_for_update()
    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def get_or_create_player(
    session: AsyncSession,
    *,
    telegram_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
) -> PlayerRegistration:
    """Атомарно создаёт профиль при первом обращении или обновляет его имя.

    PostgreSQL INSERT ... ON CONFLICT DO NOTHING защищает регистрацию от
    гонки, когда два обновления одного Telegram-пользователя обрабатываются
    одновременно. Баланс, банк, опыт, уровень и репутация существующего
    профиля никогда не перезаписываются этой операцией.

    Важно: вызывающий код должен завершить транзакцию через commit/rollback.
    """
    if telegram_id <= 0:
        raise ValueError("telegram_id должен быть положительным числом")

    normalized_first_name = (first_name or "").strip()[:128]
    normalized_last_name = _clean_optional_name(last_name, limit=128)
    normalized_username = _clean_optional_name(username, limit=64)
    if not normalized_first_name:
        normalized_first_name = "Игрок"

    insert_statement = (
        insert(Player)
        .values(
            telegram_id=telegram_id,
            first_name=normalized_first_name,
            last_name=normalized_last_name,
            username=normalized_username,
            wallet=0,
            bank_balance=0,
            experience=0,
            reputation=0,
            is_globally_banned=False,
        )
        .on_conflict_do_nothing(index_elements=[Player.telegram_id])
        .returning(Player.telegram_id)
    )
    inserted_id = (await session.execute(insert_statement)).scalar_one_or_none()
    created = inserted_id is not None

    # Получаем ORM-объект независимо от того, была ли запись только что
    # создана или уже существовала до текущего обновления.
    player = await get_player(session, telegram_id, for_update=True)
    if player is None:
        # При обычной транзакции PostgreSQL это означает нарушение целостности
        # или неожиданное удаление записи параллельной операцией.
        raise RuntimeError(
            f"Не удалось получить профиль игрока Telegram ID {telegram_id}"
        )

    # Синхронизируем только Telegram-идентичность. Экономические и игровые
    # поля здесь намеренно не меняются.
    player.first_name = normalized_first_name
    player.last_name = normalized_last_name
    player.username = normalized_username

    return PlayerRegistration(player=player, created=created)


async def update_player_identity(
    session: AsyncSession,
    *,
    telegram_id: int,
    first_name: str,
    last_name: str | None = None,
    username: str | None = None,
) -> Player:
    """Обновляет имя/username уже зарегистрированного игрока.

    Если профиль отсутствует, выбрасывает LookupError, а не регистрирует
    пользователя незаметно для вызывающего кода.
    """
    player = await get_player(session, telegram_id, for_update=True)
    if player is None:
        raise LookupError(f"Игрок Telegram ID {telegram_id} не зарегистрирован")

    normalized_first_name = (first_name or "").strip()[:128]
    if normalized_first_name:
        player.first_name = normalized_first_name

    if last_name is not None:
        player.last_name = _clean_optional_name(last_name, limit=128)
    if username is not None:
        player.username = _clean_optional_name(username, limit=64)

    return player
