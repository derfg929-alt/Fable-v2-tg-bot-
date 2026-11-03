"""Переводы монет между глобальными профилями игроков.

Правила из ТЗ:
- налог 0,5% при сумме перевода до 10 000 монет;
- налог 1% при сумме перевода свыше 10 000 монет;
- отправитель теряет всю указанную сумму;
- получатель получает сумму за вычетом налога.

Сумма налога округляется вниз до целой монеты. Сервис не делает commit:
операция должна быть завершена вызывающим кодом в одной транзакции.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EconomyTransaction, Player
from app.services.economy_service import (
    InsufficientFundsError,
    InvalidAmountError,
    PlayerNotFoundError,
    SelfTransferError,
)


@dataclass(frozen=True, slots=True)
class TransferResult:
    transaction: EconomyTransaction
    sender_id: int
    recipient_id: int
    gross_amount: int
    tax: int
    received_amount: int
    sender_balance: int
    recipient_balance: int
    duplicate: bool = False


def calculate_transfer_tax(amount: int) -> tuple[int, Decimal]:
    """Возвращает (налог в монетах, ставка) по сумме перевода."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise InvalidAmountError("Сумма перевода должна быть целым числом больше нуля")

    rate = Decimal("0.005") if amount <= 10_000 else Decimal("0.01")
    tax = int((Decimal(amount) * rate).to_integral_value(rounding=ROUND_DOWN))
    return tax, rate


async def transfer_coins_with_tax(
    session: AsyncSession,
    *,
    sender_id: int,
    recipient_id: int,
    amount: int,
    idempotency_key: str | None = None,
    reference: str | None = None,
) -> TransferResult:
    """Выполняет перевод с налогом атомарно и защищает от повторов."""
    tax, rate = calculate_transfer_tax(amount)

    if sender_id <= 0 or recipient_id <= 0:
        raise ValueError("Telegram ID должны быть положительными числами")
    if sender_id == recipient_id:
        raise SelfTransferError("Нельзя переводить монеты самому себе")
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise ValueError("Ключ идемпотентности должен содержать 1–128 символов")

    # Блокируем строки в одном и том же порядке во всех переводах:
    # это снижает вероятность deadlock при встречных переводах.
    ids = sorted((sender_id, recipient_id))
    result = await session.execute(
        select(Player)
        .where(Player.telegram_id.in_(ids))
        .order_by(Player.telegram_id)
        .with_for_update()
    )
    players = {player.telegram_id: player for player in result.scalars().all()}

    if sender_id not in players or recipient_id not in players:
        missing = [
            str(user_id) for user_id in ids if user_id not in players
        ]
        raise PlayerNotFoundError(
            "Не найдены профили игроков: " + ", ".join(missing)
        )

    sender = players[sender_id]
    recipient = players[recipient_id]

    # Проверяем идемпотентность после блокировки профилей: повторный запрос
    # не должен списывать деньги второй раз.
    if idempotency_key is not None:
        existing = await session.scalar(
            select(EconomyTransaction).where(
                EconomyTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is not None:
            if (
                existing.operation_type != "transfer"
                or existing.actor_id != sender_id
                or existing.recipient_id != recipient_id
                or existing.amount != amount
            ):
                raise ValueError(
                    "Ключ идемпотентности уже использован для другой операции"
                )
            existing_tax = int(existing.fee)
            return TransferResult(
                transaction=existing,
                sender_id=sender_id,
                recipient_id=recipient_id,
                gross_amount=int(existing.amount),
                tax=existing_tax,
                received_amount=int(existing.amount) - existing_tax,
                sender_balance=sender.wallet,
                recipient_balance=recipient.wallet,
                duplicate=True,
            )

    if sender.wallet < amount:
        raise InsufficientFundsError(
            f"Недостаточно монет: доступно {sender.wallet}, требуется {amount}"
        )

    received_amount = amount - tax
    transaction_values = {
        "operation_type": "transfer",
        "amount": amount,
        "actor_id": sender_id,
        "recipient_id": recipient_id,
        "fee": tax,
        "actor_balance_delta": -amount,
        "recipient_balance_delta": received_amount,
        "reason": "Перевод монет с налогом",
        "reference": reference,
        "idempotency_key": idempotency_key,
        "metadata_json": {
            "tax_rate": str(rate),
            "gross_amount": amount,
            "tax_amount": tax,
            "received_amount": received_amount,
        },
    }

    statement = insert(EconomyTransaction).values(**transaction_values)
    if idempotency_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=[EconomyTransaction.idempotency_key]
        )

    transaction_id = (
        await session.execute(statement.returning(EconomyTransaction.id))
    ).scalar_one_or_none()

    if transaction_id is None:
        # Возможен конфликт с запросом, пришедшим из другого процесса.
        if idempotency_key is None:
            raise RuntimeError("Не удалось сохранить запись перевода")
        existing = await session.scalar(
            select(EconomyTransaction).where(
                EconomyTransaction.idempotency_key == idempotency_key
            )
        )
        if existing is None:
            raise RuntimeError("Конфликт ключа идемпотентности без записи")
        if (
            existing.operation_type != "transfer"
            or existing.actor_id != sender_id
            or existing.recipient_id != recipient_id
            or existing.amount != amount
        ):
            raise ValueError(
                "Ключ идемпотентности уже использован для другой операции"
            )
        existing_tax = int(existing.fee)
        return TransferResult(
            existing, sender_id, recipient_id, int(existing.amount),
            existing_tax, int(existing.amount) - existing_tax,
            sender.wallet, recipient.wallet, True
        )

    transaction = await session.get(EconomyTransaction, transaction_id)
    if transaction is None:
        raise RuntimeError("Запись перевода создана, но не найдена")

    # Все три изменения (ledger + оба баланса) коммитятся одной транзакцией.
    sender.wallet -= amount
    recipient.wallet += received_amount

    return TransferResult(
        transaction=transaction,
        sender_id=sender_id,
        recipient_id=recipient_id,
        gross_amount=amount,
        tax=tax,
        received_amount=received_amount,
        sender_balance=sender.wallet,
        recipient_balance=recipient.wallet,
    )
