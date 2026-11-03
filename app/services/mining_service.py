"""Сервис майнинг-фермы разной криптовалюты для Fable.

Механика:
- Игрок покупает разные типы майнинг-компьютеров (ригов):
  1. cpu: CPU Rig — майнит DOGE или TON (стоимость: 2 500 монет)
  2. gpu: GPU Риг — майнит ETH или SOL (стоимость: 10 000 монет)
  3. asic: ASIC Майнер — майнит BTC (стоимость: 50 000 монет)
  4. quantum: Квантовый майнер — майнит любую крипту с максимальной эффективностью (стоимость: 250 000 монет)
- Игрок может переключать активную монету для майнинга (`ферма монета [BTC|ETH|TON|SOL|DOGE]`).
- Намайненная криптовалюта начисляется напрямую в портфель реальных активов (`PlayerAsset`),
  а также генерируются вспомогательные игровые монеты.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerMining, PlayerAsset
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class MiningError(Exception):
    """Базовая ошибка майнинга."""


@dataclass(frozen=True, slots=True)
class ComputerTypeConfig:
    code: str
    name: str
    icon: str
    price: int
    hashrate: int  # относительная мощность хэшрейта
    allowed_coins: tuple[str, ...]


MINING_RIGS: dict[str, ComputerTypeConfig] = {
    "cpu": ComputerTypeConfig("cpu", "CPU Компьютер", "💻", 2_500, 10, ("DOGE", "TON")),
    "gpu": ComputerTypeConfig("gpu", "GPU Риг", "⚡️", 10_000, 50, ("ETH", "SOL", "TON")),
    "asic": ComputerTypeConfig("asic", "ASIC Майнер", "📦", 50_000, 300, ("BTC",)),
    "quantum": ComputerTypeConfig("quantum", "Квантовый суперкомпьютер", "🔮", 250_000, 2_000, ("BTC", "ETH", "TON", "SOL", "DOGE")),
}

# Базовая часовая добыча криптовалюты на 1 единицу хэшрейта
COIN_BASE_YIELD_PER_HASHRATE: dict[str, Decimal] = {
    "BTC": Decimal("0.00000005"),
    "ETH": Decimal("0.0000015"),
    "SOL": Decimal("0.00002"),
    "TON": Decimal("0.0008"),
    "DOGE": Decimal("0.02"),
}

SUPPORTED_MINING_COINS = ("BTC", "ETH", "TON", "SOL", "DOGE")
COMPUTER_PRICE = 2_500  # Дефолт для обратной совместимости
MAX_FARM_LEVEL = 10


def get_farm_upgrade_cost(current_level: int) -> int:
    return current_level * 10_000


def get_rigs_dict(mining: PlayerMining) -> dict[str, int]:
    rigs = dict(mining.rigs_json or {})
    # Поддержка старого поля `computers`
    if "cpu" not in rigs and mining.computers > 0:
        rigs["cpu"] = mining.computers
    return {k: int(v) for k, v in rigs.items() if int(v) > 0}


def calculate_effective_hashrate(rigs: dict[str, int], active_coin: str, farm_level: int) -> int:
    total_hr = 0
    coin_upper = active_coin.upper()
    for rig_code, count in rigs.items():
        if rig_code in MINING_RIGS:
            cfg = MINING_RIGS[rig_code]
            if coin_upper in cfg.allowed_coins:
                total_hr += cfg.hashrate * count
            else:
                # Универсальная остаточная мощность 10% для неспециализированных монет
                total_hr += int(cfg.hashrate * count * 0.10)
    level_mult = 1.0 + (farm_level - 1) * 0.25
    return int(total_hr * level_mult)


def get_hourly_yield(computers: int, farm_level: int) -> int:
    """Для обратной совместимости старых тестов."""
    multiplier = 1.0 + (farm_level - 1) * 0.25
    return int(computers * 25 * multiplier)


async def get_or_create_mining(session: AsyncSession, *, user_id: int) -> PlayerMining:
    row = await session.scalar(
        select(PlayerMining).where(PlayerMining.user_id == user_id).with_for_update()
    )
    if not row:
        row = PlayerMining(
            user_id=user_id,
            computers=1,
            farm_level=1,
            active_coin="BTC",
            rigs_json={"cpu": 1},
            last_mined_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
    else:
        if not row.rigs_json:
            row.rigs_json = {"cpu": max(1, row.computers)}
            session.add(row)
            await session.flush()
    return row


async def switch_mining_coin(session: AsyncSession, *, user_id: int, coin: str) -> str:
    coin_norm = coin.upper().strip()
    if coin_norm not in SUPPORTED_MINING_COINS:
        raise MiningError(f"Неизвестная криптовалюта '{coin}'. Доступны для майнинга: {', '.join(SUPPORTED_MINING_COINS)}")

    mining = await get_or_create_mining(session, user_id=user_id)
    mining.active_coin = coin_norm
    await session.flush()
    return coin_norm


async def buy_computers(
    session: AsyncSession,
    *,
    user_id: int,
    count: int = 1,
    rig_type: str = "cpu",
) -> tuple[PlayerMining, int]:
    if count <= 0:
        raise MiningError("Количество компьютеров должно быть больше нуля")

    rig_code = rig_type.lower()
    if rig_code not in MINING_RIGS:
        raise MiningError(f"Неизвестный тип рига '{rig_type}'. Доступны: {', '.join(MINING_RIGS.keys())}")

    cfg = MINING_RIGS[rig_code]
    cost = count * cfg.price

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " "))

    mining = await get_or_create_mining(session, user_id=user_id)

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Покупка {count} {cfg.name}",
        operation_type="mining_buy_computer",
    )

    rigs = get_rigs_dict(mining)
    rigs[rig_code] = rigs.get(rig_code, 0) + count
    mining.rigs_json = rigs
    mining.computers = sum(rigs.values())
    await session.flush()
    return mining, cost


async def upgrade_mining_farm(session: AsyncSession, *, user_id: int) -> tuple[PlayerMining, int]:
    mining = await get_or_create_mining(session, user_id=user_id)
    if mining.farm_level >= MAX_FARM_LEVEL:
        raise MiningError("Ферма уже достигла максимального 10 уровня!")

    cost = get_farm_upgrade_cost(mining.farm_level)
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(f"Недостаточно монет для улучшения: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " "))

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Улучшение фермы до уровня {mining.farm_level + 1}",
        operation_type="mining_upgrade_farm",
    )

    mining.farm_level += 1
    await session.flush()
    return mining, cost


@dataclass(frozen=True, slots=True)
class MiningCollectionResult:
    coins: int
    crypto_coin: str
    crypto_amount: Decimal
    total_computers: int


async def collect_mining_profit(session: AsyncSession, *, user_id: int) -> tuple[int, int]:
    """Собирает намайненные монеты и крипту. Возвращает (монеты, компьютеров) для обратной совместимости."""
    res = await collect_mining_profit_detailed(session, user_id=user_id)
    return res.coins, res.total_computers


async def collect_mining_profit_detailed(session: AsyncSession, *, user_id: int) -> MiningCollectionResult:
    """Собирает намайненные монеты и криптовалюту в портфель игрока."""
    mining = await get_or_create_mining(session, user_id=user_id)
    now = datetime.now(timezone.utc)
    last = mining.last_mined_at or now

    elapsed_hours = (now - last).total_seconds() / 3600.0
    rigs = get_rigs_dict(mining)
    total_computers = sum(rigs.values())

    active_coin = (mining.active_coin or "BTC").upper()
    if elapsed_hours < (1.0 / 60.0):
        return MiningCollectionResult(0, active_coin, Decimal("0"), total_computers)

    # Максимальный накопитель майнинга - 4 часа (как у бизнеса)
    capped_hours = min(elapsed_hours, 4.0)

    hashrate = calculate_effective_hashrate(rigs, active_coin, mining.farm_level)
    base_rate = COIN_BASE_YIELD_PER_HASHRATE.get(active_coin, Decimal("0.00000005"))
    crypto_amount = (Decimal(str(hashrate)) * base_rate * Decimal(str(capped_hours))).quantize(Decimal("0.00000001"))

    # Дополнительно бонусные игровые монеты
    coins = int(hashrate * 0.5 * capped_hours)

    if crypto_amount > 0:
        asset = await session.scalar(
            select(PlayerAsset).where(
                PlayerAsset.user_id == user_id,
                PlayerAsset.ticker == active_coin,
            ).with_for_update()
        )
        if not asset:
            asset = PlayerAsset(user_id=user_id, ticker=active_coin, amount=crypto_amount)
            session.add(asset)
        else:
            asset.amount += crypto_amount

    if coins > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=coins,
            reason=f"Сбор дохода с крипто-фермы ({active_coin})",
            operation_type="mining_profit",
        )

    mining.last_mined_at = now
    await session.flush()
    return MiningCollectionResult(coins, active_coin, crypto_amount, total_computers)
