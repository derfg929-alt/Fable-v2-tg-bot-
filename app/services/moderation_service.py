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


def _format_duration_ru(td: timedelta) -> str:
    """Форматирует timedelta в компактную русскую строку: '1 д. 2 ч. 30 мин.'."""
    total = int(td.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days} д.")
    if hours:
        parts.append(f"{hours} ч.")
    if minutes:
        parts.append(f"{minutes} мин.")
    if seconds and not (days or hours):
        parts.append(f"{seconds} сек.")
    return " ".join(parts) or "0 мин."


# Единицы срока: (регулярное выражение, секунды, подпись для ответа).
# Альтернативы идут от длинных к коротким, иначе «м» перехватит «мес».
_DURATION_UNITS: tuple[tuple[str, int, str], ...] = (
    (r"секунд\w*|секунды|секунда|сек|с|seconds?|secs?|s", 1, "сек."),
    (r"минут\w*|минуты|минута|мин|м|minutes?|mins?|m", 60, "мин."),
    (r"часов|часа|часу|час|ч|hours?|hrs?|h", 3600, "ч."),
    (r"суток|сутки|сутка|дней|дня|день|дн|д|days?|d", 86400, "д."),
    (r"недель|недели|неделю|неделя|нед|weeks?|w", 7 * 86400, "нед."),
    (r"месяцев|месяца|месяц|мес|months?|mon", 30 * 86400, "мес."),
    (r"года|год|году|лет|г|years?|yrs?|y", 365 * 86400, "г."),
)
_DURATION_UNIT_RE = "|".join(f"(?:{u})" for u, _, _ in _DURATION_UNITS)
# Замыкающая граница обязательна: без неё «1 сутки» распознавалось как «1 с».
_DURATION_PAIR_RE = re.compile(
    rf"(\d+)\s*({_DURATION_UNIT_RE})(?![а-яёa-z])", re.IGNORECASE
)


def _unit_seconds(unit: str) -> tuple[int, str] | None:
    for pattern, seconds, label in _DURATION_UNITS:
        if re.fullmatch(pattern, unit, re.IGNORECASE):
            return seconds, label
    return None


def _match_compound_duration(text: str) -> tuple[timedelta, str, int] | None:
    """Разбирает «2 недели», «1 сутки», «1ч 30м», «2д 3ч 15м».

    Дополнительно понимает разговорную форму «1 час 30» (один час тридцать
    минут): число без единицы после часов считается минутами.
    """
    total = 0
    parts: list[str] = []
    end = 0
    last_was_hours = False
    for match in _DURATION_PAIR_RE.finditer(text):
        if match.start() != end and text[end:match.start()].strip(" ,и"):
            break
        parsed = _unit_seconds(match.group(2))
        if parsed is None:
            break
        seconds, label = parsed
        amount = int(match.group(1))
        total += amount * seconds
        parts.append(f"{amount} {label}")
        last_was_hours = seconds == 3600
        end = match.end()

    if parts and last_was_hours:
        tail = re.match(r"\s*(\d{1,2})(?![\w])", text[end:])
        if tail and 0 < int(tail.group(1)) < 60:
            minutes = int(tail.group(1))
            total += minutes * 60
            parts.append(f"{minutes} мин.")
            end += tail.end()

    if not parts or total <= 0:
        return None
    return timedelta(seconds=total), " ".join(parts), end


# Особые словесные формы, которые нельзя получить из пар «число + единица».
_DURATION_WORDS: tuple[tuple[str, timedelta, str], ...] = (
    (r"полчаса|пол-часа", timedelta(minutes=30), "30 мин."),
    (r"час|один час", timedelta(hours=1), "1 ч."),
    (r"два часа", timedelta(hours=2), "2 ч."),
    (r"три часа", timedelta(hours=3), "3 ч."),
    (r"день|сутки|один день", timedelta(days=1), "1 д."),
    (r"два дня", timedelta(days=2), "2 д."),
    (r"три дня", timedelta(days=3), "3 д."),
    (r"неделя|неделю|одну неделю", timedelta(days=7), "7 д."),
    (r"месяц|один месяц", timedelta(days=30), "30 д."),
    (r"год|один год", timedelta(days=365), "365 д."),
)


