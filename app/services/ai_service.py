"""Сервис взаимодействия с Grok AI (xAI) для бота Fable."""
from __future__ import annotations

import logging
from typing import Any
import aiohttp

from app.config import get_settings

logger = logging.getLogger("fable.ai")

SYSTEM_PROMPT = (
    "Ты — Fable (Фабле), умный, дружелюбный и харизматичный Telegram-бот и игровой компаньон. "
    "Ты помогаешь участникам чата, поддерживаешь интересную беседу, шутишь к месту и даешь точные, "
    "понятные и лаконичные ответы на русском языке. Не будь слишком навязчивым или занудным. "
    "Если тебя спрашивают о командах Fable, напоминай, что основные команды доступны по слову 'команды' "
    "(профиль, банк, биржа, игры, кланы, топы)."
)


async def ask_grok(
    prompt: str,
    *,
    user_name: str | None = None,
    chat_history: list[dict[str, str]] | None = None,
) -> str:
    """Отправляет запрос к Grok API (xAI) и возвращает сгенерированный ответ."""
    settings = get_settings()
    api_key = settings.grok_api_key_value

    if not api_key:
        logger.warning("GROK_API_KEY не настроен в конфигурации.")
        return (
            "Привет! Я Фабле 🌟 Мой модуль искусственного интеллекта на базе Grok готов к работе, "
            "но администратор еще не указал API-ключ (переменная GROK_API_KEY в .env)."
        )

    url = settings.grok_api_url
    model = settings.grok_model

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]

    if chat_history:
        messages.extend(chat_history)

    user_msg_content = prompt
    if user_name:
        user_msg_content = f"[{user_name}]: {prompt}"

    messages.append({"role": "user", "content": user_msg_content})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
        "max_tokens": 1024,
    }

    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        reply_text = choices[0]["message"].get("content", "").strip()
                        if reply_text:
                            return reply_text
                    return "Фабле на связи, но ответ от нейросети оказался пустым 🤔"
                else:
                    error_text = await resp.text()
                    logger.error("Ошибка Grok API HTTP %s: %s", resp.status, error_text)
                    return f"⚠️ Ошибка обращения к Grok AI (код {resp.status}). Попробуйте позже."
    except aiohttp.ClientError as e:
        logger.error("Сетевая ошибка при запросе к Grok AI: %s", e)
        return "⚠️ Не удалось связаться с сервером Grok AI. Проверьте интернет-соединение."
    except Exception as e:
        logger.exception("Непредвиденная ошибка при запросе к Grok AI: %s", e)
        return "⚠️ Произошла ошибка при обработке запроса к Grok AI."
