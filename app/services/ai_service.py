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
    """Отправляет запрос к ИИ (Groq Cloud API или xAI Grok) и возвращает сгенерированный ответ."""
    try:
        settings = get_settings()
        # Проверяем grok_api_key_value в первую очередь (он возвращает effective_ai_api_key в реальном Settings)
        # либо явно effective_ai_api_key / groq_api_key_value
        api_key = getattr(settings, "grok_api_key_value", None)
        if not api_key or not isinstance(api_key, str):
            eff_key = getattr(settings, "effective_ai_api_key", None)
            if isinstance(eff_key, str) and eff_key:
                api_key = eff_key
            else:
                groq_key = getattr(settings, "groq_api_key_value", None)
                if isinstance(groq_key, str) and groq_key:
                    api_key = groq_key
                else:
                    api_key = None
    except Exception as e:
        logger.error("Ошибка при получении настроек AI: %s", e)
        api_key = None

    if not api_key:
        logger.warning("GROQ_API_KEY или GROK_API_KEY не настроен в конфигурации.")
        return (
            "Привет! Я Фабле 🌟 Мой модуль искусственного интеллекта готов к работе, "
            "но администратор еще не указал API-ключ (переменная GROQ_API_KEY или GROK_API_KEY в .env)."
        )

    # Получаем URL и модель со строгой проверкой типов на случай моков
    raw_url = getattr(settings, "effective_ai_api_url", None)
    if isinstance(raw_url, str) and raw_url:
        url = raw_url
    else:
        url = str(getattr(settings, "grok_api_url", "https://api.x.ai/v1/chat/completions"))

    raw_model = getattr(settings, "effective_ai_model", None)
    if isinstance(raw_model, str) and raw_model:
        model = raw_model
    else:
        model = str(getattr(settings, "grok_model", "grok-beta"))

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
                    logger.error("Ошибка AI API HTTP %s: %s", resp.status, error_text)
                    return f"⚠️ Ошибка обращения к ИИ (код {resp.status}). Попробуйте позже."
    except aiohttp.ClientError as e:
        logger.error("Сетевая ошибка при запросе к ИИ: %s", e)
        return "⚠️ Не удалось связаться с сервером ИИ. Проверьте интернет-соединение."
    except Exception as e:
        logger.exception("Непредвиденная ошибка при запросе к ИИ: %s", e)
        return "⚠️ Произошла ошибка при обработке запроса к ИИ."


async def analyze_chat_moderation_ai(
    chat_title: str,
    recent_events_text: str,
) -> str:
    """Интеллектуальный анализ активности чата и рекомендации для владельца/администраторов."""
    prompt = (
        f"Ты — виртуальный ИИ-администратор чата «{chat_title}». "
        f"Проанализируй следующую сводку активности и событий:\n"
        f"{recent_events_text}\n\n"
        f"Дай краткий, профессиональный и полезный отчет для владельца чата: "
        f"1. Оценка безопасности и токсичности. 2. Рекомендации по настройкам чата. "
        f"3. Общий вердикт. Форматируй красиво для Telegram (HTML)."
    )
    return await ask_grok(prompt)
