"""ИИ-модерация спам-ботов и рекламы в чатах Fable.

Слои проверки (от дешёвого к дорогому):
1. Быстрые эвристики: реклама, инвайт-ссылки, крипто-скам, «заработок в ЛС»,
   массовые упоминания, повторы, капс — работают без внешнего API.
2. Вердикт LLM (Grok/Groq через ai_service) — включается только для сообщений,
   которые эвристики сочли подозрительными, чтобы не тратить токены впустую.

По умолчанию выключено: администратор включает `ии модерация вкл`.
Действие настраивается: `ии действие удалить|мут|бан`.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ai_service

logger = logging.getLogger(__name__)

DEFAULT_AI_MODERATION: dict[str, object] = {
    "ai_moderation_enabled": False,
    "ai_moderation_action": "delete",
    "ai_moderation_duration_seconds": 3600,
    "ai_moderation_use_llm": True,
    "ai_moderation_min_score": 0.6,
}

# Признаки рекламы и скама: (регулярка, вес, пояснение для лога).
_SPAM_SIGNALS: tuple[tuple[str, float, str], ...] = (
    (r"t\.me/(?:joinchat/|\+)?[A-Za-z0-9_\-]{6,}", 0.75, "инвайт-ссылка на другой чат"),
    (r"@[A-Za-z0-9_]{4,}\s*(?:пиши|пишите|жми|заходи|переходи)", 0.6, "призыв писать в ЛС"),
    (r"(?:заработ|заработок|подработ|прибыл\w*)\s*(?:от|до)?\s*\d+\s*(?:к|тыс|000|руб|₽|\$)", 0.6, "обещание дохода"),
    (r"(?:крипт\w*|биткоин|bitcoin|btc|usdt|toncoin|ton\b|p2p)\s*(?:раздач|раздача|бонус|airdrop|дроп)", 0.7, "крипто-раздача"),
    (r"(?:airdrop|giveaway|free\s*nft|бесплатн\w*\s*(?:нфт|крипт|токен))", 0.65, "airdrop/скам-раздача"),
    (r"(?:казино|букмекер|ставки\s*на\s*спорт|1xbet|1win|vavada|joycasino)", 0.7, "реклама казино"),
    (r"(?:инвест\w*|трейд\w*|сигнал\w*)\s*(?:без\s*риск|гарант\w*|\+\d+\s*%)", 0.65, "«безрисковые» инвестиции"),
    (r"(?:подпишись|подписка)\s*(?:на\s*канал|взаимн\w*|взаимка)", 0.5, "накрутка подписок"),
    (r"(?:зарплат\w*|работа)\s*(?:на\s*дому|онлайн)\s*(?:без\s*опыт\w*)?\s*\d+", 0.5, "«работа на дому»"),
    (r"(?:промокод|промо-код)\s*[:=]?\s*[A-Z0-9]{6,}", 0.45, "рассылка промокодов"),
    (r"(?:выиграл\w*|выиграл|получил\w*)\s*(?:\d+\s*)?(?:₽|руб|\$|usdt|ton)", 0.55, "скрин-скам о выигрыше"),
    (r"t\.me/[A-Za-z0-9_]*bot\?start=", 0.5, "диплинк на сторонний бот"),
)

_MENTION_RE = re.compile(r"@[A-Za-z0-9_]{3,}")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AiModerationVerdict:
    is_spam: bool
    score: float
    reason: str
    source: str


def _heuristic_score(text: str) -> tuple[float, list[str]]:
    """Считает «спамность» текста по словарю признаков.

    Итоговый вес — максимум по сработавшим признакам плюс небольшой бонус за
    каждый дополнительный, но не больше 1.0. Так одно явное совпадение уже
    даёт высокий скор, а набор слабых — тоже поднимает его.
    """
    lowered = (text or "").lower()
    if not lowered:
        return 0.0, []

    hits: list[tuple[float, str]] = []
    for pattern, weight, label in _SPAM_SIGNALS:
        if re.search(pattern, lowered, re.IGNORECASE):
            hits.append((weight, label))

    if not hits:
        return 0.0, []

    hits.sort(reverse=True)
    score = hits[0][0] + 0.08 * (len(hits) - 1)

    # Массовые упоминания и обилие ссылок — типичный признак рассылки.
    if len(_MENTION_RE.findall(text)) >= 5:
        score += 0.2
        hits.append((0.2, "массовые упоминания"))
    if len(_URL_RE.findall(text)) >= 3:
        score += 0.15
        hits.append((0.15, "много ссылок"))

    return min(1.0, round(score, 3)), [label for _, label in hits]


_LLM_PROMPT = (
    "Ты — антиспам-модератор Telegram-чата. Оцени, является ли сообщение "
    "рекламой, спамом или скамом. Ответь ОДНОЙ строкой в формате:\n"
    "SPAM <уверенность 0..1> <короткая причина на русском>\n"
    "или\n"
    "OK 0 <короткая причина на русском>"
)


def _parse_llm_verdict(raw: str) -> tuple[bool, float, str] | None:
    """Разбирает ответ модели вида «SPAM 0.9 реклама казино» или «BOT 0.9 спам-ник»."""
    if not raw:
        return None
    first = raw.strip().splitlines()[0].strip()
    match = re.match(
        r"^(SPAM|OK|NOT\s*SPAM|BOT|HUMAN)\b\s*([01](?:[.,]\d+)?)?\s*(.*)$", first, re.IGNORECASE
    )
    if not match:
        return None
    label = match.group(1).upper()
    is_spam = label in ("SPAM", "BOT")
    try:
        score = float((match.group(2) or ("0.8" if is_spam else "0")).replace(",", "."))
    except ValueError:
        score = 0.8 if is_spam else 0.0
    return is_spam, max(0.0, min(1.0, score)), (match.group(3) or "").strip()


async def classify_message(
    text: str,
    *,
    settings: dict | None = None,
    user_name: str | None = None,
) -> AiModerationVerdict:
    """Определяет, спам ли сообщение.

    Эвристики решают быстрые случаи сами; LLM подключается только для
    подозрительных сообщений (0 < score < порог), чтобы не тратить запросы.
    """
    settings = settings or {}
    threshold = float(settings.get("ai_moderation_min_score", DEFAULT_AI_MODERATION["ai_moderation_min_score"]) or 0.6)

    score, labels = _heuristic_score(text)
    if score >= threshold:
        return AiModerationVerdict(True, score, ", ".join(labels) or "спам-признаки", "heuristic")
    if score <= 0:
        return AiModerationVerdict(False, 0.0, "", "heuristic")

    if not settings.get("ai_moderation_use_llm", True):
        return AiModerationVerdict(False, score, ", ".join(labels), "heuristic")

    prompt = f"{_LLM_PROMPT}\n\nСообщение:\n{text[:1500]}"
    try:
        raw = await ai_service.ask_grok(prompt, user_name=user_name)
    except Exception as e:  # noqa: BLE001 — модерация не должна падать из-за ИИ
        logger.warning("ИИ-модерация: запрос к модели не удался (%s)", e)
        return AiModerationVerdict(False, score, ", ".join(labels), "heuristic")

    parsed = _parse_llm_verdict(raw)
    if parsed is None:
        return AiModerationVerdict(False, score, ", ".join(labels), "heuristic")

    is_spam, llm_score, reason = parsed
    if is_spam and llm_score >= threshold:
        return AiModerationVerdict(True, max(score, llm_score), reason or ", ".join(labels), "llm")
    return AiModerationVerdict(False, score, reason, "llm")


def looks_like_bot_name(first_name: str | None, username: str | None) -> str | None:
    """Эвристика «имя как у спам-бота»: причина или None.

    Ловит типичные ники вроде «Crypto Signals», «Free Money Bot», «Airdrop 🚀»,
    которые массово заходят в чаты вместе с рекламой.
    """
    blob = f"{first_name or ''} {username or ''}".lower()
    if not blob.strip():
        return None
    patterns = (
        (r"(?:crypto|bitcoin|btc|usdt|ton|nft|airdrop)", "крипто-ник"),
        (r"(?:casino|bet|betting|1xbet|1win|vavada)", "казино-ник"),
        (r"(?:earn|money|profit|invest|trading|signal)", "«заработок» в нике"),
        (r"(?:free|giveaway|bonus|promo)", "обещание бонусов в нике"),
        (r"(?:support|admin|moderator|fable)\w*\s*\d{3,}", "маскировка под администрацию"),
        (r"(?:sex|porn|xxx|dating|знакомств)", "взрослый спам в нике"),
    )
    for pattern, reason in patterns:
        if re.search(pattern, blob):
            return reason
    return None


_PROFILE_PROMPT = (
    "Ты — антиспам-модератор Telegram-чата. Тебе дают профиль аккаунта, "
    "который только что вошёл в чат. Реши, похож ли он на рекламного бота. "
    "Признаки: рекламный или бессмысленный ник, ник с обещанием денег/крипты/"
    "казино, ник, маскирующийся под администрацию, ник из случайных символов, "
    "аккаунт без username и без фамилии с «шумным» именем.\n"
    "Ответь ОДНОЙ строкой:\n"
    "BOT <уверенность 0..1> <короткая причина на русском>\n"
    "или\n"
    "HUMAN 0 <короткая причина на русском>"
)


def _profile_blob(first_name: str | None, last_name: str | None, username: str | None) -> str:
    return " ".join(part for part in (first_name, last_name, username) if part)


def looks_like_random_handle(first_name: str | None, username: str | None) -> str | None:
    """Ник из случайных символов/цифр — типичный автогенерируемый аккаунт.

    Telegram-боты массово регистрируются с никами вида «xQ7fK2pL9», «user8271364».
    Живые люди такие ники почти не выбирают.
    """
    handle = (username or "").strip().lstrip("@")
    if not handle:
        return None
    digits = sum(ch.isdigit() for ch in handle)
    letters = [ch for ch in handle if ch.isalpha()]
    mixed_case = bool(re.search(r"[a-z][A-Z]|[A-Z][a-z][A-Z]", handle))

    # «user8271364»: много цифр в длинном нике. Короткие живые ники вроде
    # «ivan2024» сюда не попадают — там цифр меньше и ник короче.
    if digits >= 5 and len(handle) >= 8 and digits / len(handle) >= 0.4:
        return "ник из цифр"

    # Мало гласных — ник сгенерирован. Требуем ещё цифры или рваный регистр,
    # иначе под правило попадают реальные имена вроде «dmitry» или «krzysztof».
    if len(letters) >= 6 and (digits >= 2 or mixed_case):
        vowels = sum(ch.lower() in "aeiouyаеёиоуыэюя" for ch in letters)
        if vowels / len(letters) < 0.3:
            return "ник без гласных (автогенерация)"
    return None


async def classify_join_profile(
    *,
    first_name: str | None,
    last_name: str | None = None,
    username: str | None = None,
    is_bot: bool = False,
    settings: dict | None = None,
) -> "AiModerationVerdict":
    """Оценивает профиль нового участника: бот или живой человек.

    Порядок как у classify_message: сначала бесплатные эвристики, LLM — только
    если эвристики не дали однозначного ответа и включён ai_moderation_use_llm.
    """
    settings = settings or {}
    threshold = float(
        settings.get("ai_moderation_min_score", DEFAULT_AI_MODERATION["ai_moderation_min_score"]) or 0.6
    )

    if is_bot:
        return AiModerationVerdict(True, 1.0, "аккаунт Telegram-бота", "heuristic")

    reasons: list[str] = []
    score = 0.0
    spam_nick = looks_like_bot_name(first_name, username)
    if spam_nick:
        score = 0.8
        reasons.append(f"рекламный ник: {spam_nick}")
    random_nick = looks_like_random_handle(first_name, username)
    if random_nick:
        score = max(score, 0.7)
        reasons.append(random_nick)
    if score >= threshold:
        return AiModerationVerdict(True, score, ", ".join(reasons), "heuristic")

    if not settings.get("ai_moderation_use_llm", True):
        return AiModerationVerdict(False, score, ", ".join(reasons), "heuristic")

    profile = _profile_blob(first_name, last_name, username) or "—"
    try:
        raw = await ai_service.ask_grok(f"{_PROFILE_PROMPT}\n\nПрофиль: {profile}")
    except Exception as e:  # noqa: BLE001 — модерация не должна падать из-за ИИ
        logger.warning("ИИ-проверка профиля не удалась (%s)", e)
        return AiModerationVerdict(False, score, ", ".join(reasons), "heuristic")

    parsed = _parse_llm_verdict(raw)
    if parsed is None:
        return AiModerationVerdict(False, score, ", ".join(reasons), "heuristic")
    is_spam, llm_score, reason = parsed
    if is_spam and llm_score >= threshold:
        return AiModerationVerdict(True, max(score, llm_score), reason or ", ".join(reasons), "llm")
    return AiModerationVerdict(False, score, reason, "llm")


def ai_moderation_status_line(settings: dict | None) -> str:
    """Строка состояния ИИ-модерации для панели настроек."""
    settings = settings or {}
    if not settings.get("ai_moderation_enabled", DEFAULT_AI_MODERATION["ai_moderation_enabled"]):
        return "🤖 ИИ-модерация: <b>Выкл</b>"
    action = settings.get("ai_moderation_action", DEFAULT_AI_MODERATION["ai_moderation_action"])
    return f"🤖 ИИ-модерация: <b>Вкл</b> ({action})"


async def cleanup_expired_captchas(session: AsyncSession, *, bot=None) -> int:
    """Помечает истёкшие капчи как expired и снимает ограничения с пользователей.

    Без этого «висящие» капчи блокировали сообщения участников бесконечно,
    если человек не нажал кнопку.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db.models import CaptchaChallenge

    now = datetime.now(timezone.utc)
    rows = (
        await session.scalars(
            select(CaptchaChallenge).where(
                CaptchaChallenge.status == "pending",
                CaptchaChallenge.expires_at <= now,
            )
        )
    ).all()
    for row in rows:
        row.status = "expired"
    await session.flush()
    return len(rows)
