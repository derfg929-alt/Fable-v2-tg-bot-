"""Сервис магазина и недвижимости для Fable.

10 видов недвижимости (способ престижной траты денег):
1. Коммуналка (цена: 15 000)
2. Квартира-студия (цена: 50 000)
3. Двушка в спальном районе (цена: 150 000)
4. Пентхаус в центре (цена: 500 000)
5. Таунхаус с террасой (цена: 1 500 000)
6. Загородный коттедж (цена: 5 000 000)
7. Элитный особняк (цена: 15 000 000)
8. Небоскреб Fable Tower (цена: 50 000 000)
9. Личный тропический остров (цена: 150 000 000)
10. Орбитальная космическая станция (цена: 500 000 000)

Магазин статусных вещей и транспорта:
1. Часы Rolex Submariner (цена: 10 000)
2. Спорткар Porsche 911 (цена: 100 000)
3. Суперкар Lamborghini Aventador (цена: 500 000)
4. Роскошная яхта Eclipse (цена: 3 000 000)
5. Частный джет Gulfstream G650 (цена: 20 000 000)
"""
from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerProperty, PlayerInventory
from app.services.economy_service import debit_coins, InsufficientFundsError, PlayerNotFoundError


class ShopError(Exception):
    """Базовая ошибка магазина."""


@dataclass(frozen=True, slots=True)
class PropertyConfig:
    id: int
    name: str
    icon: str
    price: int
    description: str


PROPERTIES: dict[int, PropertyConfig] = {
    1: PropertyConfig(1, "Коммуналка", "🚪", 15_000, "Уютная комната со скрипучим полом"),
    2: PropertyConfig(2, "Квартира-студия", "🛋", 50_000, "Современный минимализм для холостяка"),
    3: PropertyConfig(3, "Двушка в спальном районе", "🏢", 150_000, "Просторная квартира рядом с парком"),
    4: PropertyConfig(4, "Пентхаус в центре", "🌇", 500_000, "Панорамный вид на ночной город"),
    5: PropertyConfig(5, "Таунхаус с террасой", "🏡", 1_500_000, "Двухэтажный дом в престижном районе"),
    6: PropertyConfig(6, "Загородный коттедж", "🌲", 5_000_000, "Большая усадьба с бассейном и садом"),
    7: PropertyConfig(7, "Элитный особняк", "🏰", 15_000_000, "Мраморные залы и личная вертолетная площадка"),
    8: PropertyConfig(8, "Небоскреб Fable Tower", "🏙", 50_000_000, "Собственный небоскреб в финансовом квартале"),
    9: PropertyConfig(9, "Личный тропический остров", "🏝", 150_000_000, "Райский уголок с чистейшими пляжами"),
    10: PropertyConfig(10, "Орбитальная космическая станция", "🛰", 500_000_000, "Собственный модуль на околоземной орбите"),
}

@dataclass(frozen=True, slots=True)
class CarConfig:
    id: int
    car_key: str
    name: str
    icon: str
    price: int
    speed_kmh: int
    description: str


CARS: dict[int, CarConfig] = {
    1: CarConfig(1, "lada", "Lada Vesta Sport", "🚗", 25_000, 190, "Бодрый отечественный седан для городских улиц"),
    2: CarConfig(2, "camry", "Toyota Camry 3.5", "🚘", 75_000, 220, "Легендарный комфорт и надежность бизнес-класса"),
    3: CarConfig(3, "bmw_m5", "BMW M5 CS", "🏎", 200_000, 305, "Баварский зверь с разгоном до сотни за 3 секунды"),
    4: CarConfig(4, "porsche_911", "Porsche 911 Turbo S", "🏎", 450_000, 330, "Идеальный немецкий спорткар с безупречной управляемостью"),
    5: CarConfig(5, "mercedes_g63", "Mercedes-AMG G63 Гелендваген", "🚙", 700_000, 240, "Брутальный внедорожник настоящего криминального авторитета"),
    6: CarConfig(6, "lambo_aventador", "Lamborghini Aventador SVJ", "🏎", 1_500_000, 352, "Итальянский суперкар с ревущим атмосферным V12"),
    7: CarConfig(7, "ferrari_sf90", "Ferrari SF90 Stradale", "🏎", 3_000_000, 340, "Гибридный гиперкар мощностью в 1000 лошадиных сил"),
    8: CarConfig(8, "rolls_royce", "Rolls-Royce Phantom", "🚘", 6_000_000, 250, "Вершина мировой автомобильной роскоши и аристократизма"),
    9: CarConfig(9, "bugatti_chiron", "Bugatti Chiron Super Sport", "🚀", 15_000_000, 440, "Король скорости и технологический шедевр инженерии"),
    10: CarConfig(10, "batmobile", "Бэтмобиль Tumbler", "🦇", 40_000_000, 380, "Бронированный прототип с ракетным ускорителем"),
}

@dataclass(frozen=True, slots=True)
class ShopItemConfig:
    id: int
    item_key: str
    name: str
    icon: str
    price: int
    category: str


