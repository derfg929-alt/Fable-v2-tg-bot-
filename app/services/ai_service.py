"""Сервис взаимодействия с Grok AI (xAI) для бота Fable."""
from __future__ import annotations

import asyncio
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
        # Проверяем все возможные свойства: grok_api_key_value, effective_ai_api_key, groq_api_key_value
        def _extract_str(val: Any) -> str | None:
            if not val:
                return None
            if hasattr(val, "get_secret_value"):
                val = val.get_secret_value()
            if isinstance(val, str):
                cleaned = val.strip().strip("\"'").strip()
                return cleaned or None
            return None

        api_key = (
            _extract_str(getattr(settings, "grok_api_key_value", None))
            or _extract_str(getattr(settings, "effective_ai_api_key", None))
            or _extract_str(getattr(settings, "groq_api_key_value", None))
        )
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

    # Очищаем и форматируем сообщения (пункт 5: правильная структура сообщений, ограничение длины)
    cleaned_messages: list[dict[str, str]] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        if role not in ("system", "user", "assistant"):
            role = "user"
        content = msg.get("content")
        if content is None:
            continue
        content_str = str(content).strip()
        if not content_str:
            continue
        # Обрезаем чрезмерно длинные сообщения во избежание 400/413
        if len(content_str) > 12000:
            content_str = content_str[:12000] + "... (обрезано)"
        cleaned_messages.append({"role": role, "content": content_str})

    if not any(m["role"] == "user" for m in cleaned_messages):
        cleaned_messages.append({"role": "user", "content": prompt[:4000] if prompt else "Привет"})

    # Корректные заголовки запроса (пункт 4: Headers)
    # Гарантируем чистый ключ без префикса 'Bearer ' или лишних символов
    clean_key = api_key.strip()
    if clean_key.lower().startswith("bearer "):
        clean_key = clean_key[7:].strip()

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "FableBot/1.0",
    }

    # Резервные модели на случай 404 model_not_found
    groq_fallback_model = "openai/gpt-oss-20b"
    grok_fallback_model = "grok-4.7"
    is_xai = "x.ai" in url
    is_groq = "groq.com" in url

    current_model = model

    timeout = aiohttp.ClientTimeout(total=45, connect=10)

    # Максимум 3 попытки при rate limits (429) или временных сбоях сервера (500, 502, 503, 498)
    max_retries = 3
    retry_delay = 1.0

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(max_retries):
                payload: dict[str, Any] = {
                    "model": current_model,
                    "messages": cleaned_messages,
                    "temperature": 0.7,
                    "max_tokens": 1024,
                }

                try:
                    async with session.post(url, json=payload, headers=headers) as resp:
                        # 1. Успешный ответ (200 OK)
                        if resp.status == 200:
                            try:
                                data = await resp.json()
                            except Exception as json_err:
                                logger.warning("Ошибка декодирования JSON ответа AI: %s", json_err)
                                raw_text = await resp.text()
                                return raw_text.strip() or "Фабле на связи, но получен неразборчивый ответ 🤔"

                            if not isinstance(data, dict):
                                return "Фабле на связи, но ответ нейросети имел неверный формат 🤔"

                            choices = data.get("choices")
                            if isinstance(choices, list) and len(choices) > 0 and isinstance(choices[0], dict):
                                message_obj = choices[0].get("message")
                                if isinstance(message_obj, dict):
                                    # Поддерживаем как content, так и reasoning_content
                                    reply_content = message_obj.get("content")
                                    if reply_content is None or str(reply_content).strip() == "":
                                        reply_content = message_obj.get("reasoning_content")
                                    if reply_content is not None:
                                        reply_str = str(reply_content).strip()
                                        if reply_str:
                                            return reply_str
                            return "Фабле на связи, но ответ от нейросети оказался пустым 🤔"

                        # 2. Модель не найдена (404 model_not_found) -> пробуем фоллбек модель (пункт 1)
                        if resp.status == 404:
                            if is_groq and current_model != groq_fallback_model:
                                error_text = await resp.text()
                                logger.warning(
                                    "Модель %s не найдена на Groq (404), переключаемся на %s. Ответ: %s",
                                    current_model, groq_fallback_model, error_text
                                )
                                current_model = groq_fallback_model
                                continue
                            elif is_xai and current_model != grok_fallback_model:
                                error_text = await resp.text()
                                logger.warning(
                                    "Модель %s не найдена на xAI Grok (404), переключаемся на %s. Ответ: %s",
                                    current_model, grok_fallback_model, error_text
                                )
                                current_model = grok_fallback_model
                                continue

                        # 3. Превышение лимитов (429 Rate Limit) или временные сбои (500, 502, 503, 498) -> retry (пункт 3)
                        if resp.status in (429, 498, 500, 502, 503) and attempt < max_retries - 1:
                            retry_after_header = resp.headers.get("Retry-After")
                            wait_time = retry_delay * (2 ** attempt)
                            if retry_after_header:
                                try:
                                    wait_time = max(wait_time, min(float(retry_after_header), 10.0))
                                except ValueError:
                                    pass
                            logger.warning(
                                "AI API вернул статус %s (попытка %d/%d). Ожидание %.1f сек...",
                                resp.status, attempt + 1, max_retries, wait_time
                            )
                            await asyncio.sleep(wait_time)
                            continue

                        # 4. Другие ошибки (401 Unauthorized, 403 Forbidden, 400 Bad Request и др.)
                        error_body = ""
                        error_code = ""
                        try:
                            err_data = await resp.json()
                            if isinstance(err_data, dict):
                                error_code = str(err_data.get("code") or "")
                                if "error" in err_data:
                                    err_obj = err_data["error"]
                                    if isinstance(err_obj, dict):
                                        error_body = err_obj.get("message", "")
                                        if not error_code:
                                            error_code = str(err_obj.get("code") or "")
                                    elif isinstance(err_obj, str):
                                        error_body = err_obj
                        except Exception:
                            error_body = await resp.text()

                        logger.error("Ошибка AI API HTTP %s (code=%s): %s", resp.status, error_code, error_body)

                        # xAI Grok возвращает HTTP 400 c error_code="invalid-argument" и сообщением про API key
                        is_api_key_error = (
                            resp.status in (401, 403)
                            or "api key" in error_body.lower()
                            or "incorrect api key" in error_body.lower()
                            or "unauthenticated" in error_code.lower()
                        )

                        if is_api_key_error:
                            provider_hint = "GROK_API_KEY (xAI)" if is_xai else "GROQ_API_KEY (Groq)"
                            return (
                                f"⚠️ Ошибка авторизации в API нейросети. "
                                f"Проверьте правильность и активность API-ключа ({provider_hint} в .env)."
                            )
                        elif resp.status == 429:
                            return "Слишком много запросов, подождите минуту"
                        elif resp.status == 400:
                            reason = error_body[:120] if error_body else "неверный формат параметров"
                            return f"⚠️ Ошибка запроса к ИИ: {reason}"
                        elif resp.status == 404:
                            return f"⚠️ Выбранная модель ИИ ({current_model}) недоступна (404 Not Found)."
                        else:
                            return f"⚠️ Ошибка обращения к ИИ (код {resp.status}). Попробуйте позже."

                except aiohttp.ClientError as client_err:
                    if attempt < max_retries - 1:
                        logger.warning("Сетевая ошибка при запросе к ИИ (попытка %d): %s", attempt + 1, client_err)
                        await asyncio.sleep(retry_delay * (2 ** attempt))
                        continue
                    raise

    except aiohttp.ClientConnectorError as e:
        logger.error("Сетевая ошибка подключения к серверам ИИ: %s", e)
        return "⚠️ Не удалось связаться с сервером ИИ (ошибка сети/DNS). Проверьте интернет-соединение или доступность хоста."
    except asyncio.TimeoutError:
        logger.error("Таймаут ожидания ответа от ИИ (превышено 45 сек).")
        return "⏳ Сервер ИИ не ответил вовремя (таймаут). Попробуйте задать вопрос еще раз."
    except aiohttp.ClientError as e:
        logger.error("Сетевая ошибка клиента при запросе к ИИ: %s", e)
        return "⚠️ Ошибка связи с сервером ИИ. Проверьте интернет-соединение."
    except Exception as e:
        logger.exception("Непредвиденная ошибка при запросе к ИИ: %s", e)
        return "⚠️ Произошла непредвиденная ошибка при обработке запроса к ИИ."


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
