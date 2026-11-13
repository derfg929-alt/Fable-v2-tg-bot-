"""Обработчик обращений к Grok AI (Fable AI):
- Реагирует на обращение "фабле", "fable", "фабля", "фаблик", "грок", "grok" (в начале сообщения или в тексте).
- Реагирует на реплай к сообщению самого бота.
- Реагирует на прямые обращения/вопросы в личных сообщениях (ЛС) с ботом.
- Безопасная отправка с разделением длинных сообщений (>4000 знаков) и фоллбеком parse_mode.
"""
from __future__ import annotations

import logging
import re
from aiogram import Router, types, Bot, F
from aiogram.enums import ChatAction, ChatType

from app.db.session import session_scope
from app.services.ai_service import ask_grok
from app.services.ai_memory_service import (
    build_context_string,
    clear_history,
    is_forget_request,
    load_history,
    remember_exchange,
)

logger = logging.getLogger("fable.ai")
router = Router(name="ai_router")

AI_TRIGGER_PATTERN = re.compile(r"\b(фабле|fable|фабля|фаблик|грок|grok)\b", re.IGNORECASE)

KNOWN_COMMANDS = {
    "профиль", "банк", "биржа", "рынок", "игры", "клан", "кланы", "топ", "топы",
    "бонус", "бизнес", "ферма", "понель", "панель", "админка", "настройки",
    "помощь", "команды", "баланс", "депозит", "снять", "казино", "кости",
    "краш", "тир", "слоты", "портфель", "акции", "купить", "продать",
    "перевод", "выдать", "забрать", "правила", "квест", "квесты", "достижения",
    "старт", "start", "хелп", "help", "инфо", "репутация", "реп",
    "рейтинг", "шахта", "инвентарь", "работа", "подработка", "снаряжение",
    "улучшить", "машины", "авто", "гараж", "недвижимость", "магазин",
    "кейсы", "кейс", "монетка", "сундук", "рыбалка", "охота", "рулетка",
    "ник", "шар", "реши", "брак", "развод", "дуэль", "космодром", "майнинг",
    "мут", "размут", "варн", "варны", "преды", "кик", "бан", "разбан",
    "чистка", "закрепить", "открепить", "заморозка", "слоумод", "репорт", "реф",
    "промо", "промокоды", "+промо", "антиспам", "антифлуд", "капча", "+капча",
    "-капча", "логи", "журнал", "админы", "заблокирован",
}


def _should_trigger_ai(message: types.Message, bot_user_id: int) -> bool:
    text = message.text or message.caption or ""
    if not text:
        return False

    stripped = text.strip()
    first_word = stripped.split()[0].lower() if stripped else ""

    # 1. Проверяем наличие ключевого слова "фабле" или "грок"
    if AI_TRIGGER_PATTERN.search(text):
        return True

    # 2. Проверяем, является ли это ответом на сообщение нашего бота
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_user_id:
            # Игнорируем технические команды, если они идут ответом
            ignored_prefixes = {"перевод", "мут", "размут", "варн", "кик", "бан", "разбан", "дуэль", "реп"}
            if first_word not in ignored_prefixes:
                return True

    # 3. В личных сообщениях с ботом (PM / ЛС) - если это не техническая команда бота
    if message.chat and message.chat.type == ChatType.PRIVATE:
        if stripped.startswith(("/", "!", ".")):
            return False
        if first_word in KNOWN_COMMANDS:
            return False
        return True

    return False


def _split_into_chunks(text: str, max_length: int = 4000) -> list[str]:
    """Разбивает длинный текст ответа нейросети на части для Telegram."""
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    current_text = text
    while current_text:
        if len(current_text) <= max_length:
            chunks.append(current_text)
            break
        split_idx = current_text.rfind("\n", 0, max_length)
        if split_idx <= 0:
            split_idx = current_text.rfind(" ", 0, max_length)
        if split_idx <= 0:
            split_idx = max_length
        chunk = current_text[:split_idx].strip()
        if chunk:
            chunks.append(chunk)
        current_text = current_text[split_idx:].strip()
    return chunks or [text]


async def _send_chunk(message: types.Message, chunk: str, is_first: bool = True) -> None:
    """Безопасная отправка фрагмента ответа с фоллбеком на чистый текст при ошибках разметки."""
    try:
        if is_first:
            await message.reply(chunk, parse_mode="HTML")
        else:
            await message.answer(chunk, parse_mode="HTML")
    except Exception as err:
        logger.debug("Не удалось отправить сообщение с HTML: %s. Отправка без parse_mode.", err)
        try:
            if is_first:
                await message.reply(chunk, parse_mode=None)
            else:
                await message.answer(chunk, parse_mode=None)
        except Exception:
            # Если reply не сработал (например, исходное сообщение удалили), отвечаем через answer
            await message.answer(chunk, parse_mode=None)


async def _send_ai_response(message: types.Message, answer: str) -> None:
    """Разбивает и безопасно отправляет полный ответ Grok пользователю."""
    chunks = _split_into_chunks(answer)
    for i, chunk in enumerate(chunks):
        await _send_chunk(message, chunk, is_first=(i == 0))


@router.message(F.text | F.caption)
async def handle_ai_query(message: types.Message, bot: Bot):
    # bot.id доступен без сетевого запроса getMe (в aiogram 3 токен парсится локально).
    if not _should_trigger_ai(message, bot.id):
        return

    text = message.text or message.caption or ""

    # Убираем имя бота / грока из самого промпта для более чистого контекста нейросети
    cleaned_prompt = AI_TRIGGER_PATTERN.sub("", text).strip(" ,:;!?\n")
    if not cleaned_prompt:
        cleaned_prompt = text.strip()

    user_name = message.from_user.first_name if message.from_user else "Пользователь"
    user_id = message.from_user.id if message.from_user else 0
    chat_id = message.chat.id if message.chat else user_id

    # Команда «забудь» стирает память диалога, не обращаясь к модели.
    if user_id and is_forget_request(text):
        async with session_scope() as session:
            await clear_history(session, user_id=user_id, chat_id=chat_id)
        await message.reply("🧠 Хорошо, я забыл нашу прошлую беседу. О чём поговорим?")
        return

    # Поднимаем контекст диалога из памяти (история для модели + выдержка для офлайн-движка).
    chat_history: list[dict[str, str]] = []
    context_str = ""
    if user_id:
        try:
            async with session_scope() as session:
                chat_history = await load_history(session, user_id=user_id, chat_id=chat_id, for_model=True)
                context_str = await build_context_string(session, user_id=user_id, chat_id=chat_id)
        except Exception as e:
            logger.debug("Не удалось загрузить память ИИ: %s", e)

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    try:
        answer = await ask_grok(
            cleaned_prompt,
            user_name=user_name,
            chat_history=chat_history or None,
            context=context_str or None,
        )
    except Exception as e:
        logger.exception("Ошибка при вызове Grok AI: %s", e)
        answer = "⚠️ Произошла ошибка при обработке запроса к Grok AI. Попробуйте еще раз позже."

    # Запоминаем обмен репликами, чтобы ИИ держал нить беседы.
    if user_id and answer and not answer.startswith("⚠️"):
        try:
            async with session_scope() as session:
                await remember_exchange(
                    session,
                    user_id=user_id,
                    chat_id=chat_id,
                    user_text=cleaned_prompt,
                    assistant_text=answer,
                )
        except Exception as e:
            logger.debug("Не удалось сохранить память ИИ: %s", e)

    await _send_ai_response(message, answer)
