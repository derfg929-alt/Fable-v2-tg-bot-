"""Сервис биржи реальных активов Fable: 10 криптовалют и 10 акций реальных компаний.
Цены 1 к 1 с реальным рынком (в монетах/USD).

Криптовалюты (10 шт):
1. BTC (Bitcoin) ~ 95,000
2. ETH (Ethereum) ~ 2,700
3. TON (Toncoin) ~ 5.5
4. SOL (Solana) ~ 190
5. BNB (BNB) ~ 650
6. XRP (Ripple) ~ 2.4
7. DOGE (Dogecoin) ~ 0.25
8. ADA (Cardano) ~ 0.75
9. TRX (Tron) ~ 0.24
10. DOT (Polkadot) ~ 8.5

Акции компаний (10 шт):
1. AAPL (Apple Inc.) ~ 230
2. MSFT (Microsoft) ~ 420
3. NVDA (NVIDIA) ~ 130
4. AMZN (Amazon) ~ 210
5. GOOGL (Alphabet / Google) ~ 180
6. TSLA (Tesla) ~ 340
7. META (Meta Platforms) ~ 660
8. SBER (Сбербанк) ~ 260
9. GAZP (Газпром) ~ 130
10. YNDX (Яндекс) ~ 4,200
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
import aiohttp
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Player, PlayerAsset
from app.services.economy_service import debit_coins, credit_coins, InsufficientFundsError, PlayerNotFoundError


class AssetError(Exception):
    """Базовая ошибка работы с активами."""


@dataclass(frozen=True, slots=True)
class AssetInfo:
    ticker: str
    name: str
    category: str  # 'crypto' or 'stock'
    default_price: Decimal
    icon: str


MARKET_ASSETS: dict[str, AssetInfo] = {
    # Криптовалюты (10)
    "BTC": AssetInfo("BTC", "Bitcoin", "crypto", Decimal("95000"), "🪙"),
    "ETH": AssetInfo("ETH", "Ethereum", "crypto", Decimal("2700"), "💎"),
    "TON": AssetInfo("TON", "Toncoin", "crypto", Decimal("5.5"), "💎"),
    "SOL": AssetInfo("SOL", "Solana", "crypto", Decimal("190"), "☀️"),
    "BNB": AssetInfo("BNB", "BNB", "crypto", Decimal("650"), "🔶"),
    "XRP": AssetInfo("XRP", "Ripple", "crypto", Decimal("2.4"), "💧"),
    "DOGE": AssetInfo("DOGE", "Dogecoin", "crypto", Decimal("0.25"), "🐕"),
    "ADA": AssetInfo("ADA", "Cardano", "crypto", Decimal("0.75"), "🔵"),
    "TRX": AssetInfo("TRX", "TRON", "crypto", Decimal("0.24"), "🔴"),
    "DOT": AssetInfo("DOT", "Polkadot", "crypto", Decimal("8.5"), "🟣"),

    # Акции компаний (10)
    "AAPL": AssetInfo("AAPL", "Apple", "stock", Decimal("230"), "🍏"),
    "MSFT": AssetInfo("MSFT", "Microsoft", "stock", Decimal("420"), "🪟"),
    "NVDA": AssetInfo("NVDA", "NVIDIA", "stock", Decimal("130"), "🟢"),
    "AMZN": AssetInfo("AMZN", "Amazon", "stock", Decimal("210"), "📦"),
    "GOOGL": AssetInfo("GOOGL", "Google", "stock", Decimal("180"), "🔍"),
    "TSLA": AssetInfo("TSLA", "Tesla", "stock", Decimal("340"), "⚡️"),
    "META": AssetInfo("META", "Meta", "stock", Decimal("660"), "👓"),
    "SBER": AssetInfo("SBER", "Сбербанк", "stock", Decimal("260"), "🏦"),
    "GAZP": AssetInfo("GAZP", "Газпром", "stock", Decimal("130"), "🔥"),
    "YNDX": AssetInfo("YNDX", "Яндекс", "stock", Decimal("4200"), "🚕"),
}

# Кэш реальных цен
_cached_prices: dict[str, Decimal] = {}
_last_fetch_time: datetime | None = None


async def fetch_real_prices() -> dict[str, Decimal]:
    """Получает актуальные котировки или использует кэш/дефолтные цены."""
    global _cached_prices, _last_fetch_time
    now = datetime.now(timezone.utc)
    if _cached_prices and _last_fetch_time and (now - _last_fetch_time).total_seconds() < 120:
        return _cached_prices

    prices = {k: v.default_price for k, v in MARKET_ASSETS.items()}

    # Запрос к публичному API CoinGecko / Binance для крипты
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as session:
            url = "https://api.binance.com/api/v3/ticker/price"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    binance_map = {
                        "BTCUSDT": "BTC",
                        "ETHUSDT": "ETH",
                        "TONUSDT": "TON",
                        "SOLUSDT": "SOL",
                        "BNBUSDT": "BNB",
                        "XRPUSDT": "XRP",
                        "DOGEUSDT": "DOGE",
                        "ADAUSDT": "ADA",
                        "TRXUSDT": "TRX",
                        "DOTUSDT": "DOT",
                    }
                    for item in data:
                        sym = item.get("symbol")
                        if sym in binance_map:
                            ticker = binance_map[sym]
                            val = Decimal(str(item.get("price", "0")))
                            if val > 0:
                                prices[ticker] = val.quantize(Decimal("0.0001"))
    except Exception:
        pass

    _cached_prices = prices
    _last_fetch_time = now
    return prices


async def get_asset_price(ticker: str) -> Decimal:
    norm_ticker = ticker.upper()
    if norm_ticker not in MARKET_ASSETS:
        raise AssetError(f"Актив {norm_ticker} не найден в листинге биржи.")
    prices = await fetch_real_prices()
    return prices.get(norm_ticker, MARKET_ASSETS[norm_ticker].default_price)


async def buy_asset(
    session: AsyncSession,
    *,
    user_id: int,
    ticker: str,
    quantity: Decimal,
) -> tuple[Decimal, int]:
    """Покупка крипты или акций. Возвращает (цена_за_ед, общая_стоимость_монет)."""
    norm_ticker = ticker.upper()
    if norm_ticker not in MARKET_ASSETS:
        raise AssetError(f"Неизвестный тикер '{ticker}'. Доступны: {', '.join(MARKET_ASSETS.keys())}")

    if quantity <= 0:
        raise AssetError("Количество для покупки должно быть больше нуля")

    price = await get_asset_price(norm_ticker)
    total_cost = int((quantity * price).to_integral_value(rounding=ROUND_DOWN))
    if total_cost <= 0:
        total_cost = 1

    player = await session.scalar(select(Player).where(Player.telegram_id == user_id).with_for_update())
    if not player:
        raise PlayerNotFoundError(f"Игрок {user_id} не найден")

    if player.wallet < total_cost:
        raise InsufficientFundsError(f"Недостаточно монет: требуется {total_cost:,} монет, у вас {player.wallet:,}".replace(",", " "))

    await debit_coins(
        session,
        user_id=user_id,
        amount=total_cost,
        reason=f"Покупка {quantity} {norm_ticker} по курсу {price}",
        operation_type=f"market_buy_{norm_ticker.lower()}",
    )

    asset = await session.scalar(
        select(PlayerAsset).where(
            PlayerAsset.user_id == user_id,
            PlayerAsset.ticker == norm_ticker,
        ).with_for_update()
    )
    if not asset:
        asset = PlayerAsset(user_id=user_id, ticker=norm_ticker, amount=quantity)
        session.add(asset)
    else:
        asset.amount += quantity

    await session.flush()
    return price, total_cost


async def sell_asset(
    session: AsyncSession,
    *,
    user_id: int,
    ticker: str,
    quantity: Decimal,
) -> tuple[Decimal, int]:
    """Продажа крипты или акций. Возвращает (цена_за_ед, полученные_монеты)."""
    norm_ticker = ticker.upper()
    if norm_ticker not in MARKET_ASSETS:
        raise AssetError(f"Неизвестный тикер '{ticker}'")

    if quantity <= 0:
        raise AssetError("Количество для продажи должно быть больше нуля")

    asset = await session.scalar(
        select(PlayerAsset).where(
            PlayerAsset.user_id == user_id,
            PlayerAsset.ticker == norm_ticker,
        ).with_for_update()
    )
    if not asset or asset.amount < quantity:
        current_amount = asset.amount if asset else 0
        raise AssetError(f"Недостаточно активов: у вас {current_amount} {norm_ticker}, требуется {quantity}")

    price = await get_asset_price(norm_ticker)
    total_revenue = int((quantity * price).to_integral_value(rounding=ROUND_DOWN))
    if total_revenue <= 0:
        total_revenue = 1

    asset.amount -= quantity

    await credit_coins(
        session,
        user_id=user_id,
        amount=total_revenue,
        reason=f"Продажа {quantity} {norm_ticker} по курсу {price}",
        operation_type=f"market_sell_{norm_ticker.lower()}",
    )

    await session.flush()
    return price, total_revenue


async def get_player_portfolio(session: AsyncSession, *, user_id: int) -> list[tuple[AssetInfo, Decimal, Decimal]]:
    """Возвращает портфель пользователя: list of (asset_info, amount, total_val_in_coins)."""
    assets = await session.scalars(
        select(PlayerAsset).where(
            PlayerAsset.user_id == user_id,
            PlayerAsset.amount > 0,
        )
    )
    prices = await fetch_real_prices()
    res = []
    for a in assets.all():
        if a.ticker in MARKET_ASSETS:
            info = MARKET_ASSETS[a.ticker]
            curr_price = prices.get(a.ticker, info.default_price)
            val = a.amount * curr_price
            res.append((info, a.amount, val))
    return res