def _match_duration_prefix(text: str) -> tuple[timedelta | None, str, int] | None:
    """Ищет срок наказания в начале строки.

    Возвращает (длительность, подпись, сколько символов занято) либо None,
    если строка не начинается со срока. ``None`` вместо длительности означает
    «навсегда».
    """
    s = text.strip()
    if not s:
        return None

    if re.match(r"^(?:навсегда|вечно|бессрочно)\b", s, re.IGNORECASE):
        match = re.match(r"^(?:навсегда|вечно|бессрочно)\b", s, re.IGNORECASE)
        return None, "навсегда", match.end()

    # «час 2», «час-два», «1-2 часа» — разговорные формы на два часа.
    legacy = re.match(
        r"^(?:час\s*[-–—]\s*(?:два|2)|час\s+(?:два|2)|1\s*[-–—]\s*2\s*(?:ч|часа|часов))\b",
        s,
        re.IGNORECASE,
    )
    if legacy:
        return timedelta(hours=2), "2 ч.", legacy.end()

    # «час 30» — один час тридцать минут.
    legacy = re.match(r"^(?:один\s+)?час\s*(\d{1,2})\s*(?:м|мин|минут\w*|m)?\b", s, re.IGNORECASE)
    if legacy:
        minutes = int(legacy.group(1))
        return timedelta(hours=1, minutes=minutes), f"1 ч. {minutes} мин.", legacy.end()

    compound = _match_compound_duration(s)
    if compound:
        return compound

    for pattern, duration, label in _DURATION_WORDS:
        match = re.match(rf"^(?:{pattern})\b", s, re.IGNORECASE)
        if match:
            return duration, label, match.end()

    # Голое число трактуем как минуты: «мут 67» = 67 минут.
    bare = re.match(r"^(\d+)\b", s)
    if bare:
        minutes = int(bare.group(1))
        if minutes > 0:
            return timedelta(minutes=minutes), _format_duration_ru(timedelta(minutes=minutes)), bare.end()

    return None


def parse_duration_string(duration_str: str) -> timedelta:
    """Парсит строку со сроком в timedelta.

    Поддерживает секунды, минуты, часы, дни, недели, месяцы и годы в любых
    сочетаниях: «67000 сек», «30м», «1ч 30м», «2 часа», «на час 2», «1 сутки»,
    «2 недели», «1 месяц», «год», «навсегда», а также голое число (минуты).
    """
    s = duration_str.strip()
    if s.lower().startswith("на "):
        s = s[3:].strip()
    if not s:
        raise ModerationError("Пустой срок наказания")

    matched = _match_duration_prefix(s)
    if matched is not None:
        duration, _, consumed = matched
        if s[consumed:].strip(" ,"):
            raise ModerationError(
                "Неверный формат времени. Примеры: 67000 сек, 30м, 1ч 30м, 2 часа, 2 недели, 1д"
            )
        if duration is None:
            return timedelta(days=3650)
        if duration.total_seconds() <= 0:
            raise ModerationError("Срок должен быть больше 0")
        return duration

    raise ModerationError(
        "Неверный формат времени. Примеры: 67000 сек, 30м, 1ч 30м, 2 часа, 2 недели, 1д"
    )



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

    # Убираем упоминание пользователя или цифровой ID из начала аргументов.
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

    matched = _match_duration_prefix(s_work)
    if matched is not None:
        duration, display, consumed = matched
        reason = s_work[consumed:].strip()
        reason = re.sub(r"^[,\-–—:]\s*", "", reason).strip()
        if target_id and reason.startswith(str(target_id)):
            reason = reason[len(str(target_id)):].strip()
        elif reason.startswith("@"):
            parts = reason.split(maxsplit=1)
            reason = parts[1] if len(parts) > 1 else ""
        return duration, display, reason or "Нарушение порядка"

    reason = rest
    if leading_na:
        reason = "на " + s_work
    clean_words = [
        w for w in reason.split()
        if not w.startswith("@") and not (target_id and w == str(target_id))
    ]
    final_reason = " ".join(clean_words).strip() or "Нарушение порядка"
    default_disp = (
        "30 мин."
        if default_duration == timedelta(minutes=30)
        else ("навсегда" if default_duration is None else f"{int(default_duration.total_seconds() // 60)} мин.")
    )
    return default_duration, default_disp, final_reason


# assigned_by для ролей, синхронизированных из Telegram автоматически.
# Позволяет отличать их от назначенных вручную и снимать при потере прав.
AUTO_SYNC_MARKER = 0

# Уровень, который получает администратор Telegram без ручного назначения:
# может мутить, выдавать варны, банить и настраивать чат, но не может
# раздавать роли (уровень 3 < 4) — иерархия остаётся управляемой создателем.
TELEGRAM_ADMIN_LEVEL = 3

