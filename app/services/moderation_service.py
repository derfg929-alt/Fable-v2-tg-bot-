"""Сервис модерации и иерархии прав Fable.

Правила из ТЗ:
- Иерархия уровней администраторов в чате:
  * Уровень 5 — Создатель (все права, назначение уровней 2-5).
  * Уровень 4 — Старший админ (назначение уровней 2-3, бан, кик, мут, варн, заморозка, очистка).
  * Уровень 3 — Админ (мут, варн, бан, кик, очистка, заморозка, правила, назначение уровня 2).
  * Уровень 2 — Модератор (мут, варн, очистка, репорты).
  * Уровень 1 — Обычный игрок.
- Правила назначения и наказаний:
  * Нельзя наказывать равного или вышестоящего администратора.
  * Нельзя повышать самого себя. Самопонижение разрешено.
  * Повышать может только пользователь, стоящий выше по уровню.
  * Назначать уровень 5 может только уровень 5. Снимать уровень 5 может только другой уровень 5.
- Команды модерации:
  * "мут @user 30м причина" (м - минуты, ч - часы, д - дни).
  * "размут @user".
  * "варн @user причина" (срок 30 дней, без автоматических санкций).
  * "варны @user", "варн снять @user".
  * "кик @user причина".
  * "бан @user причина", "разбан @user".
  * "админ список".
  * Назначение: "админ назначить @user <уровень>", "админ снять @user".
- Антиссылки:
  * Ссылки запрещены по умолчанию (http, https, www, t.me, инвайты, домены).
  * Упоминания вида @username разрешены.
  * Белый список ссылок: "ссылки добавить <домен>", "ссылки удалить <домен>", "ссылки список".
- Все действия логируются в AdminActionLog и ModerationCase.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Sequence

from sqlalchemy import delete, desc, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdminActionLog,
    Chat,
    ChatAdmin,
    ChatLinkWhitelist,
    ModerationCase,
    Player,
)


class ModerationError(Exception):
    """Базовая ошибка модерации."""


class PermissionDeniedError(ModerationError):
    """Недостаточно прав."""


class HierarchyViolationError(ModerationError):
    """Нарушение субординации уровней."""


ROLE_NAMES = {
    5: "Создатель",
    4: "Старший админ",
    3: "Админ",
    2: "Модератор",
    1: "Игрок",
}

# Регулярные выражения для поиска ссылок
URL_REGEX = re.compile(
    r"(?:https?://|www\.)?[a-zA-Z0-9-]+\.(?:com|org|ru|net|io|me|info|biz|ua|kz|by|dev|app|online|site|top|xyz)(?:/[^\s]*)?|t\.me/[^\s]+",
    re.IGNORECASE,
)


def parse_duration_string(duration_str: str) -> timedelta:
    """Парсит строки с длительностью в timedelta.
    Поддерживает:
      - Секунды: '67000 сек', '67000 секунд', '500с', '100s'
      - Минуты: '120 мин', '30м', '45 минут', 'полчаса'
      - Часы: '1 час 30 минут', 'час 30', 'час 2', '2 часа', 'на 2 часа', 'на час', '1ч 30м', '2ч'
      - Дни: '1 день', '3 дня', '7д', '1д'
      - Недели и месяцы: '1 неделя', '1 месяц'
      - Бессрочно: 'навсегда', 'вечно'
    """
    s = duration_str.strip().lower()
    if s.startswith("на "):
        s = s[3:].strip()

    patterns = [
        # Xд Yч Zм
        (
            r"^(\d+)\s*(?:д|дн|дней|дня|день|days?|d)\s*(\d+)\s*(?:ч|час|часа|часов|hours?|h)\s*(\d+)\s*(?:м|мин|минут|минуты|minutes?|m)?$",
            lambda m: timedelta(days=int(m.group(1)), hours=int(m.group(2)), minutes=int(m.group(3) or 0)),
        ),
        # Xд Yч
        (
            r"^(\d+)\s*(?:д|дн|дней|дня|день|days?|d)\s*(\d+)\s*(?:ч|час|часа|часов|hours?|h)$",
            lambda m: timedelta(days=int(m.group(1)), hours=int(m.group(2))),
        ),
        # Xч Yм ('1ч 30м', '1ч30м', '2 часа 15 минут', '1 ч 45')
        (
            r"^(\d+)\s*(?:ч|час|часа|часов|hours?|h)\s*(\d+)\s*(?:м|мин|минут|минуты|минута|minutes?|m)?$",
            lambda m: timedelta(hours=int(m.group(1)), minutes=int(m.group(2))),
        ),
        # 'час 2' / 'час два' / 'час-два' / '1-2 часа' -> 2 часа
        (
            r"^(?:час\s*[-–—]\s*(?:два|2)|час\s+(?:два|2)|1\s*[-–—]\s*2\s*(?:ч|часа|часов))$",
            lambda m: timedelta(hours=2),
        ),
        # 'час 30' / 'час 15' / 'час 45'
        (
            r"^(?:один\s+)?час\s*(\d{1,2})\s*(?:м|мин|минут|минуты|minutes?|m)?$",
            lambda m: timedelta(hours=1, minutes=int(m.group(1))),
        ),
        # Секунды: '67000 сек', '67000 секунд', '500с', '100s'
        (
            r"^(\d+)\s*(?:с|сек|секунд|секунды|секунда|seconds?|sec|s)$",
            lambda m: timedelta(seconds=int(m.group(1))),
        ),
        # Одиночные с числами: дни, часы, минуты
        (
            r"^(\d+)\s*(?:д|дн|дней|дня|день|days?|d)$",
            lambda m: timedelta(days=int(m.group(1))),
        ),
        (
            r"^(\d+)\s*(?:ч|час|часа|часов|hours?|h)$",
            lambda m: timedelta(hours=int(m.group(1))),
        ),
        (
            r"^(\d+)\s*(?:м|мин|минут|минуты|минута|minutes?|m)$",
            lambda m: timedelta(minutes=int(m.group(1))),
        ),
        # Слова без чисел
        (r"^(?:полчаса|пол-часа)$", lambda m: timedelta(minutes=30)),
        (r"^(?:час|один час)$", lambda m: timedelta(hours=1)),
        (r"^(?:два часа)$", lambda m: timedelta(hours=2)),
        (r"^(?:три часа)$", lambda m: timedelta(hours=3)),
        (r"^(?:день|сутки|один день)$", lambda m: timedelta(days=1)),
        (r"^(?:два дня)$", lambda m: timedelta(days=2)),
        (r"^(?:три дня)$", lambda m: timedelta(days=3)),
        (r"^(?:неделя|неделю|одну неделю)$", lambda m: timedelta(days=7)),
        (r"^(?:месяц|один месяц)$", lambda m: timedelta(days=30)),
        (r"^(?:навсегда|вечно)$", lambda m: timedelta(days=3650)),
    ]

    for pat, func in patterns:
        m = re.match(pat, s, re.IGNORECASE)
        if m:
            res = func(m)
            if res.total_seconds() <= 0:
                raise ModerationError("Срок должен быть больше 0")
            return res

    raise ModerationError("Неверный формат времени. Примеры: 67000 сек, 30м, 1ч 30м, 2 часа, на час 2, 1д")


def extract_moderation_args(
    full_text: str,
    target_id: int | None = None,
    default_duration: timedelta | None = timedelta(minutes=30),
) -> tuple[timedelta | None, str, str]:
    """Извлекает срок наказания (timedelta | None), строковое представление срока и причину.
    Корректно обрабатывает:
      - 'мут на 67000 сек спам'
      - 'мут 120 мин флуд'
      - 'мут 1 час 30 спам'
      - 'мут на час 2 флуд'
      - 'мут 2ч мат'
      - 'мут @user 1ч 30м оффтоп'
      - 'бан на 2 дня реклама'
      - 'бан читы' (бессрочно)
    """
    tokens = full_text.strip().split()
    if not tokens:
        disp = "30 мин." if default_duration == timedelta(minutes=30) else "навсегда"
        return default_duration, disp, "Нарушение порядка"

    cmd = tokens[0]
    rest = full_text.strip()[len(cmd):].strip()

    # Удаляем упоминание пользователя или цифровой ID в начале аргументов, если есть
    if rest.startswith("@"):
        parts = rest.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""
    elif target_id and rest.startswith(str(target_id)):
        parts = rest.split(maxsplit=1)
        rest = parts[1] if len(parts) > 1 else ""

    rest = rest.strip()
    s_work = rest
    leading_na = False
    if re.match(r"^на\s+", s_work, re.IGNORECASE):
        s_work = re.sub(r"^на\s+", "", s_work, count=1, flags=re.IGNORECASE).strip()
        leading_na = True

    patterns = [
        # Xд Yч Zм
        (
            r"^(\d+)\s*(?:д|дн|дней|дня|день|days?|d)\s*(\d+)\s*(?:ч|час|часа|часов|hours?|h)\s*(\d+)\s*(?:м|мин|минут|минуты|minutes?|m)?\b",
            lambda m: (timedelta(days=int(m.group(1)), hours=int(m.group(2)), minutes=int(m.group(3) or 0)), f"{m.group(1)} д. {m.group(2)} ч. {m.group(3) or 0} мин."),
        ),
        # Xд Yч
        (
            r"^(\d+)\s*(?:д|дн|дней|дня|день|days?|d)\s*(\d+)\s*(?:ч|час|часа|часов|hours?|h)\b",
            lambda m: (timedelta(days=int(m.group(1)), hours=int(m.group(2))), f"{m.group(1)} д. {m.group(2)} ч."),
        ),
        # Xч Yм
        (
            r"^(\d+)\s*(?:ч|час|часа|часов|hours?|h)\s*(\d+)\s*(?:м|мин|минут|минуты|минута|minutes?|m)?\b",
            lambda m: (timedelta(hours=int(m.group(1)), minutes=int(m.group(2))), f"{m.group(1)} ч. {m.group(2)} мин."),
        ),
        # 'час 2' / 'час два' / 'час-два' / '1-2 часа' -> 2 часа
        (
            r"^(?:час\s*[-–—]\s*(?:два|2)|час\s+(?:два|2)|1\s*[-–—]\s*2\s*(?:ч|часа|часов))\b",
            lambda m: (timedelta(hours=2), "2 ч."),
        ),
        # 'час 30' / 'час 15' / 'час 45'
        (
            r"^(?:один\s+)?час\s*(\d{1,2})\s*(?:м|мин|минут|минуты|minutes?|m)?\b",
            lambda m: (timedelta(hours=1, minutes=int(m.group(1))), f"1 ч. {m.group(1)} мин."),
        ),
        # Секунды: '67000 сек', '67000 секунд', '500с', '100s'
        (
            r"^(\d+)\s*(?:с|сек|секунд|секунды|секунда|seconds?|sec|s)\b",
            lambda m: (timedelta(seconds=int(m.group(1))), f"{m.group(1)} сек."),
        ),
        # Одиночные с числами: дни, часы, минуты
        (
            r"^(\d+)\s*(?:д|дн|дней|дня|день|days?|d)\b",
            lambda m: (timedelta(days=int(m.group(1))), f"{m.group(1)} д."),
        ),
        (
            r"^(\d+)\s*(?:ч|час|часа|часов|hours?|h)\b",
            lambda m: (timedelta(hours=int(m.group(1))), f"{m.group(1)} ч."),
        ),
        (
            r"^(\d+)\s*(?:м|мин|минут|минуты|минута|minutes?|m)\b",
            lambda m: (timedelta(minutes=int(m.group(1))), f"{m.group(1)} мин."),
        ),
        # Слова без чисел
        (r"^(?:полчаса|пол-часа)\b", lambda m: (timedelta(minutes=30), "30 мин.")),
        (r"^(?:час|один час)\b", lambda m: (timedelta(hours=1), "1 ч.")),
        (r"^(?:два часа)\b", lambda m: (timedelta(hours=2), "2 ч.")),
        (r"^(?:три часа)\b", lambda m: (timedelta(hours=3), "3 ч.")),
        (r"^(?:день|сутки|один день)\b", lambda m: (timedelta(days=1), "1 д.")),
        (r"^(?:два дня)\b", lambda m: (timedelta(days=2), "2 д.")),
        (r"^(?:три дня)\b", lambda m: (timedelta(days=3), "3 д.")),
        (r"^(?:неделя|неделю|одну неделю)\b", lambda m: (timedelta(days=7), "7 д.")),
        (r"^(?:месяц|один месяц)\b", lambda m: (timedelta(days=30), "30 д.")),
        (r"^(?:навсегда|вечно)\b", lambda m: (None, "навсегда")),
    ]

    matched_td = None
    matched_disp = ""
    matched_len = 0

    for pat, func in patterns:
        m = re.match(pat, s_work, re.IGNORECASE)
        if m:
            matched_td, matched_disp = func(m)
            matched_len = m.end()
            break

    if matched_td is not None or matched_disp == "навсегда":
        reason = s_work[matched_len:].strip()
        reason = re.sub(r"^[,\-–—:]\s*", "", reason).strip()
        if target_id and reason.startswith(str(target_id)):
            reason = reason[len(str(target_id)):].strip()
        elif reason.startswith("@"):
            parts = reason.split(maxsplit=1)
            reason = parts[1] if len(parts) > 1 else ""
        return matched_td, matched_disp, reason or "Нарушение порядка"

    reason = rest
    if leading_na:
        reason = "на " + s_work
    clean_words = []
    for w in reason.split():
        if w.startswith("@") or (target_id and w == str(target_id)):
            continue
        clean_words.append(w)
    final_reason = " ".join(clean_words).strip() or "Нарушение порядка"
    default_disp = (
        "30 мин."
        if default_duration == timedelta(minutes=30)
        else ("навсегда" if default_duration is None else f"{int(default_duration.total_seconds() // 60)} мин.")
    )
    return default_duration, default_disp, final_reason


async def get_user_admin_level(
    session: AsyncSession, *, chat_id: int, user_id: int, bot=None
) -> int:
    """Возвращает локальный уровень прав пользователя в чате (от 1 до 5).
    Если пользователь является создателем или администратором в самом Telegram,
    но в базе еще нет записи, уровень синхронизируется.
    """
    from app.config import get_settings
    # Глобальный владелец бота всегда имеет уровень 5 во всех чатах
    if user_id == 8039111975 or user_id == get_settings().owner_id:
        return 5

    admin_row = await session.scalar(
        select(ChatAdmin).where(
            ChatAdmin.chat_id == chat_id,
            ChatAdmin.user_id == user_id,
            ChatAdmin.is_active.is_(True),
        )
    )
    if admin_row:
        return admin_row.level

    # Попытка проверить права напрямую в Telegram чате, если в БД еще нет роли
    if bot:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            status = getattr(member, "status", None)
            if status in ("creator", "owner"):
                # Назначаем создателю ур. 5
                stmt = insert(ChatAdmin).values(
                    chat_id=chat_id,
                    user_id=user_id,
                    level=5,
                    assigned_by=user_id,
                    is_active=True,
                ).on_conflict_do_update(
                    index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
                    set_={"level": 5, "is_active": True},
                )
                await session.execute(stmt)
                return 5
            elif status == "administrator":
                # Обычные администраторы Telegram не получают админку бота автоматически
                return 1
        except Exception:
            pass

    return 1


async def sync_chat_creator_and_admins(session: AsyncSession, *, chat_id: int, bot) -> int | None:
    """Синхронизирует владельца (создателя) чата через Telegram Bot API.
    Админку (ур. 5) автоматически получает только создатель группы.
    Возвращает telegram_id создателя чата, если найден.
    """
    if not bot:
        return None
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
        creator_id = None
        for adm in admins:
            user = getattr(adm, "user", None)
            if not user or getattr(user, "is_bot", False):
                continue

            status = getattr(adm, "status", None)
            if status in ("creator", "owner"):
                creator_id = user.id
                level = 5

                # Убеждаемся, что создатель существует в БД игроков
                p_stmt = insert(Player).values(
                    telegram_id=user.id,
                    first_name=user.first_name or "Создатель",
                    username=user.username,
                ).on_conflict_do_nothing(index_elements=[Player.telegram_id])
                await session.execute(p_stmt)

                # Синхронизируем уровень только создателя
                stmt = insert(ChatAdmin).values(
                    chat_id=chat_id,
                    user_id=user.id,
                    level=level,
                    assigned_by=user.id,
                    is_active=True,
                ).on_conflict_do_update(
                    index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
                    set_={"level": level, "is_active": True},
                )
                await session.execute(stmt)
            else:
                # Обычные администраторы Telegram не получают права автоматически
                continue

        return creator_id
    except Exception:
        return None


async def ensure_player_and_chat_exist(
    session: AsyncSession,
    *,
    chat_id: int,
    user_ids: Sequence[int],
    bot=None,
) -> None:
    """Гарантирует существование чата и игроков в БД перед операциями модерации.
    Предотвращает ForeignKeyViolationError в moderation_cases, chat_admins и т.д.
    """
    # 1. Гарантируем запись чата
    chat_stmt = insert(Chat).values(
        telegram_id=chat_id,
        title=f"Chat {chat_id}",
        chat_type="supergroup" if chat_id < 0 else "private",
        is_active=True,
    ).on_conflict_do_nothing(index_elements=[Chat.telegram_id])
    await session.execute(chat_stmt)

    # 2. Гарантируем записи игроков
    for uid in user_ids:
        if not uid or uid <= 0:
            continue
        first_name = "Пользователь"
        username = None
        if bot:
            try:
                member = await bot.get_chat_member(chat_id=chat_id, user_id=uid)
                u = getattr(member, "user", None)
                if u:
                    fn = getattr(u, "first_name", None)
                    un = getattr(u, "username", None)
                    if isinstance(fn, str) and fn:
                        first_name = fn
                    if isinstance(un, str):
                        username = un
            except Exception:
                pass

        p_stmt = insert(Player).values(
            telegram_id=uid,
            first_name=first_name,
            username=username,
            wallet=0,
            bank_balance=0,
            experience=0,
            reputation=0,
        ).on_conflict_do_nothing(index_elements=[Player.telegram_id])
        await session.execute(p_stmt)


async def check_can_moderate(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    min_required_level: int = 2,
    bot=None,
) -> tuple[int, int]:
    """Проверяет право модератора наказывать цель."""
    await ensure_player_and_chat_exist(
        session, chat_id=chat_id, user_ids=[moderator_id, target_id], bot=bot
    )
    mod_level = await get_user_admin_level(session, chat_id=chat_id, user_id=moderator_id, bot=bot)
    if mod_level < min_required_level:
        raise PermissionDeniedError(
            f"Для этого действия требуется уровень {min_required_level} ({ROLE_NAMES[min_required_level]}), ваш уровень: {mod_level}"
        )

    target_level = await get_user_admin_level(session, chat_id=chat_id, user_id=target_id, bot=bot)
    if moderator_id != target_id and mod_level <= target_level:
        raise HierarchyViolationError(
            f"Вы не можете применить санкции к участнику равного или более высокого уровня ({ROLE_NAMES[target_level]})"
        )

    return mod_level, target_level


async def set_admin_level(
    session: AsyncSession,
    *,
    chat_id: int,
    actor_id: int,
    target_id: int,
    new_level: int,
    bot=None,
) -> int:
    """Назначение роли / уровня администратора с соблюдением всех правил иерархии."""
    await ensure_player_and_chat_exist(
        session, chat_id=chat_id, user_ids=[actor_id, target_id], bot=bot
    )
    actor_level = await get_user_admin_level(session, chat_id=chat_id, user_id=actor_id, bot=bot)
    target_level = await get_user_admin_level(session, chat_id=chat_id, user_id=target_id, bot=bot)

    if not (1 <= new_level <= 5):
        raise ModerationError("Уровень должен быть от 1 до 5")

    # Самопонижение разрешено
    if actor_id == target_id and actor_level > 1:
        if new_level >= actor_level:
            raise HierarchyViolationError("Самоповышение запрещено!")
    else:
        # Нельзя изменять равных и вышестоящих
        if actor_level <= target_level:
            raise HierarchyViolationError("Вы не можете изменять роль равного или вышестоящего администратора")
        # Нельзя назначить уровень равный или выше своего собственного
        if new_level >= actor_level:
            raise HierarchyViolationError("Вы не можете назначить уровень равный или выше собственного")
        # Назначать уровень 5 может только уровень 5
        if new_level == 5 and actor_level != 5:
            raise HierarchyViolationError("Только Создатель (ур. 5) может назначить другого Создателя")

    now = datetime.now(timezone.utc)
    if new_level == 1:
        # Снятие админки
        await session.execute(
            delete(ChatAdmin).where(
                ChatAdmin.chat_id == chat_id, ChatAdmin.user_id == target_id
            )
        )
    else:
        statement = insert(ChatAdmin).values(
            chat_id=chat_id,
            user_id=target_id,
            level=new_level,
            assigned_by=actor_id,
            is_active=True,
        ).on_conflict_do_update(
            index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
            set_={"level": new_level, "assigned_by": actor_id, "is_active": True},
        )
        await session.execute(statement)

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=actor_id,
        target_id=target_id,
        action="set_level",
        reason=f"Установлен уровень {new_level} ({ROLE_NAMES[new_level]})",
        details={"old_level": target_level, "new_level": new_level},
    )
    session.add(log_entry)
    await session.flush()
    return new_level


async def issue_mute(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    duration: timedelta,
    reason: str | None = None,
    bot=None,
) -> ModerationCase:
    """Выдача мута с проверкой прав."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    now = datetime.now(timezone.utc)
    expires_at = now + duration

    case = ModerationCase(
        chat_id=chat_id,
        target_id=target_id,
        moderator_id=moderator_id,
        action="mute",
        reason=reason or "Не указана",
        expires_at=expires_at,
        is_active=True,
    )
    session.add(case)

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="mute",
        reason=reason,
        details={"duration_sec": int(duration.total_seconds()), "expires_at": expires_at.isoformat()},
    )
    session.add(log_entry)
    await session.flush()
    return case


