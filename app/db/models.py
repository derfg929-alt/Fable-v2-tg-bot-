"""Global player/economy records are keyed by Telegram user ID. Chat-specific
configuration and moderation records always include chat_id in their keys.
All monetary values use integer minor units (whole coins) or fixed precision
NUMERIC for exchange prices; floating point is intentionally avoided.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index,
    Integer, Numeric, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base metadata for Alembic migrations."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(),
        onupdate=func.now(), nullable=False
    )


class Player(Base, TimestampMixin):
    """One globally shared account per Telegram user."""
    __tablename__ = "players"
    __table_args__ = (
        CheckConstraint("wallet >= 0", name="ck_players_wallet_nonnegative"),
        CheckConstraint("bank_balance >= 0", name="ck_players_bank_nonnegative"),
        CheckConstraint("experience >= 0", name="ck_players_experience_nonnegative"),
        CheckConstraint("level >= 1", name="ck_players_level_positive"),
        CheckConstraint("reputation >= 0", name="ck_players_reputation_nonnegative"),
        CheckConstraint("shares >= 0", name="ck_players_shares_nonnegative"),
    )

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    last_name: Mapped[str | None] = mapped_column(String(128))
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    wallet: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bank_balance: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    experience: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reputation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_globally_banned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    global_ban_reason: Mapped[str | None] = mapped_column(Text)
    global_ban_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chat(Base, TimestampMixin):
    __tablename__ = "chats"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    chat_type: Mapped[str] = mapped_column(String(32), nullable=False, default="supergroup")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    log_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ChatAdmin(Base, TimestampMixin):
    __tablename__ = "chat_admins"
    __table_args__ = (
        UniqueConstraint("chat_id", "user_id", name="uq_chat_admin_chat_user"),
        CheckConstraint("level BETWEEN 2 AND 5", name="ck_chat_admin_level"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"), index=True
    )
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    assigned_by: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ChatRule(Base, TimestampMixin):
    __tablename__ = "chat_rules"
    __table_args__ = (UniqueConstraint("chat_id", "position", name="uq_rule_chat_position"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ChatLinkWhitelist(Base):
    __tablename__ = "chat_link_whitelist"
    __table_args__ = (UniqueConstraint("chat_id", "domain", name="uq_chat_whitelist_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"), index=True
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    added_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RuleAcceptance(Base):
    __tablename__ = "rule_acceptances"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", name="uq_rule_acceptance"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ActivityRecord(Base):
    __tablename__ = "activity_records"
    __table_args__ = (
        Index("ix_activity_chat_created", "chat_id", "created_at"),
        Index("ix_activity_user_created", "user_id", "created_at"),
        CheckConstraint("points >= 0", name="ck_activity_points_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    points: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reward_coins: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EconomyTransaction(Base):
    """Immutable audit ledger for all global coin movements."""
    __tablename__ = "economy_transactions"
    __table_args__ = (
        Index("ix_economy_actor_created", "actor_id", "created_at"),
        Index("ix_economy_recipient_created", "recipient_id", "created_at"),
        CheckConstraint("amount >= 0", name="ck_economy_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    recipient_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    fee: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actor_balance_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    recipient_balance_delta: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reason: Mapped[str | None] = mapped_column(Text)
    reference: Mapped[str | None] = mapped_column(String(128), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    metadata_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BankInterestRun(Base):
    __tablename__ = "bank_interest_runs"
    __table_args__ = (UniqueConstraint("user_id", "period_date", name="uq_bank_interest_user_period"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    period_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    principal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False)
    interest: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReputationAction(Base):
    __tablename__ = "reputation_actions"
    __table_args__ = (
        Index("ix_reputation_actor_created", "actor_id", "created_at"),
        Index("ix_reputation_target_created", "target_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Achievement(Base, TimestampMixin):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reward_coins: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class PlayerAchievement(Base):
    __tablename__ = "player_achievements"
    __table_args__ = (UniqueConstraint("user_id", "achievement_id", name="uq_player_achievement"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    achievement_id: Mapped[int] = mapped_column(Integer, ForeignKey("achievements.id", ondelete="CASCADE"))
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MarketOrder(Base, TimestampMixin):
    __tablename__ = "market_orders"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_market_order_quantity_positive"),
        CheckConstraint("remaining_quantity >= 0 AND remaining_quantity <= quantity", name="ck_market_order_remaining"),
        CheckConstraint("limit_price > 0", name="ck_market_order_price_positive"),
        Index("ix_market_book", "side", "status", "limit_price", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    side: Mapped[str] = mapped_column(String(8), nullable=False)  # buy / sell
    order_type: Mapped[str] = mapped_column(String(12), nullable=False, default="limit")
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remaining_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    reserved_coins: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reserved_assets: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    is_market_maker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)


class MarketTrade(Base):
    __tablename__ = "market_trades"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_market_trade_quantity_positive"),
        CheckConstraint("price > 0", name="ck_market_trade_price_positive"),
        Index("ix_market_trade_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    buy_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("market_orders.id", ondelete="RESTRICT"))
    sell_order_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("market_orders.id", ondelete="RESTRICT"))
    buyer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    quantity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    buyer_fee: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    seller_fee: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MarketQuote(Base):
    __tablename__ = "market_quotes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    demand: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    supply: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class MarketMakerState(Base, TimestampMixin):
    __tablename__ = "market_maker_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False, default=Decimal("1"))
    spread: Mapped[Decimal] = mapped_column(Numeric(8, 5), nullable=False, default=Decimal("0.02"))
    coin_reserve: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    asset_reserve: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    parameters: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class GameSession(Base):
    __tablename__ = "game_sessions"
    __table_args__ = (Index("ix_game_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    game_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="finished")
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    opponent_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    stake: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    payout: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    result: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Clan(Base, TimestampMixin):
    __tablename__ = "clans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    creator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="RESTRICT"))
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rating: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ClanMember(Base):
    __tablename__ = "clan_members"
    __table_args__ = (UniqueConstraint("clan_id", "user_id", name="uq_clan_member"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"), unique=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClanMessage(Base):
    __tablename__ = "clan_messages"
    __table_args__ = (Index("ix_clan_messages_clan_created", "clan_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    clan_id: Mapped[int] = mapped_column(Integer, ForeignKey("clans.id", ondelete="CASCADE"))
    sender_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    sender_name: Mapped[str] = mapped_column(String(256), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ClanAudit(Base):
    __tablename__ = "clan_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    clan_id: Mapped[int | None] = mapped_column(Integer)
    clan_name: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModerationCase(Base):
    __tablename__ = "moderation_cases"
    __table_args__ = (Index("ix_moderation_chat_target", "chat_id", "target_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"))
    target_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    moderator_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="RESTRICT"))
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # warn/mute/ban/kick/unmute/unban
    reason: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    revoked_by: Mapped[int | None] = mapped_column(BigInteger)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ModerationReport(Base):
    __tablename__ = "moderation_reports"
    __table_args__ = (Index("ix_reports_chat_status", "chat_id", "status", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"))
    reporter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    target_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="SET NULL"))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    message_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    handled_by: Mapped[int | None] = mapped_column(BigInteger)
    resolution: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatActivityStats(Base):
    __tablename__ = "chat_activity_stats"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", "period", name="uq_chat_activity_period"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    message_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    activity_points: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CaptchaChallenge(Base):
    __tablename__ = "captcha_challenges"
    __table_args__ = (Index("ix_captcha_chat_user_status", "chat_id", "user_id", "status"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chats.telegram_id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"))
    challenge_token: Mapped[str] = mapped_column(String(96), unique=True, nullable=False)
    expected_answer: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminActionLog(Base):
    __tablename__ = "admin_action_logs"
    __table_args__ = (Index("ix_admin_logs_chat_created", "chat_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    actor_id: Mapped[int | None] = mapped_column(BigInteger)
    target_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PlayerBusiness(Base, TimestampMixin):
    __tablename__ = "player_businesses"
    __table_args__ = (
        UniqueConstraint("user_id", "business_id", name="uq_player_business"),
        CheckConstraint("level >= 1", name="ck_business_level_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"), index=True)
    business_id: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 to 10
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlayerMining(Base, TimestampMixin):
    __tablename__ = "player_mining"
    __table_args__ = (
        CheckConstraint("computers >= 0", name="ck_mining_computers_nonnegative"),
        CheckConstraint("farm_level >= 1", name="ck_mining_farm_level_positive"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"), unique=True, index=True)
    computers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    farm_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_coin: Mapped[str] = mapped_column(String(16), nullable=False, default="BTC")
    rigs_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_mined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PlayerAsset(Base, TimestampMixin):
    """Assets owned by player: Cryptocurrencies and Company Stocks."""
    __tablename__ = "player_assets"
    __table_args__ = (
        UniqueConstraint("user_id", "ticker", name="uq_player_asset_ticker"),
        CheckConstraint("amount >= 0", name="ck_asset_amount_nonnegative"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"), index=True)
    ticker: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False, default=Decimal("0"))


class DailyBonus(Base, TimestampMixin):
    """Tracking player daily bonus streaks and last claim times."""
    __tablename__ = "daily_bonuses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("players.telegram_id", ondelete="CASCADE"), unique=True, index=True)
    streak: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GlobalSetting(Base, TimestampMixin):
    __tablename__ = "global_settings"

    key: Mapped[str] = mapped_column(String(96), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class OwnerBroadcast(Base):
    __tablename__ = "owner_broadcasts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    total_chats: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    results: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