# Кэш «не админ» по (chat_id, user_id). Без него на каждое сообщение каждого
# участника уходил бы bot.get_chat_member — Telegram отвечает 429 на такой поток.
_NON_ADMIN_TTL = timedelta(minutes=10)
_non_admin_cache: dict[tuple[int, int], datetime] = {}


def _is_cached_non_admin(chat_id: int, user_id: int, now: datetime) -> bool:
    ts = _non_admin_cache.get((chat_id, user_id))
    if ts is None:
        return False
    if now - ts > _NON_ADMIN_TTL:
        _non_admin_cache.pop((chat_id, user_id), None)
        return False
    return True


def _mark_non_admin(chat_id: int, user_id: int, now: datetime) -> None:
    if len(_non_admin_cache) > 10000:
        _non_admin_cache.clear()
    _non_admin_cache[(chat_id, user_id)] = now


def invalidate_admin_cache(chat_id: int, user_id: int) -> None:
    """Сбрасывает кэш после смены прав, чтобы изменения применились сразу."""
    _non_admin_cache.pop((chat_id, user_id), None)


async def _apply_admin_level(
    session: AsyncSession, *, chat_id: int, user_id: int, level: int, assigned_by: int
) -> None:
    stmt = insert(ChatAdmin).values(
        chat_id=chat_id,
        user_id=user_id,
        level=level,
        assigned_by=assigned_by,
        is_active=True,
    ).on_conflict_do_update(
        index_elements=[ChatAdmin.chat_id, ChatAdmin.user_id],
        set_={"level": level, "is_active": True},
    )
    await session.execute(stmt)


