"""Сервис космодрома, покупки ракет и космических экспедиций для Fable.

Механики:
- Игрок строит и развивает космодром (уровни 1..10).
- Игрок покупает ракеты разного класса:
  1. Восток-1 (vostok): базовая ракета (стоимость: 25 000 монет, доход: 1 200/ч)
  2. Союз-2 (soyuz): надежная рабочая ракета (стоимость: 100 000 монет, доход: 4 500/ч)
  3. Falcon-9 (falcon): многоразовая ракета (стоимость: 400 000 монет, доход: 16 000/ч)
  4. Протон-М (proton): тяжелый ракетоноситель (стоимость: 1 500 000 монет, доход: 55 000/ч)
  5. Starship (starship): сверхтяжелая система колонизации (стоимость: 6 000 000 монет, доход: 200 000/ч)
  6. Сатурн-5 (saturn): легендарный лунный гигант (стоимость: 25 000 000 монет, доход: 750 000/ч)
- Расчет и детальное расписывание возможностей прибыли:
  доход в час, доход за 4 часа (максимум накопления), окупаемость.
- Экспедиции и запуски: приносят мгновенный космический бонус и увеличивают счётчик запусков.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerCosmodrome
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class CosmodromeError(Exception):
    """Базовая ошибка космодрома."""


@dataclass(frozen=True, slots=True)
class RocketConfig:
    code: str
    name: str
    icon: str
    price: int
    hourly_income: int
    min_cosmodrome_level: int
    description: str


ROCKETS: dict[str, RocketConfig] = {
    "vostok": RocketConfig(
        code="vostok",
        name="Восток-1",
        icon="🚀",
        price=25_000,
        hourly_income=1_200,
        min_cosmodrome_level=1,
        description="Первая легендарная ракета для суборбитальных миссий.",
    ),
    "soyuz": RocketConfig(
        code="soyuz",
        name="Союз-2",
        icon="🛸",
        price=100_000,
        hourly_income=4_500,
        min_cosmodrome_level=2,
        description="Надежный рабочий корабль для доставки грузов на МКС.",
    ),
    "falcon": RocketConfig(
        code="falcon",
        name="Falcon-9",
        icon="🛰",
        price=400_000,
        hourly_income=16_000,
        min_cosmodrome_level=3,
        description="Многоразовая ракета с управляемой посадкой ступени.",
    ),
    "proton": RocketConfig(
        code="proton",
        name="Протон-М",
        icon="☄️",
        price=1_500_000,
        hourly_income=55_000,
        min_cosmodrome_level=4,
        description="Тяжелая ракета-носитель для вывода спутников на геостационар.",
    ),
    "starship": RocketConfig(
        code="starship",
        name="Starship",
        icon="🌌",
        price=6_000_000,
        hourly_income=200_000,
        min_cosmodrome_level=5,
        description="Сверхтяжелый межпланетный транспорт для миссий на Марс.",
    ),
    "saturn": RocketConfig(
        code="saturn",
        name="Сатурн-5",
        icon="🪐",
        price=25_000_000,
        hourly_income=750_000,
        min_cosmodrome_level=6,
        description="Легендарный лунный гигант максимальной коммерческой отдачи.",
    ),
}

MAX_COSMODROME_LEVEL = 10
MAX_ACCUMULATION_HOURS = 4.0


def get_cosmodrome_upgrade_cost(current_level: int) -> int:
    return current_level * 50_000


def get_rockets_dict(cosmodrome: PlayerCosmodrome) -> dict[str, int]:
    raw = dict(cosmodrome.rockets_json or {})
    return {k: int(v) for k, v in raw.items() if int(v) > 0}


def calculate_hourly_income(rockets: dict[str, int], cosmodrome_level: int) -> int:
    base = sum(ROCKETS[code].hourly_income * count for code, count in rockets.items() if code in ROCKETS)
    multiplier = 1.0 + (cosmodrome_level - 1) * 0.20  # +20% за каждый уровень космодрома
    return int(base * multiplier)


async def get_or_create_cosmodrome(session: AsyncSession, *, user_id: int) -> PlayerCosmodrome:
    row = await session.scalar(
        select(PlayerCosmodrome).where(PlayerCosmodrome.user_id == user_id).with_for_update()
    )
    if not row:
        row = PlayerCosmodrome(
            user_id=user_id,
            level=1,
            rockets_json={},
            total_launches=0,
            last_collected_at=datetime.now(timezone.utc),
        )
        session.add(row)
        await session.flush()
    return row


async def buy_rocket(
    session: AsyncSession,
    *,
    user_id: int,
    rocket_code: str,
    count: int = 1,
) -> tuple[PlayerCosmodrome, RocketConfig, int]:
    if count <= 0:
        raise CosmodromeError("Количество ракет должно быть больше нуля!")

    code_norm = rocket_code.strip().lower()
    if code_norm not in ROCKETS:
        available = ", ".join(ROCKETS.keys())
        raise CosmodromeError(f"Неизвестная ракета '{rocket_code}'. Доступные типы: {available}")

    cfg = ROCKETS[code_norm]
    cosmo = await get_or_create_cosmodrome(session, user_id=user_id)
    if cosmo.level < cfg.min_cosmodrome_level:
        raise CosmodromeError(
            f"Для покупки ракеты {cfg.name} требуется уровень космодрома {cfg.min_cosmodrome_level}! "
            f"(У вас сейчас {cosmo.level} ур.)"
        )

    total_cost = cfg.price * count
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < total_cost:
        raise InsufficientFundsError(
            f"Недостаточно монет: требуется {total_cost:,} монет, у вас {player.wallet:,}".replace(",", " ")
        )

    await debit_coins(
        session,
        user_id=user_id,
        amount=total_cost,
        reason=f"Покупка ракет {cfg.name} ({count} шт.)",
        operation_type="cosmodrome_buy_rocket",
        reference=f"rocket:{code_norm}",
    )

    rockets = get_rockets_dict(cosmo)
    rockets[code_norm] = rockets.get(code_norm, 0) + count
    cosmo.rockets_json = rockets
    await session.flush()
    return cosmo, cfg, total_cost


async def upgrade_cosmodrome(session: AsyncSession, *, user_id: int) -> tuple[PlayerCosmodrome, int]:
    cosmo = await get_or_create_cosmodrome(session, user_id=user_id)
    if cosmo.level >= MAX_COSMODROME_LEVEL:
        raise CosmodromeError("Космодром уже улучшен до максимального (10) уровня!")

    cost = get_cosmodrome_upgrade_cost(cosmo.level)
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(
            f"Недостаточно монет для улучшения: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " ")
        )

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Улучшение космодрома до уровня {cosmo.level + 1}",
        operation_type="cosmodrome_upgrade",
    )

    cosmo.level += 1
    await session.flush()
    return cosmo, cost


async def collect_cosmodrome_profits(session: AsyncSession, *, user_id: int) -> tuple[int, int]:
    """Собирает пассивную прибыль от коммерческих полетов всех ракет (до 4 часов накопления)."""
    cosmo = await get_or_create_cosmodrome(session, user_id=user_id)
    rockets = get_rockets_dict(cosmo)
    total_rockets = sum(rockets.values())
    if total_rockets == 0:
        return 0, 0

    now = datetime.now(timezone.utc)
    last = cosmo.last_collected_at or now
    elapsed_hours = (now - last).total_seconds() / 3600.0

    if elapsed_hours < (1.0 / 60.0):  # менее 1 минуты
        return 0, total_rockets

    capped_hours = min(elapsed_hours, MAX_ACCUMULATION_HOURS)
    hourly = calculate_hourly_income(rockets, cosmo.level)
    profit = int(hourly * capped_hours)

    if profit > 0:
        await credit_coins(
            session,
            user_id=user_id,
            amount=profit,
            reason="Сбор прибыли с коммерческих рейсов космодрома",
            operation_type="cosmodrome_profit",
        )
        cosmo.last_collected_at = now
        await session.flush()

    return profit, total_rockets


@dataclass(frozen=True, slots=True)
class LaunchMissionResult:
    rocket_name: str
    reward_coins: int
    reward_exp: int
    is_success: bool
    story: str


async def launch_rocket_mission(session: AsyncSession, *, user_id: int, rocket_code: str | None = None) -> LaunchMissionResult:
    """Запуск отдельной космической экспедиции."""
    cosmo = await get_or_create_cosmodrome(session, user_id=user_id)
    rockets = get_rockets_dict(cosmo)
    if not rockets:
        raise CosmodromeError("На вашем космодроме нет ни одной ракеты! Купите ракету: <code>космодром купить vostok</code>")

    if rocket_code:
        code_norm = rocket_code.strip().lower()
        if code_norm not in rockets:
            raise CosmodromeError(f"У вас нет ракеты '{rocket_code}' в ангаре!")
    else:
        # Выбираем сильнейшую доступную ракету
        code_norm = list(rockets.keys())[-1]

    cfg = ROCKETS[code_norm]
    # Награда за экспедицию зависит от стоимости ракеты
    base_launch_reward = int(cfg.hourly_income * random.uniform(1.2, 2.5))
    exp = 25 * cfg.min_cosmodrome_level

    stories = [
        f"Ракета <b>{cfg.name}</b> успешно вывела спутниковую группировку на орбиту!",
        f"Миссия к лунной исследовательской базе завершилась успехом!",
        f"Космический корабль доставил научные образцы с околоземной станции!",
    ]
    story = random.choice(stories)

    await credit_coins(
        session,
        user_id=user_id,
        amount=base_launch_reward,
        reason=f"Успешный запуск ракеты {cfg.name}",
        operation_type="cosmodrome_mission",
    )

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if player:
        player.experience += exp

    cosmo.total_launches += 1
    await session.flush()

    return LaunchMissionResult(
        rocket_name=cfg.name,
        reward_coins=base_launch_reward,
        reward_exp=exp,
        is_success=True,
        story=story,
    )
