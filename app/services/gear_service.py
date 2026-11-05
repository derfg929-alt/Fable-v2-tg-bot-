"""Сервис снаряжения и предметов для подработок, охоты и рыбалки Fable.

Категории снаряжения:
1. Охотничье оружие (hunting_rifle): повышает шанс редкой дичи (заяц, олень, медведь, дракон) и снижает шанс промаха.
2. Удочка для рыбалки (fishing_rod): повышает шанс редкой рыбы и снижает шанс холостой поклевки.
3. Авто для такси / Рок-машина (taxi_car): увеличивает выплаты таксисту.
4. Транспорт для курьера (courier_bike): увеличивает выплаты курьеру.
5. Кофемашина для бариста (barista_machine): увеличивает выплаты бариста.
6. Экзоскелет для грузчика (loader_exosuit): увеличивает выплаты грузчику.
7. Инвентарь для уборщика (cleaner_gear): увеличивает выплаты уборщику.
"""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerGear
from app.services.economy_service import debit_coins, InsufficientFundsError, PlayerNotFoundError


class GearError(Exception):
    """Базовая ошибка снаряжения."""


@dataclass(frozen=True, slots=True)
class GearItemConfig:
    code: str
    name: str
    icon: str
    base_cost: int
    cost_multiplier: float
    description: str


GEAR_ITEMS: dict[str, GearItemConfig] = {
    "оружие": GearItemConfig(
        code="hunting_rifle",
        name="Охотничье ружьё",
        icon="🔫",
        base_cost=3_000,
        cost_multiplier=1.8,
        description="Повышает меткость охоты и открывает ценные трофеи (медведь, дракон).",
    ),
    "удочка": GearItemConfig(
        code="fishing_rod",
        name="Рыболовная удочка",
        icon="🎣",
        base_cost=2_500,
        cost_multiplier=1.8,
        description="Повышает улов на озере и шансы на золотую рыбку и сундуки.",
    ),
    "такси": GearItemConfig(
        code="taxi_car",
        name="Машина для такси (Рок-кар)",
        icon="🚕",
        base_cost=4_000,
        cost_multiplier=1.8,
        description="Улучшает класс автомобиля для таксиста, увеличивая чаевые и оплату заказов.",
    ),
    "курьер": GearItemConfig(
        code="courier_bike",
        name="Транспорт курьера",
        icon="🛵",
        base_cost=3_000,
        cost_multiplier=1.7,
        description="Скоростной электробайк курьера для быстрой доставки заказов.",
    ),
    "бариста": GearItemConfig(
        code="barista_machine",
        name="Кофемашина бариста",
        icon="☕️",
        base_cost=3_500,
        cost_multiplier=1.7,
        description="Профессиональная эспрессо-станция с капучинатором.",
    ),
    "грузчик": GearItemConfig(
        code="loader_exosuit",
        name="Экзоскелет грузчика",
        icon="🦾",
        base_cost=4_500,
        cost_multiplier=1.8,
        description="Гидравлический силовой экзоскелет для разгрузки тяжелых фур.",
    ),
    "уборщик": GearItemConfig(
        code="cleaner_gear",
        name="Инвентарь клинера",
        icon="🧼",
        base_cost=2_000,
        cost_multiplier=1.6,
        description="Профессиональное клининговое оборудование высокой эффективности.",
    ),
}

MAX_GEAR_LEVEL = 10


def get_gear_upgrade_cost(code_key: str, current_level: int) -> int:
    cfg = GEAR_ITEMS[code_key]
    return int(cfg.base_cost * (cfg.cost_multiplier ** (current_level - 1)))


def get_gear_bonus_percent(level: int) -> int:
    """Процентный бонус к выплатам за каждый уровень (+15% за ур)."""
    return (level - 1) * 15


async def get_or_create_gear(session: AsyncSession, *, user_id: int) -> PlayerGear:
    row = await session.scalar(
        select(PlayerGear).where(PlayerGear.user_id == user_id).with_for_update()
    )
    if not row:
        row = PlayerGear(
            user_id=user_id,
            hunting_rifle_level=1,
            fishing_rod_level=1,
            taxi_car_level=1,
            courier_bike_level=1,
            barista_machine_level=1,
            loader_exosuit_level=1,
            cleaner_gear_level=1,
        )
        session.add(row)
        await session.flush()
    return row


def _resolve_gear_key(name_or_code: str) -> str | None:
    norm = name_or_code.strip().lower()
    mapping = {
        "оружие": "оружие",
        "ружье": "оружие",
        "ружьё": "оружие",
        "винтовка": "оружие",
        "hunting_rifle": "оружие",
        "удочка": "удочка",
        "снасть": "удочка",
        "fishing_rod": "удочка",
        "такси": "такси",
        "машина": "такси",
        "авто": "такси",
        "рок": "такси",
        "рок-машина": "такси",
        "рок-кар": "такси",
        "taxi_car": "такси",
        "курьер": "курьер",
        "байк": "курьер",
        "велосипед": "курьер",
        "courier_bike": "курьер",
        "бариста": "бариста",
        "кофе": "бариста",
        "кофемашина": "бариста",
        "barista_machine": "бариста",
        "грузчик": "грузчик",
        "экзоскелет": "грузчик",
        "loader_exosuit": "грузчик",
        "уборщик": "уборщик",
        "клинер": "уборщик",
        "швабра": "уборщик",
        "cleaner_gear": "уборщик",
    }
    return mapping.get(norm)


async def upgrade_player_gear(
    session: AsyncSession,
    *,
    user_id: int,
    gear_type: str,
) -> tuple[str, int, int]:
    """Улучшает выбранное снаряжение игрока. Возвращает (название, новый_уровень, стоимость)."""
    key = _resolve_gear_key(gear_type)
    if not key:
        avail = ", ".join(GEAR_ITEMS.keys())
        raise GearError(f"Неизвестное снаряжение '{gear_type}'. Доступны: {avail}")

    cfg = GEAR_ITEMS[key]
    gear = await get_or_create_gear(session, user_id=user_id)
    attr_name = f"{cfg.code}_level"
    curr_lvl = getattr(gear, attr_name)

    if curr_lvl >= MAX_GEAR_LEVEL:
        raise GearError(f"{cfg.name} уже улучшено до максимального 10 уровня!")

    cost = get_gear_upgrade_cost(key, curr_lvl)
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cost:
        raise InsufficientFundsError(
            f"Недостаточно монет для улучшения {cfg.name}: требуется {cost:,} монет, у вас {player.wallet:,}".replace(",", " ")
        )

    await debit_coins(
        session,
        user_id=user_id,
        amount=cost,
        reason=f"Улучшение снаряжения: {cfg.name} до уровня {curr_lvl + 1}",
        operation_type="gear_upgrade",
        reference=f"gear:{cfg.code}",
    )

    new_lvl = curr_lvl + 1
    setattr(gear, attr_name, new_lvl)
    await session.flush()
    return cfg.name, new_lvl, cost
