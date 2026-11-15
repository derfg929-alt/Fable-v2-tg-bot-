"""Fable Randomizer: contest settings, tickets, contest referrals & leaderboard

Adds the growth-mechanics data model from the spec:
- `contests.settings_json` (JSON only) — all flexible contest toggles.
- `entries.tickets` / `entries.conditions_met` / `entries.conditions_met_at`.
- `ticket_logs` — idempotent ticket ledger.
- `contest_referrals` — referral credited only after the invited user acts.
- `contest_leaderboard` — materialized top inviters.
- `winners.is_reserve` / `winners.status` / `winners.notified_at` (reserve winners).
- `draw_logs.salt` / `draw_logs.participants_hash` (transparency).

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-15 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "contests",
        sa.Column("settings_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )

    op.add_column("entries", sa.Column("tickets", sa.Integer(), nullable=False, server_default="1"))
    op.add_column(
        "entries", sa.Column("conditions_met", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("entries", sa.Column("conditions_met_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "ticket_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("contest_id", sa.BigInteger(), sa.ForeignKey("contests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("players.telegram_id", ondelete="CASCADE"), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("reason", sa.String(32), nullable=False, server_default="base"),
        sa.Column("reason_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("contest_id", "user_id", "reason_key", name="uq_ticket_log_event"),
    )
    op.create_index("ix_ticket_logs_contest_id", "ticket_logs", ["contest_id"])
    op.create_index("ix_ticket_logs_user_id", "ticket_logs", ["user_id"])

    op.create_table(
        "contest_referrals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("contest_id", sa.BigInteger(), sa.ForeignKey("contests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("referrer_id", sa.BigInteger(), sa.ForeignKey("players.telegram_id", ondelete="CASCADE"), nullable=False),
        sa.Column("referred_id", sa.BigInteger(), sa.ForeignKey("players.telegram_id", ondelete="CASCADE"), nullable=False),
        sa.Column("tickets_awarded", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("credited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("contest_id", "referred_id", name="uq_contest_referral_referred"),
    )
    op.create_index("ix_contest_referrals_contest_id", "contest_referrals", ["contest_id"])
    op.create_index("ix_contest_referrals_referrer_id", "contest_referrals", ["referrer_id"])
    op.create_index("ix_contest_referrals_referred_id", "contest_referrals", ["referred_id"])

    op.create_table(
        "contest_leaderboard",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("contest_id", sa.BigInteger(), sa.ForeignKey("contests.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("players.telegram_id", ondelete="CASCADE"), nullable=False),
        sa.Column("invited_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("contest_id", "user_id", name="uq_contest_leaderboard_user"),
    )
    op.create_index("ix_contest_leaderboard_contest_id", "contest_leaderboard", ["contest_id"])
    op.create_index("ix_contest_leaderboard_user_id", "contest_leaderboard", ["user_id"])

    op.add_column("winners", sa.Column("is_reserve", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("winners", sa.Column("status", sa.String(16), nullable=False, server_default="pending"))
    op.add_column("winners", sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("draw_logs", sa.Column("salt", sa.String(64), nullable=True))
    op.add_column("draw_logs", sa.Column("participants_hash", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("draw_logs", "participants_hash")
    op.drop_column("draw_logs", "salt")

    op.drop_column("winners", "notified_at")
    op.drop_column("winners", "status")
    op.drop_column("winners", "is_reserve")

    op.drop_index("ix_contest_leaderboard_user_id", table_name="contest_leaderboard")
    op.drop_index("ix_contest_leaderboard_contest_id", table_name="contest_leaderboard")
    op.drop_table("contest_leaderboard")

    op.drop_index("ix_contest_referrals_referred_id", table_name="contest_referrals")
    op.drop_index("ix_contest_referrals_referrer_id", table_name="contest_referrals")
    op.drop_index("ix_contest_referrals_contest_id", table_name="contest_referrals")
    op.drop_table("contest_referrals")

    op.drop_index("ix_ticket_logs_user_id", table_name="ticket_logs")
    op.drop_index("ix_ticket_logs_contest_id", table_name="ticket_logs")
    op.drop_table("ticket_logs")

    op.drop_column("entries", "conditions_met_at")
    op.drop_column("entries", "conditions_met")
    op.drop_column("entries", "tickets")

    op.drop_column("contests", "settings_json")
