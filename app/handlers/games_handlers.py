"""Обработчики игр Fable:
- "рыбалка"
- "охота"
- "слоты 100"
- "рулетка красное 100", "рулетка 17 100"
- "дуэль @user 100" + инлайн-кнопки (принять, отклонить, прицелиться, выстрелить).
"""
from __future__ import annotations

import re
from aiogram import Router, types, F
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db.session import session_scope
from app.services.player_service import get_or_create_player
from app.services.games_service import (
    play_fishing,
    play_hunting,
    play_slots,
    play_roulette,
    claim_daily_bonus,
    play_dice,
    play_coinflip,
    play_mystery_box,
)
from app.services.duel_service import (
    create_duel_invite,
    accept_duel,
    decline_duel,
    duel_aim,
    duel_shoot,
)

router = Router(name="games_router")


def _extract_target_id(message: types.Message) -> int | None:
    if message.reply_to_message and message.reply_to_message.from_user:
        return message.reply_to_message.from_user.id
    if message.entities:
        for ent in message.entities:
            if ent.type == "text_mention" and ent.user:
                return ent.user.id
    return None


@router.callback_query(F.data == "bait:claim")
async def handle_bait_claim(callback: types.CallbackQuery):
    """Игрок нажимает на байт-кнопку в чате."""
    if not callback.from_user:
        return
    user_id = callback.from_user.id
    async with session_scope() as session:
        await get_or_create_player(
            session,
            telegram_id=user_id,
            first_name=callback.from_user.first_name,
            last_name=callback.from_user.last_name,
            username=callback.from_user.username,
        )
        bonus_coins = 25
        await credit_coins(
            session,
            user_id=user_id,
            amount=bonus_coins,
            reason="Захват чат-дропа (байт)",
            operation_type="reward",
        )
    await callback.answer(f"🎉 Вы перехватили дроп и получили +{bonus_coins} монет!", show_alert=True)
    if callback.message:
        try:
            await callback.message.edit_text(
                f"⚡️ Игрок <b>{callback.from_user.first_name}</b> быстрее всех среагировал и забрал дроп на <b>+{bonus_coins}</b> монет!",
                parse_mode="HTML",
                reply_markup=None,
            )
        except Exception:
            pass


