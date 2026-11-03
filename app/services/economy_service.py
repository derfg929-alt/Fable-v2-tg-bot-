"""Безопасные операции с глобальным балансом монет.

Каждое изменение кошелька сопровождается неизменяемой записью
EconomyTransaction. Операции не выполняют commit самостоятельно: ими
управляет вызывающий use-case через общую транзакцию БД.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EconomyTransaction, Player


class EconomyError(Exception):
    """Базовая ошибка экономики."""


class PlayerNotFoundError(EconomyError):
    """Профиль игрока не найден."""


class InvalidAmountError(EconomyError):
    """Сумма операции некорректна."""


class InsufficientFundsError(EconomyError):
    """На кошельке недостаточно монет."""


class SelfTransferError(EconomyError):
    """Перевод самому себе запрещён."""


@dataclass(frozen=True, slots=True)
class EconomyResult:
    transaction: EconomyTransaction
    actor_balance: int | None
    recipient_balance: int | None
    duplicate: bool = False


def _validate_amount(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidAmountError("Количество монет должно быть целым числом больше нуля")


async def _lock_players(
    session: AsyncSession, telegram_ids: list[int]
) -> dict[int, Player]:
    """Блокирует строки игроков в едином порядке, исключая взаимные deadlock."""
    unique_ids = sorted(set(telegram_ids))
    result = await session.execute(
        select(Player)
        .where(Player.telegram_id.in_(unique_ids))
        .order_by(Player.telegram_id)
        .with_for_update()
    )
    players = {player.telegram_id: player for player in result.scalars().all()}
    missing = [user_id for user_id in unique_ids if user_id not in players]
    if missing:
        raise PlayerNotFoundError(
            "Не найдены профили игроков: " + ", ".join(map(str, missing))
        )
    return players


async def _insert_ledger(
    session: AsyncSession,
    *,
    operation_type: str,
    amount: int,
    actor_id: int | None,
    recipient_id: int | None,
    fee: int = 0,
    actor_delta: int = 0,
    recipient_delta: int = 0,
    reason: str | None = None,
    reference: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> tuple[EconomyTransaction | None, EconomyTransaction | None]:
    """Вставляет ledger-запись; при повторном ключе возвращает старую запись."""
    values = {
        "operation_type": operation_type,
        "amount": amount,
        "actor_id": actor_id,
        "recipient_id": recipient_id,
        "fee": fee,
        "actor_balance_delta": actor_delta,
        "recipient_balance_delta": recipient_delta,
        "reason": reason,
        "reference": reference,
        "idempotency_key": idempotency_key,
        "metadata_json": metadata or {},
    }

    statement = insert(EconomyTransaction).values(**values)
    if idempotency_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[EconomyTransaction.idempotency_key]
        )
    inserted_id = (
        await session.execute(statement.returning(EconomyTransaction.id))
    ).scalar_one_or_none()

    if inserted_id is not None:
        transaction = await session.get(EconomyTransaction, inserted_id)
        if transaction is None:
            raise RuntimeError("Запись экономики вставлена, но не найдена")
        return transaction, None

    if idempotency_key is None:
        raise RuntimeError("Не удалось записать операцию экономики")

    existing = await session.scalar(
        select(EconomyTransaction).where(
            EconomyTransaction.idempotency_key == idempotency_key
        )
    )
    if existing is None:
        raise RuntimeError("Конфликт ключа идемпотентности без существующей записи")
    return None, existing


async def credit_coins(
    session: AsyncSession,
    *,
    user_id: int,
    amount: int,
    reason: str,
    actor_id: int | None = None,
    operation_type: Literal["reward", "admin_credit", "refund", "system_credit"] = "reward",
    reference: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> EconomyResult:
    """Начисляет монеты игроку и записывает операцию в журнал."""
    _validate_amount(amount)
    player = (await _lock_players(session, [user_id]))[user_id]

    transaction, duplicate = await _insert_ledger(
        session,
        operation_type=operation_type,
        amount=amount,
        actor_id=actor_id,
        recipient_id=user_id,
        actor_delta=0,
        recipient_delta=amount,
        reason=reason,
        reference=reference,
        idempotency_key=idempotency_key,
        metadata=metadata,
    )
    if duplicate is not None:
        return EconomyResult(
            duplicate, player.wallet, player.wallet, duplicate=True
        )

    player.wallet += amount
    return EconomyResult(transaction, player.wallet, player.wallet)


async def debit_coins(
    session: AsyncSession,
    *,
    user_id: int,
    amount: int,
    reason: str,
    actor_id: int | None = None,
    operation_type: Literal["purchase", "admin_debit", "fee", "system_debit"] = "purchase",
    reference: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> EconomyResult:
    """Списывает монеты; отрицательный баланс невозможен."""
    _validate_amount(amount)
    player = (await _lock_players(session, [user_id]))[user_id]

    # Сначала проверяем ключ идемпотентности, чтобы повторный запрос уже
    # исполненной покупки не вернул ошибку из-за изменившегося баланса.
    if idempotency_key is not None:
        existing = await session.scalar(
            select(EconomyTransaction).where(
                EconomyTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return EconomyResult(existing, player.wallet, player.wallet, duplicate=True)

    if player.wallet < amount:
        raise InsufficientFundsError(
            f"Недостаточно монет: доступно {player.wallet}, требуется {amount}"
        )

    transaction, duplicate = await _insert_ledger(
        session,
        operation_type=operation_type,
        amount=amount,
        actor_id=actor_id if actor_id is not None else user_id,
        recipient_id=None,
        actor_delta=-amount,
        recipient_delta=0,
        reason=reason,
        reference=reference,
        idempotency_key=idempotency_key,
        metadata=metadata,
    )
    if duplicate is not None:
        return EconomyResult(duplicate, player.wallet, None, duplicate=True)

    player.wallet -= amount
    return EconomyResult(transaction, player.wallet, None)


async def transfer_coins(
    session: AsyncSession,
    *,
    sender_id: int,
    recipient_id: int,
    amount: int,
    reason: str = "Перевод монет",
    fee: int = 0,
    idempotency_key: str | None = None,
    reference: str | None = None,
) -> EconomyResult:
    """Переводит монеты между игроками в одной атомарной транзакции.

    Комиссия удерживается с отправителя и не зачисляется получателю.
    В журнале amount — сумма перевода, fee — комиссия.
    """
    _validate_amount(amount)
    if isinstance(fee, bool) or not isinstance(fee, int) or fee < 0:
        raise InvalidAmountError("Комиссия должна быть целым неотрицательным числом")
    if sender_id == recipient_id:
        raise SelfTransferError("Нельзя переводить монеты самому себе")
    if sender_id <= 0 or recipient_id <= 0:
        raise ValueError("Telegram ID должны быть положительными")

    players = await _lock_players(session, [sender_id, recipient_id])
    sender = players[sender_id]
    recipient = players[recipient_id]

    if idempotency_key is not None:
        existing = await session.scalar(
            select(EconomyTransaction).where(
                EconomyTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            return EconomyResult(
                existing, sender.wallet, recipient.wallet, duplicate=True
            )

    total_debit = amount + fee
    if sender.wallet < total_debit:
        raise InsufficientFundsError(
            f"Недостаточно монет: доступно {sender.wallet}, "
            f"требуется {total_debit} с учётом комиссии"
        )

    transaction, duplicate = await _insert_ledger(
        session,
        operation_type="transfer",
        amount=amount,
        actor_id=sender_id,
        recipient_id=recipient_id,
        fee=fee,
        actor_delta=-total_debit,
        recipient_delta=amount,
        reason=reason,
        reference=reference,
        idempotency_key=idempotency_key,
    )
    if duplicate is not None:
        return EconomyResult(
            duplicate, sender.wallet, recipient.wallet, duplicate=True
        )

    sender.wallet -= total_debit
    recipient.wallet += amount
    return EconomyResult(transaction, sender.wallet, recipient.wallet)


async def get_wallet_balance(
    session: AsyncSession, user_id: int
) -> int:
    """Возвращает текущий баланс кошелька игрока."""
    balance = await session.scalar(
        select(Player.wallet).where(Player.telegram_id == user_id)
    )
    if balance is None:
        raise PlayerNotFoundError(f"Игрок Telegram ID {user_id} не найден")
    return int(balance)

async def burn_coins(
    session: AsyncSession,
    *,
    user_id: int,
    amount: int,
    reason: str,
    operation_type: str = "burn",
    reference: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict | None = None,
) -> EconomyResult:
    """Сжигает (удаляет из экономики) монеты игрока."""
    return await debit_coins(
        session,
        user_id=user_id,
        amount=amount,
        reason=reason,
        operation_type=operation_type,
        reference=reference,
        idempotency_key=idempotency_key,
        metadata=metadata,
    )

class WorkCooldownError(EconomyError):
    """Кулдаун на работу еще не истек."""


async def claim_work_reward(
    session: AsyncSession,
    *,
    user_id: int,
    base_reward: int = 250,
    cooldown_seconds: int = 3600,
) -> tuple[EconomyResult, int]:
    """Начисление награды за работу с учетом кулдауна."""
    player = await _lock_players(session, [user_id])
    p = player[user_id]

    now = datetime.now(timezone.utc)
    last_work = await session.scalar(
        select(EconomyTransaction)
        .where(
            EconomyTransaction.actor_id == user_id,
            EconomyTransaction.operation_type == "work",
        )
        .order_by(EconomyTransaction.created_at.desc())
        .limit(1)
    )

    if last_work and last_work.created_at:
        elapsed = (now - last_work.created_at).total_seconds()
        if elapsed < cooldown_seconds:
            remaining = int(cooldown_seconds - elapsed)
            raise WorkCooldownError(f"Следующую смену можно начать через {remaining // 60} мин {remaining % 60} сек")

    # Награда зависит от уровня игрока: base_reward + (level * 25)
    reward = base_reward + (p.level * 25)
    # Начисляем монеты
    res = await credit_coins(
        session,
        user_id=user_id,
        amount=reward,
        reason=f"Зарплата за смену (уровень {p.level})",
        operation_type="work",
    )
    # Даем опыт за работу
    p.experience += 25
    next_level_exp = p.level * 100
    if p.experience >= next_level_exp:
        p.level += 1
        p.experience -= next_level_exp

    return res, reward
