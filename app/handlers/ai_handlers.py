"""Обработчик обращений к Grok AI (Fable AI):
- Реагирует на обращение "фабле", "fable", "фабля" (в начале сообщения или в тексте).
- Реагирует на реплай к сообщению самого бота.
"""
from __future__ import annotations

import re
from aiogram import Router, types, Bot, F
from aiogram.enums import ChatAction

from app.services.ai_service import ask_grok

router = Router(name="ai_router")

AI_TRIGGER_PATTERN = re.compile(r"\b(фабле|fable|фабля|фаблик)\b", re.IGNORECASE)


def _should_trigger_ai(message: types.Message, bot_user_id: int) -> bool:
    text = message.text or message.caption or ""
    if not text:
        return False

    # 1. Проверяем наличие ключевого слова "фабле"
    if AI_TRIGGER_PATTERN.search(text):
        return True

    # 2. Проверяем, является ли это ответом на сообщение нашего бота
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == bot_user_id:
            # Игнорируем технические команды, если они идут ответом
            first_word = text.strip().split()[0].lower() if text.strip() else ""
            ignored_prefixes = {"перевод", "мут", "размут", "варн", "кик", "бан", "разбан", "дуэль", "реп"}
            if first_word not in ignored_prefixes:
                return True

    return False


@router.message(F.text | F.caption)
async def handle_ai_query(message: types.Message, bot: Bot):
    bot_info = await bot.get_me()
    if not _should_trigger_ai(message, bot_info.id):
        return

    text = message.text or message.caption or ""

    # Убираем имя бота из самого промпта для более чистого контекста нейросети
    cleaned_prompt = AI_TRIGGER_PATTERN.sub("", text).strip(" ,:;!?\n")
    if not cleaned_prompt:
        cleaned_prompt = text.strip()

    user_name = message.from_user.first_name if message.from_user else "Пользователь"

    try:
        await bot.send_chat_action(chat_id=message.chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    answer = await ask_grok(cleaned_prompt, user_name=user_name)
    await message.reply(answer)