async def revoke_mute(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    bot=None,
) -> None:
    """Снятие мута."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    cases = (
        await session.scalars(
            select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "mute",
                ModerationCase.is_active.is_(True),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for c in cases:
        c.is_active = False
        c.revoked_by = moderator_id
        c.revoked_at = now

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="unmute",
        reason="Снятие мута",
        details={},
    )
    session.add(log_entry)
    await session.flush()


async def issue_warn(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    reason: str | None = None,
    bot=None,
) -> tuple[ModerationCase, int]:
    """Выдача варна (срок 30 дней, без авто-санкций). Возвращает (case, активных_варнов)."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=30)

    case = ModerationCase(
        chat_id=chat_id,
        target_id=target_id,
        moderator_id=moderator_id,
        action="warn",
        reason=reason or "Не указана",
        expires_at=expires_at,
        is_active=True,
    )
    session.add(case)

    # Считаем число активных варнов пользователя
    count = (
        await session.scalar(
            select(func.count(ModerationCase.id)).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "warn",
                ModerationCase.is_active.is_(True),
                ModerationCase.expires_at > now,
            )
        )
        or 0
    ) + 1

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="warn",
        reason=reason,
        details={"active_warns": count},
    )
    session.add(log_entry)
    await session.flush()
    return case, count