SHOP_ITEMS: dict[int, ShopItemConfig] = {
    1: ShopItemConfig(1, "rolex", "Часы Rolex Submariner", "⌚️", 10_000, "luxury"),
    2: ShopItemConfig(2, "porsche", "Спорткар Porsche 911", "🏎", 100_000, "transport"),
    3: ShopItemConfig(3, "lambo", "Суперкар Lamborghini Aventador", "🏎", 500_000, "transport"),
    4: ShopItemConfig(4, "yacht", "Роскошная яхта Eclipse", "🛥", 3_000_000, "transport"),
    5: ShopItemConfig(5, "jet", "Частный джет Gulfstream G650", "✈️", 20_000_000, "transport"),
}


async def buy_property(session: AsyncSession, *, user_id: int, property_id: int) -> tuple[PlayerProperty, PropertyConfig]:
    if property_id not in PROPERTIES:
        raise ShopError(f"Недвижимость #{property_id} не найдена (выберите 1..10)")

    cfg = PROPERTIES[property_id]
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cfg.price:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cfg.price:,} монет, у вас {player.wallet:,}".replace(",", " "))

    existing = await session.scalar(
        select(PlayerProperty).where(PlayerProperty.user_id == user_id, PlayerProperty.property_id == property_id)
    )
    if existing:
        raise ShopError(f"У вас уже куплена недвижимость «{cfg.name}»!")

    await debit_coins(
        session,
        user_id=user_id,
        amount=cfg.price,
        reason=f"Покупка недвижимости #{property_id} ({cfg.name})",
        operation_type="property_buy",
    )

    prop = PlayerProperty(user_id=user_id, property_id=property_id, name=cfg.name, quantity=1)
    session.add(prop)
    await session.flush()
    return prop, cfg


async def get_player_properties(session: AsyncSession, *, user_id: int) -> list[PlayerProperty]:
    res = await session.scalars(
        select(PlayerProperty).where(PlayerProperty.user_id == user_id).order_by(PlayerProperty.property_id)
    )
    return list(res.all())


async def buy_car(session: AsyncSession, *, user_id: int, car_id: int) -> tuple[PlayerInventory, CarConfig]:
    if car_id not in CARS:
        raise ShopError(f"Машина #{car_id} не найдена в автосалоне (выберите 1..{len(CARS)})")

    cfg = CARS[car_id]
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cfg.price:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cfg.price:,} монет, у вас {player.wallet:,}".replace(",", " "))

    existing = await session.scalar(
        select(PlayerInventory).where(
            PlayerInventory.user_id == user_id,
            PlayerInventory.item_id == f"car_{cfg.car_key}",
        )
    )
    if existing:
        raise ShopError(f"У вас в гараже уже есть автомобиль «{cfg.name}»!")

    await debit_coins(
        session,
        user_id=user_id,
        amount=cfg.price,
        reason=f"Покупка авто #{car_id} ({cfg.name})",
        operation_type="car_buy",
    )

    inv = PlayerInventory(
        user_id=user_id,
        item_id=f"car_{cfg.car_key}",
        item_name=cfg.name,
        category="cars",
        quantity=1,
        is_exclusive=False,
    )
    session.add(inv)
    await session.flush()
    return inv, cfg


async def get_player_cars(session: AsyncSession, *, user_id: int) -> list[PlayerInventory]:
    res = await session.scalars(
        select(PlayerInventory).where(
            PlayerInventory.user_id == user_id,
            PlayerInventory.category == "cars",
        ).order_by(PlayerInventory.id)
    )
    return list(res.all())


async def buy_shop_item(session: AsyncSession, *, user_id: int, item_id: int) -> tuple[PlayerInventory, ShopItemConfig]:
    if item_id not in SHOP_ITEMS:
        raise ShopError(f"Товар #{item_id} не найден в магазине (выберите 1..{len(SHOP_ITEMS)})")

    cfg = SHOP_ITEMS[item_id]
    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < cfg.price:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {cfg.price:,} монет, у вас {player.wallet:,}".replace(",", " "))

    inv = await session.scalar(
        select(PlayerInventory).where(PlayerInventory.user_id == user_id, PlayerInventory.item_id == cfg.item_key)
    )
    if inv:
        raise ShopError(f"У вас уже есть «{cfg.name}»!")

    await debit_coins(
        session,
        user_id=user_id,
        amount=cfg.price,
        reason=f"Покупка в магазине: {cfg.name}",
        operation_type="shop_buy",
    )

    inv = PlayerInventory(
        user_id=user_id,
        item_id=cfg.item_key,
        item_name=cfg.name,
        category=cfg.category,
        quantity=1,
        is_exclusive=False,
    )
    session.add(inv)
    await session.flush()
    return inv, cfg


async def get_player_shop_items(session: AsyncSession, *, user_id: int) -> list[PlayerInventory]:
    res = await session.scalars(
        select(PlayerInventory).where(PlayerInventory.user_id == user_id).order_by(PlayerInventory.id)
    )
    return list(res.all())