@router.message(F.text.casefold().in_(["бонус", "!бонус", "ежедневный бонус"]))
async def handle_daily_bonus(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            res = await claim_daily_bonus(session, user_id=message.from_user.id)
            await message.reply(res.message, parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("кости"))
async def handle_dice(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Использование: <code>кости 100</code>", parse_mode="HTML")
        return
    stake = int(parts[1])
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            p_roll, b_roll, is_win, payout = await play_dice(session, user_id=message.from_user.id, stake=stake)
            if p_roll == b_roll:
                await message.reply(f"🎲 <b>КОСТИ</b>\n\nВы выбросили: <b>{p_roll}</b>\nБот выбросил: <b>{b_roll}</b>\n\n🤝 Ничья! Ставка {stake} монет возвращена.", parse_mode="HTML")
            elif is_win:
                await message.reply(f"🎲 <b>КОСТИ</b>\n\nВы выбросили: <b>{p_roll}</b>\nБот выбросил: <b>{b_roll}</b>\n\n🎉 <b>Победа!</b> Выигрыш: <b>+{payout}</b> монет!", parse_mode="HTML")
            else:
                await message.reply(f"🎲 <b>КОСТИ</b>\n\nВы выбросили: <b>{p_roll}</b>\nБот выбросил: <b>{b_roll}</b>\n\n💀 <b>Проигрыш!</b> Ставка {stake} монет сгорела.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("монетка"))
async def handle_coinflip(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit():
        await message.reply("Использование: <code>монетка орел 100</code> или <code>монетка решка 100</code>", parse_mode="HTML")
        return
    choice = parts[1].casefold()
    stake = int(parts[2])
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            outcome, is_win, payout = await play_coinflip(session, user_id=message.from_user.id, choice=choice, stake=stake)
            icon = "🦅" if outcome == "орел" else "🪙"
            if is_win:
                await message.reply(f"🪙 <b>МОНЕТКА</b> 🪙\n\nВыпало: {icon} <b>{outcome.upper()}</b>!\n\n🎉 <b>Победа!</b> Выигрыш: <b>+{payout}</b> монет!", parse_mode="HTML")
            else:
                await message.reply(f"🪙 <b>МОНЕТКА</b> 🪙\n\nВыпало: {icon} <b>{outcome.upper()}</b>!\n\n💀 <b>Проигрыш!</b> Ставка {stake} монет сгорела.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("сундук"))
async def handle_box(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Использование: <code>сундук 100</code>", parse_mode="HTML")
        return
    stake = int(parts[1])
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            mult, payout = await play_mystery_box(session, user_id=message.from_user.id, stake=stake)
            if mult >= 1.0:
                await message.reply(f"📦 <b>ТАИНСТВЕННЫЙ СУНДУК</b> 📦\n\nВам выпал множитель: <b>x{mult:.1f}</b>!\n🎉 Выигрыш: <b>+{payout}</b> монет!", parse_mode="HTML")
            else:
                await message.reply(f"📦 <b>ТАИНСТВЕННЫЙ СУНДУК</b> 📦\n\nВам выпал множитель: <b>x{mult:.1f}</b>\n💀 В сундуке оказалось меньше, чем ожидалось: получено {payout} монет.", parse_mode="HTML")
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_(["рыбалка", "!рыбалка"]))
async def handle_fishing(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            res = await play_fishing(session, user_id=message.from_user.id)
            await message.reply(res.message)
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().in_(["охота", "!охота"]))
async def handle_hunting(message: types.Message):
    if not message.from_user:
        return
    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            res = await play_hunting(session, user_id=message.from_user.id)
            await message.reply(res.message)
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("слоты"))
async def handle_slots(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Использование: <code>слоты 100</code>", parse_mode="HTML")
        return
    stake = int(parts[1])

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            res = await play_slots(session, user_id=message.from_user.id, stake=stake)
            reels_str = " | ".join(res.reels)
            if res.is_win:
                await message.reply(
                    f"🎰 <b>СЛОТЫ</b> 🎰\n\n"
                    f"[{reels_str}]\n\n"
                    f"🎉 <b>ПОБЕДА!</b> Коэффициент: x{res.multiplier:.1f}\n"
                    f"Выигрыш: <b>+{res.payout}</b> монет!",
                    parse_mode="HTML",
                )
            else:
                await message.reply(
                    f"🎰 <b>СЛОТЫ</b> 🎰\n\n"
                    f"[{reels_str}]\n\n"
                    f"💀 <b>Проигрыш!</b> Ставка {stake} монет сгорела.",
                    parse_mode="HTML",
                )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.message(F.text.casefold().startswith("рулетка"))
async def handle_roulette(message: types.Message):
    if not message.from_user:
        return
    parts = (message.text or "").split()
    if len(parts) < 3 or not parts[2].isdigit():
        await message.reply("Использование: <code>рулетка красное 100</code> или <code>рулетка 17 100</code>", parse_mode="HTML")
        return
    target = parts[1]
    stake = int(parts[2])

    async with session_scope() as session:
        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            res = await play_roulette(session, user_id=message.from_user.id, bet_target=target, stake=stake)
            color_emoji = "🔴" if res.winning_color == "red" else ("⚫" if res.winning_color == "black" else "🟢")
            if res.is_win:
                await message.reply(
                    f"🎡 <b>РУЛЕТКА</b> 🎡\n\n"
                    f"Выпало: {color_emoji} <b>{res.winning_number} ({res.winning_color})</b>\n\n"
                    f"🎉 <b>ВЫИГРЫШ!</b> Коэффициент: x{res.multiplier}\n"
                    f"Выплата: <b>+{res.payout}</b> монет!",
                    parse_mode="HTML",
                )
            else:
                await message.reply(
                    f"🎡 <b>РУЛЕТКА</b> 🎡\n\n"
                    f"Выпало: {color_emoji} <b>{res.winning_number} ({res.winning_color})</b>\n\n"
                    f"💀 <b>Проигрыш!</b> Ставка {stake} монет сгорела.",
                    parse_mode="HTML",
                )
        except Exception as e:
            await message.reply(f"❌ {e}")


# ===================== ДУЭЛИ =====================

@router.message(F.text.casefold().startswith("дуэль"))
async def handle_duel_invite(message: types.Message):
    if not message.from_user or not message.chat:
        return
    if message.chat.type in (types.ChatType.PRIVATE,):
        await message.reply("Дуэли проводятся только в группах!")
        return

    parts = (message.text or "").split()
    stake = None
    for p in reversed(parts):
        if p.isdigit():
            stake = int(p)
            break

    async with session_scope() as session:
        from app.handlers.social_moderation_handlers import _get_target_user_id
        target_id = await _get_target_user_id(message, session)

        if not target_id or not stake:
            await message.reply("Использование: <code>дуэль @user 100</code> или ответом: <code>дуэль 100</code>", parse_mode="HTML")
            return

        await get_or_create_player(session, telegram_id=message.from_user.id, first_name=message.from_user.first_name)
        try:
            duel = await create_duel_invite(
                session,
                chat_id=message.chat.id,
                challenger_id=message.from_user.id,
                opponent_id=target_id,
                stake=stake,
            )
            builder = InlineKeyboardBuilder()
            builder.button(text="⚔️ Принять дуэль", callback_data=f"duel_acc_{duel.id}")
            builder.button(text="❌ Отклонить", callback_data=f"duel_dec_{duel.id}")
            builder.adjust(2)

            from app.db.models import Player
            opp_player = await session.get(Player, target_id)
            opp_name = opp_player.first_name if opp_player else f"ID {target_id}"

            await message.reply(
                f"⚔️ <b>ВЫЗОВ НА ДУЭЛЬ!</b>\n\n"
                f"🤠 <b>{message.from_user.first_name}</b> бросает перчатку игроку <b>{opp_name}</b>!\n"
                f"💰 Ставка: <b>{stake:,}</b> монет с каждого дуэлянта.\n"
                f"🏆 Общий призовой фонд: <b>{stake * 2:,}</b> монет!".replace(",", " "),
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception as e:
            await message.reply(f"❌ {e}")


@router.callback_query(F.data.startswith("duel_acc_"))
async def on_duel_accept(callback: types.CallbackQuery):
    duel_id = int(callback.data.split("_")[2])
    async with session_scope() as session:
        try:
            resp = await accept_duel(session, session_id=duel_id, actor_id=callback.from_user.id)
            builder = InlineKeyboardBuilder()
            builder.button(text="🎯 Прицелиться (+10%)", callback_data=f"duel_aim_{duel_id}")
            builder.button(text="💥 Выстрелить", callback_data=f"duel_shoot_{duel_id}")
            builder.adjust(2)

            from app.db.models import Player
            curr_p = await session.get(Player, resp.current_turn_user_id)
            turn_name = curr_p.first_name if curr_p else f"ID {resp.current_turn_user_id}"

            await callback.message.edit_text(
                f"⚔️ <b>ДУЭЛЬ НАЧАЛАСЬ!</b>\n\n"
                f"{resp.message}\n\n"
                f"👉 Первым ходит: <b>{turn_name}</b>!",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception as e:
            await callback.answer(f"❌ {e}", show_alert=True)


@router.callback_query(F.data.startswith("duel_dec_"))
async def on_duel_decline(callback: types.CallbackQuery):
    duel_id = int(callback.data.split("_")[2])
    async with session_scope() as session:
        try:
            resp = await decline_duel(session, session_id=duel_id, actor_id=callback.from_user.id)
            await callback.message.edit_text("❌ Вызов на дуэль отклонен.")
        except Exception as e:
            await callback.answer(f"❌ {e}", show_alert=True)


@router.callback_query(F.data.startswith("duel_aim_"))
async def on_duel_aim(callback: types.CallbackQuery):
    duel_id = int(callback.data.split("_")[2])
    async with session_scope() as session:
        try:
            resp = await duel_aim(session, session_id=duel_id, actor_id=callback.from_user.id)
            builder = InlineKeyboardBuilder()
            builder.button(text="🎯 Прицелиться (+10%)", callback_data=f"duel_aim_{duel_id}")
            builder.button(text="💥 Выстрелить", callback_data=f"duel_shoot_{duel_id}")
            builder.adjust(2)

            from app.db.models import Player
            curr_p = await session.get(Player, resp.current_turn_user_id)
            turn_name = curr_p.first_name if curr_p else f"ID {resp.current_turn_user_id}"

            await callback.message.edit_text(
                f"⚔️ <b>ДУЭЛЬ</b>\n\n"
                f"{resp.message}\n\n"
                f"👉 Сейчас ход дуэлянта: <b>{turn_name}</b>!",
                reply_markup=builder.as_markup(),
                parse_mode="HTML",
            )
        except Exception as e:
            await callback.answer(f"❌ {e}", show_alert=True)


@router.callback_query(F.data.startswith("duel_shoot_"))
async def on_duel_shoot(callback: types.CallbackQuery):
    duel_id = int(callback.data.split("_")[2])
    async with session_scope() as session:
        try:
            resp = await duel_shoot(session, session_id=duel_id, actor_id=callback.from_user.id)
            from app.db.models import Player
            if resp.is_finished:
                winner_p = await session.get(Player, resp.winner_id)
                win_name = winner_p.first_name if winner_p else f"ID {resp.winner_id}"
                await callback.message.edit_text(
                    f"🏆 <b>ДУЭЛЬ ЗАВЕРШЕНА!</b>\n\n"
                    f"{resp.message}\n\n"
                    f"👑 <b>Победитель: {win_name}!</b>\n"
                    f"💰 Он забирает весь призовой банк себе!",
                    parse_mode="HTML",
                )
            else:
                builder = InlineKeyboardBuilder()
                builder.button(text="🎯 Прицелиться (+10%)", callback_data=f"duel_aim_{duel_id}")
                builder.button(text="💥 Выстрелить", callback_data=f"duel_shoot_{duel_id}")
                builder.adjust(2)

                curr_p = await session.get(Player, resp.current_turn_user_id)
                turn_name = curr_p.first_name if curr_p else f"ID {resp.current_turn_user_id}"

                await callback.message.edit_text(
                    f"⚔️ <b>ДУЭЛЬ</b>\n\n"
                    f"{resp.message}\n\n"
                    f"👉 Сейчас ход дуэлянта: <b>{turn_name}</b>!",
                    reply_markup=builder.as_markup(),
                    parse_mode="HTML",
                )
        except Exception as e:
            await callback.answer(f"❌ {e}", show_alert=True)