async def revoke_warn(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    bot=None,
) -> bool:
    """Снятие последнего активного варна."""
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)

    last_warn = await session.scalar(
        select(ModerationCase)
        .where(
            ModerationCase.chat_id == chat_id,
            ModerationCase.target_id == target_id,
            ModerationCase.action == "warn",
            ModerationCase.is_active.is_(True),
        )
        .order_by(desc(ModerationCase.created_at))
        .limit(1)
    )
    if not last_warn:
        return False

    now = datetime.now(timezone.utc)
    last_warn.is_active = False
    last_warn.revoked_by = moderator_id
    last_warn.revoked_at = now

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action="unwarn",
        reason="Снятие предупреждения",
        details={"warn_id": last_warn.id},
    )
    session.add(log_entry)
    await session.flush()
    return True


async def get_active_warns(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
) -> list[ModerationCase]:
    """Возвращает список активных варнов пользователя."""
    now = datetime.now(timezone.utc)
    cases = (
        await session.scalars(
            select(ModerationCase)
            .where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "warn",
                ModerationCase.is_active.is_(True),
                ModerationCase.expires_at > now,
            )
            .order_by(desc(ModerationCase.created_at))
        )
    ).all()
    return list(cases)


async def record_ban_or_kick(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    action: str,  # 'ban' or 'kick'
    reason: str | None = None,
    duration: timedelta | None = None,
    bot=None,
) -> ModerationCase:
    """Фиксация бана или кика (требуется ур. 3+ для бана/кика).
    Если указан duration, рассчитывается expires_at для временного бана.
    """
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=3, bot=bot)

    now = datetime.now(timezone.utc)
    expires_at = now + duration if (duration and action == "ban") else None

    case = ModerationCase(
        chat_id=chat_id,
        target_id=target_id,
        moderator_id=moderator_id,
        action=action,
        reason=reason or "Не указана",
        expires_at=expires_at,
        is_active=True,
    )
    session.add(case)

    log_entry = AdminActionLog(
        chat_id=chat_id,
        actor_id=moderator_id,
        target_id=target_id,
        action=action,
        reason=reason,
        details={
            "duration_sec": int(duration.total_seconds()) if duration else None,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )
    session.add(log_entry)
    await session.flush()
    return case


async def is_link_allowed(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
) -> tuple[bool, str | None]:
    """Проверяет сообщение на запрещенные ссылки.

    Возвращает (разрешено, найденная_ссылка).
    Если ссылка есть, но её домен в whitelist этого чата — разрешено.
    """
    matches = URL_REGEX.findall(text)
    if not matches:
        return True, None

    # Получаем белый список доменов данного чата
    whitelist_domains = (
        await session.scalars(
            select(ChatLinkWhitelist.domain).where(ChatLinkWhitelist.chat_id == chat_id)
        )
    ).all()
    allowed_domains = {d.lower() for d in whitelist_domains}

    for found in matches:
        clean_found = found.lower()
        # Проверяем, содержит ли ссылка домен из белого списка
        is_whitelisted = any(domain in clean_found for domain in allowed_domains)
        if not is_whitelisted:
            return False, found

    return True, None


async def add_link_whitelist(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    domain: str,
) -> str:
    """Добавляет домен в белый список ссылок (ур. 5)."""
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if level < 5:
        raise PermissionDeniedError("Управлять белым списком ссылок может только Создатель (ур. 5)")

    norm_domain = domain.strip().lower()
    stmt = insert(ChatLinkWhitelist).values(
        chat_id=chat_id, domain=norm_domain, added_by=admin_id
    ).on_conflict_do_nothing()
    await session.execute(stmt)
    await session.flush()
    return norm_domain


async def remove_link_whitelist(
    session: AsyncSession,
    *,
    chat_id: int,
    admin_id: int,
    domain: str,
) -> bool:
    """Удаляет домен из белого списка ссылок (ур. 5)."""
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id)
    if level < 5:
        raise PermissionDeniedError("Управлять белым списком ссылок может только Создатель (ур. 5)")

    norm_domain = domain.strip().lower()
    res = await session.execute(
        delete(ChatLinkWhitelist).where(
            ChatLinkWhitelist.chat_id == chat_id, ChatLinkWhitelist.domain == norm_domain
        )
    )
    await session.flush()
    return (res.rowcount or 0) > 0


async def list_link_whitelist(session: AsyncSession, *, chat_id: int) -> list[str]:
    """Возвращает список разрешенных доменов для чата."""
    domains = (
        await session.scalars(
            select(ChatLinkWhitelist.domain)
            .where(ChatLinkWhitelist.chat_id == chat_id)
            .order_by(ChatLinkWhitelist.domain)
        )
    ).all()
    return list(domains)


async def get_chat_admins(
    session: AsyncSession, *, chat_id: int
) -> list[tuple[Player, int, str]]:
    """Возвращает список админов чата: (player, level, role_name)."""
    res = (
        await session.execute(
            select(Player, ChatAdmin.level)
            .join(ChatAdmin, ChatAdmin.user_id == Player.telegram_id)
            .where(ChatAdmin.chat_id == chat_id, ChatAdmin.is_active.is_(True))
            .order_by(desc(ChatAdmin.level), Player.telegram_id)
        )
    ).all()
    return [(p, lvl, ROLE_NAMES.get(lvl, "Админ")) for p, lvl in res]