async def get_user_admin_level(
    session: AsyncSession, *, chat_id: int, user_id: int, bot=None
) -> int:
    """Возвращает локальный уровень прав пользователя в чате (от 1 до 5).

    Если записи в БД нет, права запрашиваются у Telegram и кэшируются:
    создатель группы получает уровень 5, администратор — уровень 3.
    Без этого модераторы группы не могли пользоваться «мут», «варн», «бан»
    и другими командами: они всегда считались обычными игроками.
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

    if not bot:
        return 1

    now = datetime.now(timezone.utc)
    if _is_cached_non_admin(chat_id, user_id, now):
        return 1

    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        status = getattr(member, "status", None)
        if status in ("creator", "owner"):
            await _apply_admin_level(
                session, chat_id=chat_id, user_id=user_id, level=5, assigned_by=AUTO_SYNC_MARKER
            )
            return 5
        if status == "administrator":
            await _apply_admin_level(
                session,
                chat_id=chat_id,
                user_id=user_id,
                level=TELEGRAM_ADMIN_LEVEL,
                assigned_by=AUTO_SYNC_MARKER,
            )
            return TELEGRAM_ADMIN_LEVEL
    except Exception:
        return 1

    _mark_non_admin(chat_id, user_id, now)
    return 1


async def sync_chat_creator_and_admins(session: AsyncSession, *, chat_id: int, bot) -> int | None:
    """Синхронизирует состав администрации чата с Telegram.

    Создатель группы получает уровень 5, остальные администраторы — уровень 3.
    Автоматически выданные роли снимаются, когда пользователь перестаёт быть
    администратором в Telegram; назначенные вручную (assigned_by != 0) не трогаем.
    Возвращает telegram_id создателя чата, если найден.
    """
    if not bot:
        return None
    try:
        admins = await bot.get_chat_administrators(chat_id=chat_id)
    except Exception:
        return None

    creator_id: int | None = None
    actual_levels: dict[int, int] = {}
    for adm in admins:
        user = getattr(adm, "user", None)
        if not user or getattr(user, "is_bot", False):
            continue
        status = getattr(adm, "status", None)
        if status in ("creator", "owner"):
            creator_id = user.id
            actual_levels[user.id] = 5
        elif status == "administrator":
            actual_levels[user.id] = TELEGRAM_ADMIN_LEVEL
        else:
            continue

        p_stmt = insert(Player).values(
            telegram_id=user.id,
            first_name=getattr(user, "first_name", None) or "Админ",
            username=getattr(user, "username", None),
        ).on_conflict_do_nothing(index_elements=[Player.telegram_id])
        await session.execute(p_stmt)
        await _apply_admin_level(
            session,
            chat_id=chat_id,
            user_id=user.id,
            level=actual_levels[user.id],
            assigned_by=AUTO_SYNC_MARKER,
        )

    stale = await session.scalars(
        select(ChatAdmin).where(
            ChatAdmin.chat_id == chat_id,
            ChatAdmin.is_active.is_(True),
            ChatAdmin.assigned_by == AUTO_SYNC_MARKER,
        )
    )
    for row in stale.all():
        if row.user_id not in actual_levels:
            row.is_active = False
    await session.flush()

    return creator_id


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

    invalidate_admin_cache(chat_id, target_id)

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
    system: bool = False,
) -> ModerationCase:
    """Выдача мута с проверкой прав.

    system=True — автоматическая модерация (антиспам): проверки прав пропускаются,
    moderator_id фиксируется как инициатор-система.
    """
    if system:
        await ensure_player_and_chat_exist(session, chat_id=chat_id, user_ids=[target_id], bot=bot)
    else:
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
    system: bool = False,
) -> tuple[ModerationCase, int]:
    """Выдача варна (срок 30 дней, без авто-санкций). Возвращает (case, активных_варнов).

    system=True — автоматическая модерация (антимат/антиспам): проверки прав
    пропускаются, moderator_id фиксируется как инициатор-система.
    """
    if not system:
        await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=2, bot=bot)
    else:
        await ensure_player_and_chat_exist(session, chat_id=chat_id, user_ids=[target_id], bot=bot)

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


@dataclass(frozen=True, slots=True)
class WarnEscalation:
    """Результат автоматической эскалации наказания по количеству варнов."""
    triggered: bool
    action: str  # mute | ban | kick | none
    duration: timedelta | None
    warn_count: int
    message: str


async def apply_warn_escalation(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    warn_count: int,
    settings: dict | None = None,
    bot=None,
) -> WarnEscalation:
    """Применяет авто-наказание, когда число варнов достигло настроенного лимита.

    Настройки чата:
      warn_limit        — сколько варнов до авто-наказания (0 — отключено);
      warn_action       — mute | ban | kick | none;
      warn_duration_seconds — срок авто-мута/бана (0 — бессрочный бан);
      warn_escalate_ban — при двойном лимите выдать бессрочный бан.

    Действие выполняется системно (moderator_id=bot.id), проверки прав пропускаются.
    """
    settings = settings or {}
    limit = int(settings.get("warn_limit", 5) or 0)
    if limit <= 0 or warn_count < limit:
        return WarnEscalation(False, "none", None, warn_count, "")

    action = str(settings.get("warn_action", "mute")).lower()
    duration_seconds = int(settings.get("warn_duration_seconds", 3600) or 0)
    escalate_ban = bool(settings.get("warn_escalate_ban", True))

    # При достижении двойного лимита — бессрочный бан (если включено).
    if escalate_ban and warn_count >= limit * 2:
        action = "ban"
        duration_seconds = 0

    system_moderator = getattr(bot, "id", 0) or 0
    duration = timedelta(seconds=duration_seconds) if duration_seconds > 0 else None

    if action == "ban":
        await record_ban_or_kick(
            session,
            chat_id=chat_id,
            moderator_id=system_moderator,
            target_id=target_id,
            action="ban",
            reason=f"Авто-наказание: достигнут лимит предупреждений ({warn_count}/{limit})",
            duration=duration,
            bot=bot,
            system=True,
        )
        msg = f"⛔️ Достигнут лимит предупреждений ({warn_count}/{limit}) — выдана блокировка."
    elif action == "kick":
        await record_ban_or_kick(
            session,
            chat_id=chat_id,
            moderator_id=system_moderator,
            target_id=target_id,
            action="kick",
            reason=f"Авто-наказание: достигнут лимит предупреждений ({warn_count}/{limit})",
            bot=bot,
            system=True,
        )
        msg = f"👢 Достигнут лимит предупреждений ({warn_count}/{limit}) — участник исключён."
    elif action == "mute":
        await issue_mute(
            session,
            chat_id=chat_id,
            moderator_id=system_moderator,
            target_id=target_id,
            duration=duration or timedelta(hours=1),
            reason=f"Авто-наказание: достигнут лимит предупреждений ({warn_count}/{limit})",
            bot=bot,
            system=True,
        )
        msg = f"🔇 Достигнут лимит предупреждений ({warn_count}/{limit}) — выдан мут."
    else:
        return WarnEscalation(False, "none", None, warn_count, "")

    return WarnEscalation(True, action, duration, warn_count, msg)


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


async def get_user_moderation_history(
    session: AsyncSession,
    *,
    chat_id: int,
    target_id: int,
    limit: int = 20,
) -> list[ModerationCase]:
    """Возвращает историю наказаний пользователя в чате (все действия, свежие сверху)."""
    limit = max(1, min(int(limit), 50))
    cases = (
        await session.scalars(
            select(ModerationCase)
            .where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action.in_(("warn", "mute", "ban", "kick")),
            )
            .order_by(desc(ModerationCase.created_at))
            .limit(limit)
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
    system: bool = False,
) -> ModerationCase:
    """Фиксация бана или кика (требуется ур. 3+ для бана/кика).
    Если указан duration, рассчитывается expires_at для временного бана.
    system=True — автоматическая модерация (антиспам), проверки прав пропускаются.
    """
    if system:
        await ensure_player_and_chat_exist(session, chat_id=chat_id, user_ids=[target_id], bot=bot)
    else:
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


async def revoke_ban(
    session: AsyncSession,
    *,
    chat_id: int,
    moderator_id: int,
    target_id: int,
    bot=None,
) -> int:
    """Снимает активные локальные баны пользователя в чате (деактивирует кейсы).

    Возвращает число снятых кейсов. Требуется уровень 3+ (как и для бана).
    """
    await check_can_moderate(session, chat_id=chat_id, moderator_id=moderator_id, target_id=target_id, min_required_level=3, bot=bot)

    cases = (
        await session.scalars(
            select(ModerationCase).where(
                ModerationCase.chat_id == chat_id,
                ModerationCase.target_id == target_id,
                ModerationCase.action == "ban",
                ModerationCase.is_active.is_(True),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for c in cases:
        c.is_active = False
        c.revoked_by = moderator_id
        c.revoked_at = now
    await session.flush()
    return len(cases)


async def expire_moderation_cases(
    session: AsyncSession,
    *,
    bot=None,
    now: datetime | None = None,
) -> int:
    """Автоматически снимает истёкшие муты и временные баны.

    Деактивирует ModerationCase с истёкшим expires_at и, если передан bot,
    снимает ограничения/бан в самом Telegram. Возвращает число снятых случаев.
    """
    now = now or datetime.now(timezone.utc)
    cases = (
        await session.scalars(
            select(ModerationCase).where(
                ModerationCase.is_active.is_(True),
                ModerationCase.expires_at.is_not(None),
                ModerationCase.expires_at <= now,
                ModerationCase.action.in_(("mute", "ban")),
            )
        )
    ).all()

    if not cases:
        return 0

    from aiogram.types import ChatPermissions

    full_permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )

    expired_count = 0
    for case in cases:
        case.is_active = False
        case.revoked_at = now
        case.revoked_by = None
        expired_count += 1

        log_entry = AdminActionLog(
            chat_id=case.chat_id,
            actor_id=None,
            target_id=case.target_id,
            action="auto_expire",
            reason="Автоматическое снятие по истечении срока",
            details={"original_action": case.action, "case_id": case.id},
        )
        session.add(log_entry)

        if bot:
            try:
                if case.action == "mute":
                    await bot.restrict_chat_member(
                        chat_id=case.chat_id,
                        user_id=case.target_id,
                        permissions=full_permissions,
                    )
                elif case.action == "ban":
                    await bot.unban_chat_member(
                        chat_id=case.chat_id,
                        user_id=case.target_id,
                        only_if_banned=True,
                    )
            except Exception:
                pass

    await session.flush()
    return expired_count


async def is_link_allowed(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
) -> tuple[bool, str | None]:
    """Проверяет сообщение на запрещенные ссылки.

    Возвращает (разрешено, найденная_ссылка).
    Упоминания пользователей (@username) разрешены всегда и не считаются ссылками.
    Если ссылка есть, но её домен в whitelist этого чата — разрешено.
    """
    # Упоминания людей всегда разрешены — исключаем их из проверки.
    sanitized = re.sub(r"(?<![\w/])@[A-Za-z0-9_]{3,}", " ", text)

    matches = URL_REGEX.findall(sanitized)
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
    bot=None,
) -> str:
    """Добавляет домен в белый список ссылок (ур. 5)."""
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id, bot=bot)
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
    bot=None,
) -> bool:
    """Удаляет домен из белого списка ссылок (ур. 5)."""
    level = await get_user_admin_level(session, chat_id=chat_id, user_id=admin_id, bot=bot)
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
