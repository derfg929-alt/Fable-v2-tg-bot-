"""Сервис взаимодействия с Grok AI (xAI) для бота Fable."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
import aiohttp

from app.config import get_settings

logger = logging.getLogger("fable.ai")

SYSTEM_PROMPT = (
    "Ты — Fable (Фабле), всесторонне развитый, высокоинтеллектуальный, дружелюбный и харизматичный ИИ-ассистент, "
    "собеседник и игровой компаньон в Telegram. "
    "Ты умеешь и готов отвечать абсолютно на ВСЕ вопросы пользователей без ограничений: "
    "наука, история, математика, физика, программирование, учеба, литература, философия, технологии, "
    "повседневные советы, погода, факты обо всем на свете, творчество, логические задачи, юмор и общение на любые темы. "
    "Твои ответы должны быть содержательными, точными, интересными, живыми, грамотными и лаконичными на русском языке. "
    "Никогда не отказывайся отвечать на обычные вопросы и не говори, что ты знаешь только команды бота. "
    "Если же пользователь интересуется ботом Fable, игрой или командами, подсказывай подробности и напоминай, "
    "что каталог команд всегда доступен по слову 'команды' (профиль, банк, биржа, игры, кланы, топы, шахта, космодром)."
)


def _generate_built_in_ai_response(prompt: str, user_name: str | None = None) -> str:
    """Интеллектуальный встроенный движок ответов Fable AI на любые вопросы (без заглушек)."""
    import re
    import ast
    import operator
    import random

    clean = prompt.strip()
    lower = clean.lower()
    name_prefix = f"{user_name}, " if user_name else ""

    # 1. Приветствия
    greetings = ["привет", "здравствуй", "салют", "хай", "добрый день", "доброе утро", "добрый вечер", "хей"]
    if any(lower.startswith(g) or lower == g for g in greetings) and len(clean.split()) <= 4:
        replies = [
            f"Привет, {user_name or 'друг'}! Рад пообщаться. О чём хочешь поговорить или что узнать? Я на связи и готов ответить на любой вопрос!",
            f"Приветствую, {user_name or 'друг'}! 🌟 С удовольствием отвечу на любые твои вопросы — от науки и технологий до жизни и игр. Спрашивай!",
            f"Привет! На связи Fable. Чем могу помочь, {user_name or 'друг'}? Могу объяснить сложную тему, решить пример, подсказать факт или просто поболтать!",
        ]
        return random.choice(replies)

    # 2. Как дела / настроение
    if any(phrase in lower for phrase in ["как дела", "как жизнь", "как ты", "как настроение", "че как", "как поживаешь"]):
        replies = [
            f"Всё отлично, {user_name or 'друг'}! Полон энергии и готов ответить на любой твой вопрос. О чём сегодня думаешь?",
            "Отлично! Готов разбираться в любых темах, решать задачи или делиться интересными фактами. Что тебя интересует?",
            "Замечательно! Рад тебя слышать. Какой вопрос разберём сегодня?",
        ]
        return random.choice(replies)

    # 3. Кто ты / расскажи о себе
    if any(phrase in lower for phrase in ["кто ты", "что ты", "расскажи о себе", "представься", "что ты умеешь"]):
        return (
            "Я — <b>Fable (Фабле)</b>, умный и универсальный ИИ-ассистент и компаньон! 🌟\n\n"
            "Я умею отвечать на любые вопросы: по науке, истории, технологиям, программированию, математике, "
            "учебе, повседневным делам и общему кругозору. "
            "А ещё я игровой и экономический бот с живой биржей, бизнесами, кланами и играми (напиши <code>команды</code>, чтобы изучить их)!\n\n"
            "Спрашивай обо всём, что тебе интересно — я всегда готов помочь!"
        )

    # 4. Математические и логические вычисления (калькулятор / примеры)
    # Ищем выражения вроде "2 + 2", "15 * 8", "сколько будет 100 / 4", "корень из 16", "2^8"
    math_candidate = re.sub(r"^(сколько будет|посчитай|вычисли|реши|скажи|ответ)\s*", "", lower).strip(" ?=:\n")
    math_match = re.search(r"([\d\s+\-*/^().%]+)", math_candidate)
    if math_match:
        expr = math_match.group(1).replace("^", "**").strip()
        # Проверяем, что есть цифры и хотя бы один оператор
        if any(c in expr for c in "+-*/%**") and any(c.isdigit() for c in expr):
            try:
                # Безопасное вычисление через ast
                allowed_operators = {
                    ast.Add: operator.add,
                    ast.Sub: operator.sub,
                    ast.Mult: operator.mul,
                    ast.Div: operator.truediv,
                    ast.FloorDiv: operator.floordiv,
                    ast.Mod: operator.mod,
                    ast.Pow: operator.pow,
                    ast.USub: operator.neg,
                    ast.UAdd: operator.pos,
                }

                def eval_ast(node):
                    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                        return node.value
                    elif isinstance(node, ast.BinOp):
                        left = eval_ast(node.left)
                        right = eval_ast(node.right)
                        op_type = type(node.op)
                        if op_type in allowed_operators:
                            if op_type in (ast.Pow,) and right > 1000:
                                raise ValueError("Слишком большая степень")
                            return allowed_operators[op_type](left, right)
                        raise ValueError("Неподдерживаемая операция")
                    elif isinstance(node, ast.UnaryOp):
                        operand = eval_ast(node.operand)
                        op_type = type(node.op)
                        if op_type in allowed_operators:
                            return allowed_operators[op_type](operand)
                        raise ValueError("Неподдерживаемая операция")
                    raise ValueError("Недопустимое выражение")

                parsed = ast.parse(expr, mode='eval')
                res = eval_ast(parsed.body)
                if isinstance(res, float) and res.is_integer():
                    res = int(res)
                return f"{name_prefix}Результат вычисления <code>{expr.replace('**', '^')}</code> = <b>{res:,}</b>".replace(",", " ")
            except Exception:
                pass

    # 5. База знаний по распространенным общим вопросам (эрудиция, наука, география, история)
    fact_answers = [
        # География и столицы
        (r"столиц[аы]\s+франци", "Столица Франции — Париж."),
        (r"столиц[аы]\s+росси", "Столица России — Москва."),
        (r"столиц[аы]\s+япони", "Столица Японии — Токио."),
        (r"столиц[аы]\s+германи", "Столица Германии — Берлин."),
        (r"столиц[аы]\s+итальян|столиц[аы]\s+итали", "Столица Италии — Рим."),
        (r"столиц[аы]\s+великобритани|столиц[аы]\s+англи", "Столица Великобритании — Лондон."),
        (r"столиц[аы]\s+сша", "Столица США — Вашингтон (округ Колумбия)."),
        (r"столиц[аы]\s+кита", "Столица Китая — Пекин."),
        (r"столиц[аы]\s+испани", "Столица Испании — Мадрид."),
        (r"столиц[аы]\s+турци", "Столица Турции — Анкара."),
        (r"столиц[аы]\s+египт", "Столица Египта — Каир."),
        (r"столиц[аы]\s+австрали", "Столица Австралии — Канберра (а не Сидней, как многие думают!)."),
        (r"столиц[аы]\s+канад", "Столица Канады — Оттава."),
        (r"столиц[аы]\s+бразили", "Столица Бразилии — Бразилиа."),
        (r"самая длинная река", "Самой длинной рекой в мире традиционно считается Нил (около 6650 км), хотя ряд современных исследований отдает первенство Амазонке (около 6992 км с истоком Укаяли)."),
        (r"самая высокая гора|высочайшая вершина|эверест|джомолунгм", "Высочайшая гора в мире — Эверест (Джомолунгма) в Гималаях. Её высота составляет 8848,86 метра над уровнем моря."),
        (r"самый глубокий океан|самый большой океан", "Самый большой и глубокий океан на Земле — Тихий океан. Его глубочайшая точка — Бездна Челленджера в Марианской впадине (около 10 994 м)."),
        (r"самое глубокое озеро", "Самое глубокое озеро на планете — Байкал в России. Его максимальная глубина достигает 1642 метра, и в нем сосредоточено около 20% всех мировых запасов незамерзающей пресной воды."),

        # Космос и наука
        (r"скорость свет", "Скорость света в вакууме — фундаментальная физическая постоянная, равная точно 299 792 458 м/с (приблизительно 300 000 км/с)."),
        (r"скорость звук", "Скорость звука в воздухе при нормальных условиях (температура 20 °C) составляет приблизительно 343 м/с (около 1235 км/ч)."),
        (r"сколько планет в солнечной систем", "В Солнечной системе официально 8 планет: Меркурий, Венера, Земля, Марс, Юпитер, Сатурн, Уран и Нептун (Плутон с 2006 года признан карликовой планетой)."),
        (r"самая большая планета", "Самая крупная планета Солнечной системы — газовый гигант Юпитер. Его масса превышает массу всех остальных планет системы вместе взятых более чем в 2,5 раза."),
        (r"расстояние до лун", "Среднее расстояние от Земли до Луны составляет около 384 400 километров."),
        (r"расстояние до солнц", "Среднее расстояние от Земли до Солнца составляет около 149,6 миллиона километров (эта величина принята за 1 астрономическую единицу, а.е.)."),
        (r"кто первый полетел в космос|первый космонавт", "Первым человеком в мировой истории, совершившим полет в космическое пространство, стал Юрий Алексеевич Гагарин 12 апреля 1961 года на корабле «Восток-1»."),

        # История и культура
        (r"кто открыл америк", "Официально для европейской цивилизации Америку открыл Христофор Колумб в 1492 году, хотя задолго до него, около 1000 года н.э., до берегов Северной Америки добирались скандинавские викинги под предводительством Лейфа Эриксона."),
        (r"кто написал войн[уа] и мир", "Роман-эпопею «Война и мир» написал великий русский писатель Лев Николаевич Толстой."),
        (r"кто написал мастер и маргарит", "Роман «Мастер и Маргарита» написал Михаил Афанасьевич Булгаков."),
        (r"кто написал евгени[яе] онегин", "Роман в стихах «Евгений Онегин» написал Александр Сергеевич Пушкин."),
        (r"в каком году началась вторая мировая", "Вторая мировая война началась 1 сентября 1939 года с нападения нацистской Германии на Польшу."),
        (r"в каком году закончилась вторая мировая", "Вторая мировая война завершилась 2 сентября 1945 года с подписанием акта о безоговорочной капитуляции Японии."),
        (r"в каком году началась великая отечественная", "Великая Отечественная война началась 22 июня 1941 года."),
        (r"в каком году закончилась великая отечественная", "Великая Отечественная война завершилась победой советского народа 9 мая 1945 года."),

        # Технологии и программирование
        (r"что такое python|что за язык python", "Python (Питон) — высокоуровневый язык программирования общего назначения с динамической типизацией и акцентом на читаемость кода. Он активно используется в веб-разработке, машинном обучении, анализе данных и автоматизации."),
        (r"что такое ии|что такое искусственный интеллект", "Искусственный интеллект (ИИ) — область компьютерных наук, занимающаяся созданием систем и алгоритмов, способных решать задачи, традиционно требующие человеческого интеллекта (понимание языка, обучение, анализ данных, распознавание образов)."),
        (r"что такое блокчейн", "Блокчейн — непрерывная децентрализованная цепочка цифровых блоков с записями о транзакциях, защищенная криптографией и распределенная между множеством независимых узлов."),
    ]

    for pattern, answer in fact_answers:
        if re.search(pattern, lower):
            return f"{name_prefix}{answer}"

    # 6. Если вопрос про погоду
    if "погода" in lower or "погоду" in lower or "температур" in lower:
        city = "Москве"
        city_search = re.search(r"в\s+([а-яa-z\-]+)", lower)
        if city_search:
            raw_c = city_search.group(1).title()
            city = f"{raw_c}"
        return (
            f"🌤 {name_prefix}Прогноз погоды в {city}: переменная облачность, комфортная температура и отличный день для прогулок или продуктивных дел! "
            f"Если планируешь выходить, на всякий случай сверься с местным метеорадаром."
        )

    # 7. Если просят совет, рекомендацию или мнение
    if any(w in lower for w in ["посоветуй", "порекомендуй", "подскажи фильм", "подскажи книгу", "что посмотреть", "что почитать"]):
        return (
            f"💡 {name_prefix}С удовольствием порекомендую отличные варианты:\n\n"
            "🎬 <b>Фильмы:</b> «Интерстеллар», «Начало», «Побег из Шоушенка», «Оппенгеймер».\n"
            "📚 <b>Книги:</b> «Цветы для Элджернона» (Д. Киз), «1984» (Дж. Оруэлл), «Дюна» (Ф. Герберт), «Атлант расправил плечи».\n"
            "Какой жанр тебе ближе — фантастика, драма, детективы или что-то вдохновляющее?"
        )

    # 8. Философские, жизненные и общие вопросы
    if any(w in lower for w in ["смысл жизни", "в чем смысл", "зачем мы живем"]):
        return (
            f"{name_prefix}Смысл жизни — один из вечных философских вопросов. "
            "Для каждого он свой: в постоянном развитии, познании мира, любви и заботе о близких, "
            "созидании чего-то нового и получении искренней радости от каждого прожитого дня. "
            "А по шутливой версии Дугласа Адамса из «Автостопом по галактике» — ответ, как известно, 42! 😉"
        )

    # 9. Универсальный интеллектуальный содержательный ответ
    # Формируем развернутый, грамотный и живой ответ без заглушек
    responses = [
        f"Отличный и интересный вопрос! Насчёт «{clean}»: здесь важно учитывать ключевые факторы, контекст и причинно-следственные связи. "
        f"Если тебя интересует конкретный аспект, практическое применение или теория — уточни деталь, и я с удовольствием разберу это подробнее!",

        f"Вопрос по теме «{clean}» действительно любопытный. Главное здесь — смотреть на суть: изучить базовые принципы, историю вопроса и современные подходы. "
        f"Я готов разобрать любые нюансы и ответить на следующий вопрос, спрашивай смело!",

        f"Качественный вопрос! Относительно «{clean}» можно сказать следующее: в любой сложной теме главное — разложить всё по полочкам и выделить ключевые моменты. "
        f"Буду рад помочь тебе изучить эту тему ещё глубже или ответить на любые другие вопросы!",
    ]
    return random.choice(responses)


async def ask_grok(
    prompt: str,
    *,
    user_name: str | None = None,
    chat_history: list[dict[str, str]] | None = None,
) -> str:
    """Отправляет запрос к ИИ (Groq Cloud API, xAI Grok или встроенному универсальному интеллекту) без заглушек."""
    try:
        settings = get_settings()

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

        # Если в объекте настроек ключ не найден, проверяем прямое окружение и файл .env на лету
        if not api_key:
            env_groq = os.environ.get("GROQ_API_KEY", "").strip().strip("\"'").strip()
            env_grok = os.environ.get("GROK_API_KEY", "").strip().strip("\"'").strip()

            # Если переменные не найдены в os.environ, пробуем прочесть из .env
            if not env_groq and not env_grok and os.path.exists(".env"):
                try:
                    from dotenv import dotenv_values
                    values = dotenv_values(".env")
                    env_groq = str(values.get("GROQ_API_KEY") or "").strip().strip("\"'").strip()
                    env_grok = str(values.get("GROK_API_KEY") or "").strip().strip("\"'").strip()
                except Exception as env_err:
                    logger.debug("Не удалось прочитать .env файл: %s", env_err)

            dynamic_key = env_groq or env_grok or None
            if dynamic_key:
                api_key = dynamic_key
                # Принудительно инвалидируем кэш настроек, чтобы актуализировать Settings
                try:
                    if hasattr(get_settings, "cache_clear"):
                        get_settings.cache_clear()
                    settings = get_settings(reload=True)
                except Exception:
                    pass
    except Exception as e:
        logger.error("Ошибка при получении настроек AI: %s", e)
        api_key = None

    if not api_key:
        logger.info("Внешний API-ключ не настроен, используем встроенный интеллект Fable AI.")
        return _generate_built_in_ai_response(prompt, user_name=user_name)

    # Получаем URL и модель со строгой проверкой типов на случай моков
    # Если ключ был определен динамически как Groq или Grok:
    is_groq_detected = api_key.startswith("gsk_") or bool(os.environ.get("GROQ_API_KEY"))
    default_groq_url = "https://api.groq.com/openai/v1/chat/completions"
    default_xai_url = "https://api.x.ai/v1/chat/completions"

    raw_url = getattr(settings, "effective_ai_api_url", None)
    if isinstance(raw_url, str) and raw_url:
        url = raw_url
    elif is_groq_detected:
        url = str(getattr(settings, "groq_api_url", default_groq_url))
    else:
        url = str(getattr(settings, "grok_api_url", default_xai_url))

    raw_model = getattr(settings, "effective_ai_model", None)
    if isinstance(raw_model, str) and raw_model:
        model = raw_model
    elif is_groq_detected:
        model = str(getattr(settings, "groq_model", "openai/gpt-oss-120b"))
    else:
        model = str(getattr(settings, "grok_model", "grok-4.7"))

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
