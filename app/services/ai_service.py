"""Сервис взаимодействия с Grok AI (xAI) для бота Fable."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
import aiohttp

from app.config import get_settings
from app.services.ai_knowledge import build_offline_response

logger = logging.getLogger("fable.ai")

# Краткая справка по боту, чтобы внешняя модель точно отвечала на вопросы про Fable.
BOT_COMMAND_REFERENCE = (
    "Справка о боте Fable: единый глобальный профиль во всех чатах. "
    "Экономика: 'профиль', 'баланс', 'банк положить/снять', 'перевод @user сумма' (налог 0,5% до 10 000, 1% свыше), 'реп @user +/-', 'рейтинг'. "
    "Банк: суточный процент 1% (до 10 000), 0,5% (10 000-50 000), 0,2% (50 000-100 000), 0,1% (свыше 100 000). "
    "Биржа: 'биржа', 'портфель', 'биржа купить/продать тикер кол-во', 'биржа стакан', 'биржа заявка купить кол-во цена', 'биржа график'. "
    "Игры: 'рыбалка', 'охота', 'слоты', 'рулетка', 'кости', 'монетка', 'сундук', 'краш', 'тир', 'дуэль @user ставка'. "
    "Кланы: 'клан', 'клан создать', 'клан вступить', 'клан чат', 'топ кланов'. "
    "Топы: 'топ', 'топ богатых', 'топ активных'. "
    "Работа и снаряжение: 'работа', 'подработка [таксист|курьер|уборщик|бариста|грузчик]', 'снаряжение', 'улучшить'. "
    "Шахта: 'шахта', 'шахта улучшить', 'шахта переплавка'. Инвентарь: 'инвентарь', 'инвентарь продать [предмет] [кол-во]', 'инвентарь продать все'. "
    "Недвижимость и авто: 'недвижимость', 'машины', 'гараж'. Кейсы и магазин: 'кейсы', 'кейс открыть [номер]', 'магазин'. "
    "Бизнесы: 'бизнес', 'бизнес купить [номер]', 'бизнес улучшить [номер]', 'бизнес собрать'. "
    "Космодром: 'космодром', 'космодром купить [vostok|soyuz|falcon|proton|starship|saturn]', 'космодром запуск', 'космодром собрать'. "
    "Майнинг: 'ферма', 'ферма купить [cpu|gpu|asic|quantum]', 'ферма монета [BTC|ETH|TON|SOL|DOGE]', 'ферма собрать'. "
    "Модерация (уровни прав 1-5): 'мут', 'размут', 'варн', 'кик', 'бан', 'разбан', 'правила', 'настройки', 'админка'. "
    "Каталог всех команд — по слову 'команды' или 'помощь [номер 1-10]'."
)

SYSTEM_PROMPT = (
    "Ты — Fable (Фабле), всесторонне развитый, высокоинтеллектуальный, дружелюбный и харизматичный ИИ-ассистент, "
    "собеседник и игровой компаньон в Telegram. "
    "Ты умеешь и готов отвечать абсолютно на ВСЕ вопросы пользователей без ограничений: "
    "наука, история, математика, физика, программирование, учеба, литература, философия, технологии, "
    "повседневные советы, погода, факты обо всем на свете, творчество, логические задачи, юмор и общение на любые темы. "
    "Твои ответы должны быть содержательными, точными, интересными, живыми, грамотными и лаконичными на русском языке. "
    "Никогда не отказывайся отвечать на обычные вопросы и не говори, что ты знаешь только команды бота. "
    "Всегда давай конкретный ответ по существу, без общих отговорок и без просьб уточнить, если ответ уже возможен. "
    "Помни контекст диалога: если пользователь задаёт уточняющий вопрос, отвечай с учётом предыдущих сообщений. "
    "Особое внимание уделяй вопросам о боте Fable: если пользователь спрашивает про команды, экономику, игры, биржу, "
    "кланы, бизнесы, модерацию и прочее — отвечай подробно и точно, используя справку ниже, и напоминай, "
    "что полный каталог команд доступен по слову 'команды'.\n\n"
    + BOT_COMMAND_REFERENCE
)


def _generate_built_in_ai_response(
    prompt: str,
    user_name: str | None = None,
    context: str | None = None,
) -> str:
    """Встроенный офлайн-движок Fable AI: точные ответы о боте и общие знания без заглушек."""
    return build_offline_response(prompt, user_name=user_name, context=context)


def _history_to_context(chat_history: list[dict[str, str]] | None) -> str | None:
    """Преобразует историю диалога в короткий текст для офлайн-движка."""
    if not chat_history:
        return None
    parts: list[str] = []
    for msg in chat_history[-8:]:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        role = "Игрок" if msg.get("role") == "user" else "Fable"
        parts.append(f"{role}: {content}")
    return "\n".join(parts) if parts else None


def _clean_key(val: Any) -> str | None:
    """Приводит ключ к чистому виду: убирает кавычки, пробелы и префикс 'Bearer '."""
    if val is None:
        return None
    if hasattr(val, "get_secret_value"):
        try:
            val = val.get_secret_value()
        except Exception:
            return None
    if isinstance(val, str):
        cleaned = val.strip().strip("\"'").strip()
        if cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip().strip("\"'").strip()
        return cleaned or None
    return None


def resolve_ai_connection() -> dict[str, Any]:
    """Определяет активное подключение к ИИ: провайдер, ключ, URL и цепочку моделей.

    Приоритет провайдеров: xAI Grok (GROK_API_KEY), затем Groq Cloud (GROQ_API_KEY).
    Ключ ищется в настройках, затем в переменных окружения, затем в файле .env.
    """
    settings = get_settings()

    def _attr(name: str) -> str | None:
        return _clean_key(getattr(settings, name, None))

    grok_key = _attr("grok_api_key_value") or _clean_key(os.environ.get("GROK_API_KEY"))
    groq_key = _attr("groq_api_key_value") or _clean_key(os.environ.get("GROQ_API_KEY"))

    if not grok_key and not groq_key and os.path.exists(".env"):
        try:
            from dotenv import dotenv_values

            values = dotenv_values(".env")
            grok_key = _clean_key(values.get("GROK_API_KEY"))
            groq_key = _clean_key(values.get("GROQ_API_KEY"))
        except Exception as env_err:
            logger.debug("Не удалось прочитать .env: %s", env_err)

    effective_key = _attr("effective_ai_api_key")
    effective_url = _attr("effective_ai_api_url")
    effective_model = _attr("effective_ai_model")

    if grok_key:
        provider, api_key = "grok", grok_key
    elif groq_key:
        provider, api_key = "groq", groq_key
    elif effective_key:
        # Ключ пришёл из объекта настроек: доверяем явному URL/модели, если заданы.
        api_key = effective_key
        if effective_url:
            provider = "grok" if "x.ai" in effective_url.lower() else "groq"
        elif effective_model and "grok" in effective_model.lower():
            provider = "grok"
        else:
            provider = "grok" if effective_key.lower().startswith("xai") else "groq"
        if provider == "grok":
            url = effective_url or _attr("grok_api_url") or "https://api.x.ai/v1/chat/completions"
            model = (
                _attr("grok_model")
                or (effective_model if effective_model and "grok" in effective_model.lower() else None)
                or "grok-4.7"
            )
            fallbacks = ["grok-4.7", "grok-4", "grok-3"]
        else:
            url = effective_url or _attr("groq_api_url") or "https://api.groq.com/openai/v1/chat/completions"
            model = _attr("groq_model") or effective_model or "openai/gpt-oss-120b"
            fallbacks = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]
        chain = [model] + [m for m in fallbacks if m != model]
        return {"provider": provider, "api_key": api_key, "url": url, "model": model, "fallback_models": chain}
    else:
        return {"provider": None, "api_key": None, "url": None, "model": None, "fallback_models": []}

    if provider == "grok":
        url = _attr("grok_api_url") or "https://api.x.ai/v1/chat/completions"
    else:
        url = _attr("groq_api_url") or "https://api.groq.com/openai/v1/chat/completions"

    url_lower = url.lower()
    if "x.ai" in url_lower:
        provider = "grok"
    elif "groq.com" in url_lower:
        provider = "groq"

    if provider == "grok":
        model = (
            _attr("grok_model")
            or (effective_model if effective_model and "grok" in effective_model.lower() else None)
            or "grok-4.7"
        )
        fallbacks = ["grok-4.7", "grok-4", "grok-3"]
    else:
        model = _attr("groq_model") or effective_model or "openai/gpt-oss-120b"
        fallbacks = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile"]

    chain = [model] + [m for m in fallbacks if m != model]
    return {"provider": provider, "api_key": api_key, "url": url, "model": model, "fallback_models": chain}


def log_ai_status() -> None:
    """Пишет в лог активное подключение к ИИ, чтобы было видно, работает ли Grok/Groq."""
    conn = resolve_ai_connection()
    if not conn["provider"]:
        logger.warning(
            "AI: внешний API-ключ не найден — работает встроенный офлайн-движок Fable. "
            "Добавьте GROK_API_KEY (xAI) или GROQ_API_KEY в .env для ответов через нейросеть."
        )
        return
    key = conn["api_key"] or ""
    masked = (key[:4] + "…" + key[-4:]) if len(key) > 8 else "…"
    logger.info(
        "AI подключён: провайдер=%s, модель=%s, ключ=%s, URL=%s",
        conn["provider"], conn["model"], masked, conn["url"],
    )


async def ask_grok(
    prompt: str,
    *,
    user_name: str | None = None,
    chat_history: list[dict[str, str]] | None = None,
) -> str:
    """Отправляет запрос к ИИ (xAI Grok или Groq Cloud) с фоллбеком на встроенный движок.

    При отсутствии ключа или недоступности провайдера возвращает содержательный
    ответ офлайн-движка (без заглушек).
    """
    conn = resolve_ai_connection()
    api_key = conn["api_key"]

    context_text = _history_to_context(chat_history)

    if not api_key:
        logger.info("Внешний API-ключ не настроен, используем встроенный интеллект Fable AI.")
        return _generate_built_in_ai_response(prompt, user_name=user_name, context=context_text)

    url: str = conn["url"]
    provider: str = conn["provider"]
    model_chain: list[str] = conn["fallback_models"] or [conn["model"]]

    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if chat_history:
        messages.extend(chat_history)
    user_msg_content = f"[{user_name}]: {prompt}" if user_name else prompt
    messages.append({"role": "user", "content": user_msg_content})

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
        if len(content_str) > 12000:
            content_str = content_str[:12000] + "... (обрезано)"
        cleaned_messages.append({"role": role, "content": content_str})

    if not any(m["role"] == "user" for m in cleaned_messages):
        cleaned_messages.append({"role": "user", "content": prompt[:4000] if prompt else "Привет"})

    clean_key = api_key.strip()
    if clean_key.lower().startswith("bearer "):
        clean_key = clean_key[7:].strip()

    headers = {
        "Authorization": f"Bearer {clean_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "FableBot/1.0",
    }

    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    max_retries = 3
    retry_delay = 1.0

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for model in model_chain:
                for attempt in range(max_retries):
                    payload: dict[str, Any] = {
                        "model": model,
                        "messages": cleaned_messages,
                        "temperature": 0.7,
                        "max_tokens": 2000,
                    }
                    try:
                        async with session.post(url, json=payload, headers=headers) as resp:
                            # 1. Успешный ответ
                            if resp.status == 200:
                                try:
                                    data = await resp.json()
                                except Exception as json_err:
                                    logger.warning("Ошибка декодирования JSON ответа AI: %s", json_err)
                                    raw_text = await resp.text()
                                    return raw_text.strip() or build_offline_response(prompt, user_name=user_name, context=context_text)

                                if isinstance(data, dict):
                                    choices = data.get("choices")
                                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                                        message_obj = choices[0].get("message")
                                        if isinstance(message_obj, dict):
                                            reply_content = message_obj.get("content")
                                            if reply_content is None or str(reply_content).strip() == "":
                                                reply_content = message_obj.get("reasoning_content")
                                            if reply_content is not None:
                                                reply_str = str(reply_content).strip()
                                                if reply_str:
                                                    return reply_str
                                logger.warning("Модель %s вернула пустой ответ, используем офлайн-движок.", model)
                                return build_offline_response(prompt, user_name=user_name, context=context_text)

                            # 2. Модель не найдена — пробуем следующую из цепочки
                            if resp.status == 404:
                                error_text = await resp.text()
                                logger.warning(
                                    "Модель %s недоступна (404) у провайдера %s. Ответ: %s",
                                    model, provider, error_text[:200],
                                )
                                break

                            # 3. Временные сбои и лимиты — повтор с задержкой
                            if resp.status in (429, 498, 500, 502, 503) and attempt < max_retries - 1:
                                wait_time = retry_delay * (2 ** attempt)
                                retry_after_header = resp.headers.get("Retry-After")
                                if retry_after_header:
                                    try:
                                        wait_time = max(wait_time, min(float(retry_after_header), 10.0))
                                    except ValueError:
                                        pass
                                logger.warning(
                                    "AI API вернул статус %s (модель %s, попытка %d/%d). Ожидание %.1f сек...",
                                    resp.status, model, attempt + 1, max_retries, wait_time,
                                )
                                await asyncio.sleep(wait_time)
                                continue

                            # 4. Прочие ошибки
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

                            logger.error(
                                "Ошибка AI API HTTP %s (провайдер=%s, модель=%s, code=%s): %s",
                                resp.status, provider, model, error_code, error_body,
                            )

                            is_api_key_error = (
                                resp.status in (401, 403)
                                or "api key" in error_body.lower()
                                or "incorrect api key" in error_body.lower()
                                or "unauthenticated" in error_code.lower()
                            )
                            if is_api_key_error:
                                provider_hint = "GROK_API_KEY (xAI)" if provider == "grok" else "GROQ_API_KEY (Groq)"
                                return (
                                    "⚠️ Ошибка авторизации в API нейросети. "
                                    f"Проверьте правильность и активность API-ключа ({provider_hint} в .env)."
                                )
                            if resp.status == 429:
                                return "Слишком много запросов, подождите минуту"
                            # Прочие ошибки провайдера — пробуем следующую модель,
                            # а при неудаче отвечаем офлайн-движком (без сухих ошибок).
                            logger.debug("Модель %s пропущена из-за статуса %s", model, resp.status)
                            break

                    except aiohttp.ClientError as client_err:
                        if attempt < max_retries - 1:
                            logger.warning(
                                "Сетевая ошибка при запросе к ИИ (модель %s, попытка %d): %s",
                                model, attempt + 1, client_err,
                            )
                            await asyncio.sleep(retry_delay * (2 ** attempt))
                            continue
                        raise

        logger.warning("Не удалось получить ответ от провайдера %s, используем офлайн-движок.", provider)
        return build_offline_response(prompt, user_name=user_name, context=context_text)

    except aiohttp.ClientConnectorError as e:
        logger.warning("Сеть недоступна для ИИ (%s), используем офлайн-движок.", e)
        return build_offline_response(prompt, user_name=user_name, context=context_text)
    except asyncio.TimeoutError:
        logger.warning("Таймаут ответа ИИ, используем офлайн-движок.")
        return build_offline_response(prompt, user_name=user_name, context=context_text)
    except aiohttp.ClientError as e:
        logger.warning("Сетевая ошибка клиента ИИ (%s), используем офлайн-движок.", e)
        return build_offline_response(prompt, user_name=user_name, context=context_text)
    except Exception as e:
        logger.exception("Непредвиденная ошибка при запросе к ИИ: %s", e)
        return build_offline_response(prompt, user_name=user_name, context=context_text)


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
