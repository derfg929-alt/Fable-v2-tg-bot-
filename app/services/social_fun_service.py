"""Сервис социальных и развлекательных функций Fable:
- Ники (смена ника: `ник [новое_имя]`)
- Браки (сделать предложение, согласиться, развестись)
- Шар судьбы (`шар [вопрос]`)
- Реши (`реши [выражение]`) — безопасный калькулятор арифметических выражений
"""
from __future__ import annotations

import ast
import operator
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player
from app.services.economy_service import debit_coins, InsufficientFundsError, PlayerNotFoundError


class SocialFunError(Exception):
    """Базовая ошибка социальных функций."""


NICKNAME_CHANGE_PRICE = 500  # Монет за смену ника
MARRIAGE_PRICE = 5_000       # Монет со стороны инициатора


# Безопасный калькулятор выражений через ast
ALLOWED_OPERATORS = {
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

BALL_ANSWERS = [
    "🔮 Бесспорно!",
    "🔮 Предрешено!",
    "🔮 Никаких сомнений!",
    "🔮 Определённо да!",
    "🔮 Можешь быть уверен в этом!",
    "🔮 Знаки говорят — да!",
    "🔮 Хорошие перспективы!",
    "🔮 Пока не ясно, попробуй снова.",
    "🔮 Спроси позже.",
    "🔮 Лучше не рассказывать...",
    "🔮 Сейчас нельзя предсказать.",
    "🔮 Даже не думай!",
    "🔮 Мой ответ — нет!",
    "🔮 По моим данным — нет.",
    "🔮 Перспективы не очень хорошие.",
    "🔮 Весьма сомнительно.",
]


def evaluate_math_expression(expr: str) -> float | int:
    """Безопасный парсинг и вычисление математического выражения."""
    cleaned = expr.strip().replace("^", "**")
    if not cleaned:
        raise SocialFunError("Введите математическое выражение! Например: <code>реши 2 + 2 * 2</code>")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise SocialFunError("Разрешены только числа и арифметические операции")
        elif isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            op_type = type(node.op)
            if op_type not in ALLOWED_OPERATORS:
                raise SocialFunError("Неподдерживаемая операция")
            if op_type in (ast.Div, ast.FloorDiv, ast.Mod) and right == 0:
                raise SocialFunError("Деление на ноль невозможно!")
            if op_type == ast.Pow and (right > 100 or left > 1000):
                raise SocialFunError("Слишком большие числа для возведения в степень!")
            return ALLOWED_OPERATORS[op_type](left, right)
        elif isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            op_type = type(node.op)
            if op_type not in ALLOWED_OPERATORS:
                raise SocialFunError("Неподдерживаемая унарная операция")
            return ALLOWED_OPERATORS[op_type](operand)
        else:
            raise SocialFunError("Недопустимое выражение!")

    try:
        parsed = ast.parse(cleaned, mode="eval")
        res = _eval(parsed)
        if isinstance(res, float) and res.is_integer():
            return int(res)
        return round(res, 4)
    except SyntaxError:
        raise SocialFunError("Ошибка синтаксиса в выражении. Пример: <code>реши (50 + 25) / 5</code>")


def get_magic_ball_prediction(question: str) -> str:
    if not question.strip():
        return "🔮 Задай вопрос магическому шару! Например: <code>шар я выиграю в казино?</code>"
    return random.choice(BALL_ANSWERS)


async def change_nickname(session: AsyncSession, *, user_id: int, new_nick: str) -> str:
    """Установка или смена ника."""
    clean_nick = new_nick.strip()
    if not clean_nick or len(clean_nick) < 2 or len(clean_nick) > 32:
        raise SocialFunError("Никнейм должен быть длиной от 2 до 32 символов")

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    # Первая смена ника бесплатная, последующие стоят NICKNAME_CHANGE_PRICE
    if player.custom_nickname and player.custom_nickname != clean_nick:
        if player.wallet < NICKNAME_CHANGE_PRICE:
            raise InsufficientFundsError(f"Смена ника стоит {NICKNAME_CHANGE_PRICE} монет. У вас {player.wallet} монет.")
        await debit_coins(session, user_id=user_id, amount=NICKNAME_CHANGE_PRICE, reason="Смена игрового ника", operation_type="social_change_nick")

    player.custom_nickname = clean_nick
    await session.flush()
    return clean_nick


# Временное хранилище предложений о браке: target_id -> proposer_id
PENDING_PROPOSALS: dict[int, int] = {}


async def propose_marriage(session: AsyncSession, *, user_id: int, target_id: int) -> tuple[str, str]:
    if user_id == target_id:
        raise SocialFunError("Нельзя жениться на самом себе!")

    p1 = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    p2 = await session.scalar(select(Player).where(Player.telegram_id == target_id).with_for_update())
    if not p1 or not p2:
        raise PlayerNotFoundError("Один из игроков не найден в базе данных")

    if p1.married_to:
        raise SocialFunError("Вы уже состоите в браке! Сначала оформите <code>развод</code>.")
    if p2.married_to:
        raise SocialFunError("Этот игрок уже состоит в браке!")

    if p1.wallet < MARRIAGE_PRICE:
        raise InsufficientFundsError(f"Свадебное торжество стоит {MARRIAGE_PRICE:,} монет. У вас {p1.wallet:,} монет.".replace(",", " "))

    PENDING_PROPOSALS[target_id] = user_id
    p1_name = p1.custom_nickname or p1.first_name
    p2_name = p2.custom_nickname or p2.first_name
    return p1_name, p2_name


async def accept_marriage(session: AsyncSession, *, target_id: int) -> tuple[str, str]:
    if target_id not in PENDING_PROPOSALS:
        raise SocialFunError("Вам никто не делал предложение руки и сердца!")

    proposer_id = PENDING_PROPOSALS.pop(target_id)
    p1 = await session.scalar(select(Player).where(Player.telegram_id == proposer_id).with_for_update())
    p2 = await session.scalar(select(Player).where(Player.telegram_id == target_id).with_for_update())

    if not p1 or not p2:
        raise SocialFunError("Игроки не найдены")

    if p1.married_to or p2.married_to:
        raise SocialFunError("Один из игроков уже вступил в брак с кем-то другим!")

    if p1.wallet < MARRIAGE_PRICE:
        raise SocialFunError(f"У жениха не хватает {MARRIAGE_PRICE:,} монет на свадьбу!".replace(",", " "))

    await debit_coins(session, user_id=proposer_id, amount=MARRIAGE_PRICE, reason="Свадебная церемония", operation_type="social_marriage")

    p1.married_to = target_id
    p2.married_to = proposer_id
    await session.flush()

    p1_name = p1.custom_nickname or p1.first_name
    p2_name = p2.custom_nickname or p2.first_name
    return p1_name, p2_name


async def decline_marriage(target_id: int) -> bool:
    if target_id in PENDING_PROPOSALS:
        del PENDING_PROPOSALS[target_id]
        return True
    return False


async def divorce(session: AsyncSession, *, user_id: int) -> str:
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player or not player.married_to:
        raise SocialFunError("Вы не состоите в браке!")

    spouse_id = player.married_to
    spouse = await session.scalar(select(Player).where(Player.telegram_id == spouse_id).with_for_update())

    player.married_to = None
    if spouse:
        spouse.married_to = None

    await session.flush()
    spouse_name = spouse.custom_nickname or spouse.first_name if spouse else f"ID {spouse_id}"
    return spouse_name
