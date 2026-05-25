import logging
import os
import time

import psutil
from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import state as gs
from bot.antiflood import is_flood
from bot.database import repo
from bot.database.engine import db_session
from bot.filters import is_message_ok
from bot.markov_cache import (
    CacheEntry,
    MarkovWrapper,
    chat_sessions,
    is_admin_in_group,
    markovs,
)
from bot.transforms import apply_transforms
from bot.config import settings
from bot.markov_builder import build_markov_wrapper, invalidate_video_cache
from bot.subtitles import extract_video_id, fetch_video_info

router = Router()
logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.dirname(__file__))
HELP_TEXT = open(os.path.join(_HERE, "texts", "help.md")).read()
PRIVACY_TEXT = open(os.path.join(_HERE, "texts", "privacy.md")).read()
CONSENT_TEXT = (
    "Markov Bot is an opt-in service. Use the buttons below to manage your data consent.\n"
    "See /privacy for details."
)


class AddSession(StatesGroup):
    waiting_for_name = State()


# ── Helpers ────────────────────────────────────────────────────────────────


def _thread_id(message: Message) -> int | None:
    if message.is_topic_message and message.message_thread_id:
        return message.message_thread_id
    return None


async def _is_group_admin(bot: Bot, message: Message) -> bool:
    if message.sender_chat and message.sender_chat.id == message.chat.id:
        return True
    if message.from_user:
        return await is_admin_in_group(bot, message.chat.id, message.from_user.id)
    return False


def _is_bot_admin(user_id: int) -> bool:
    return user_id in gs.admins


async def _ensure_markov(db, session_obj, chat_id: int) -> MarkovWrapper:
    if chat_id not in markovs:
        wrapper = await build_markov_wrapper(db, session_obj)
        markovs[chat_id] = CacheEntry(timestamp=time.time(), value=wrapper)
    else:
        markovs[chat_id].timestamp = time.time()
    return markovs[chat_id].value  # type: ignore[return-value]


# ── /start ─────────────────────────────────────────────────────────────────


@router.message(Command("start"))
async def cmd_start(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return

    is_pm = message.chat.type == "private"

    if is_pm and command.args == "consent":
        async with db_session() as db:
            user = await repo.get_or_insert_user(db, message.from_user.id)
            await db.commit()
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Revoke consent" if user.consented else "Give consent",
                callback_data="consent_revoke" if user.consented else "consent_give",
            )
        ]])
        await message.answer(CONSENT_TEXT, reply_markup=keyboard)
        return

    me = await bot.get_me()
    text = (
        "Hello! I learn from your messages and generate my own sentences using a Markov chain.\n"
        "Send /enable to try me out here, or /help for more information."
    )
    if is_pm:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Add me to a group 🤖",
                url=f"https://t.me/{me.username}?startgroup=enable",
            )
        ]])
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer(text)


# ── /help ──────────────────────────────────────────────────────────────────


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT)


# ── /privacy ───────────────────────────────────────────────────────────────


@router.message(Command("privacy"))
async def cmd_privacy(message: Message) -> None:
    await message.answer(PRIVACY_TEXT)


# ── /manageconsent ─────────────────────────────────────────────────────────


@router.message(Command("manageconsent"))
async def cmd_manageconsent(message: Message) -> None:
    if not message.from_user:
        return
    async with db_session() as db:
        user = await repo.get_or_insert_user(db, message.from_user.id)
        await db.commit()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Revoke consent" if user.consented else "Give consent",
            callback_data="consent_revoke" if user.consented else "consent_give",
        )
    ]])
    await message.answer(CONSENT_TEXT, reply_markup=keyboard)


# ── /enable / /disable ─────────────────────────────────────────────────────


@router.message(Command("enable"))
async def cmd_enable(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    async with db_session() as db:
        chat = await repo.set_enabled(db, message.chat.id, True, is_private=is_pm)
        await db.commit()
    await message.answer("✅ Learning enabled in this chat.")


@router.message(Command("disable"))
async def cmd_disable(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    async with db_session() as db:
        await repo.set_enabled(db, message.chat.id, False, is_private=is_pm)
        await db.commit()
    await message.answer("❌ Learning disabled in this chat.")


# ── /percentage ────────────────────────────────────────────────────────────


@router.message(Command("percentage"))
async def cmd_percentage(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, message.chat.id, is_private=is_pm)
        if not command.args:
            await db.commit()
            await message.answer(
                f"Current reply percentage: **{chat.percentage}%**",
            )
            return
        try:
            value = int(command.args.strip())
            if not 0 <= value <= 100:
                raise ValueError
        except ValueError:
            await db.commit()
            await message.answer("Please provide a value between 0 and 100.")
            return
        chat.percentage = value
        await db.commit()
    await message.answer(f"Reply percentage set to **{value}%**.")


# ── /subtitlepercentage ────────────────────────────────────────────────────


@router.message(Command("subtitlepercentage"))
async def cmd_subtitlepercentage(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, message.chat.id, is_private=is_pm)
        if not command.args:
            await db.commit()
            await message.answer(
                f"Current subtitle augmentation: **{chat.subtitle_percentage}%** of training messages "
                f"are randomly replaced with subtitle lines.",
            )
            return
        try:
            value = int(command.args.strip())
            if not 0 <= value <= 100:
                raise ValueError
        except ValueError:
            await db.commit()
            await message.answer("Please provide a value between 0 and 100.")
            return
        chat.subtitle_percentage = value
        await db.commit()

    # Invalidate the cached wrapper so it rebuilds with the new percentage.
    markovs.pop(message.chat.id, None)
    await message.answer(f"Subtitle augmentation set to **{value}%**.")


# ── /videos / /addvideo / /removevideo ────────────────────────────────────


@router.message(Command("videos"))
async def cmd_videos(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    async with db_session() as db:
        videos = await repo.get_chat_videos(db, message.chat.id)
        await db.commit()
    if not videos:
        await message.answer("No YouTube videos configured for this chat.")
        return
    lines = []
    for v in videos:
        label = v.title or v.video_id
        if v.channel:
            label = f"{label} — {v.channel}"
        lines.append(f"• [{label}](https://youtu.be/{v.video_id})")
    await message.answer(
        "**Configured videos:**\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


@router.message(Command("addvideo"))
async def cmd_addvideo(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    if not command.args:
        await message.answer("Usage: /addvideo <youtube_url_or_id>")
        return
    video_id = extract_video_id(command.args.strip())
    if video_id is None:
        await message.answer("Could not recognise a YouTube video ID in that input.")
        return
    title, channel = await fetch_video_info(video_id)
    async with db_session() as db:
        entry = await repo.add_chat_video(db, message.chat.id, video_id, title=title, channel=channel)
        await db.commit()
    if entry is None:
        await message.answer(f"Video `{video_id}` is already in the list.")
        return
    markovs.pop(message.chat.id, None)
    label = title or video_id
    if channel:
        label = f"{label} — {channel}"
    await message.answer(
        f"Added [{label}](https://youtu.be/{video_id}).",
        parse_mode="Markdown",
    )


@router.message(Command("removevideo"))
async def cmd_removevideo(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    if not command.args:
        await message.answer("Usage: /removevideo <youtube_video_id>")
        return
    video_id = command.args.strip()
    async with db_session() as db:
        removed = await repo.remove_chat_video(db, message.chat.id, video_id)
        await db.commit()
    if not removed:
        await message.answer(f"Video `{video_id}` was not in the list.")
        return
    invalidate_video_cache(video_id)
    markovs.pop(message.chat.id, None)
    await message.answer(f"✅ Removed `{video_id}`.")


# ── /markov ────────────────────────────────────────────────────────────────


@router.message(Command("markov"))
async def cmd_markov(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, chat_id)
        if not chat.enabled:
            await db.commit()
            await message.answer("Learning is not enabled. Use /enable first.")
            return
        user = await repo.get_or_insert_user(db, user_id)
        if not user.consented:
            await db.commit()
            await message.answer(
                "❕ You haven't given consent. Use /manageconsent.",
            )
            return
        if chat.markov_disabled and not _is_bot_admin(user_id) and not await _is_group_admin(bot, message):
            await db.commit()
            return
        session_obj = await repo.get_default_session(db, chat_id)
        wrapper = await _ensure_markov(db, session_obj, chat_id)
        await db.commit()

    if wrapper.sample_count == 0:
        await message.answer("Not enough data to generate a sentence.")
        return

    begin = command.args.strip() if command.args else None
    if begin and not session_obj.case_sensitive:
        begin = begin.lower()

    generated = wrapper.generate(begin=begin)
    if generated is None:
        await message.answer("Not enough data to generate a sentence.")
        return

    generated = apply_transforms(generated, session_obj)
    reply_to = message.reply_to_message.message_id if message.reply_to_message else None
    await message.answer(generated, reply_to_message_id=reply_to)


# ── /wouldyourather ────────────────────────────────────────────────────────


@router.message(Command("wouldyourather"))
async def cmd_wouldyourather(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    chat_id = message.chat.id
    user_id = message.from_user.id

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, chat_id)
        if not chat.enabled:
            await db.commit()
            await message.answer("Learning is not enabled. Use /enable first.")
            return
        user = await repo.get_or_insert_user(db, user_id)
        if not user.consented:
            await db.commit()
            await message.answer("❕ Use /manageconsent to give consent.")
            return
        if chat.polls_disabled and not _is_bot_admin(user_id) and not await _is_group_admin(bot, message):
            await db.commit()
            return
        session_obj = await repo.get_default_session(db, chat_id)
        wrapper = await _ensure_markov(db, session_obj, chat_id)
        await db.commit()

    if wrapper.sample_count < 10:
        await message.answer("Not enough data to generate a poll.")
        return

    options: list[str] = []
    for _ in range(10):
        g = wrapper.generate()
        if g:
            g = apply_transforms(g, session_obj)
            # Telegram poll options max 100 chars
            if len(g) > 100:
                g = g[:97] + "..."
            options.append(g)

    options = list(dict.fromkeys(options))  # deduplicate preserving order
    options.sort(key=len, reverse=True)

    if len(options) < 2:
        await message.answer("Not enough data to generate a poll.")
        return

    is_anon = command.args and "anon" in command.args.lower()
    await bot.send_poll(
        chat_id=chat_id,
        question="🌺 Would you rather...",
        options=options[:2],
        is_anonymous=bool(is_anon),
        message_thread_id=_thread_id(message),
    )


# ── /settings ──────────────────────────────────────────────────────────────


@router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    async with db_session() as db:
        session_obj = await repo.get_default_session(db, message.chat.id)
        await db.commit()

    from bot.handlers.callbacks import get_settings_keyboard
    await message.answer(
        "Tap a button to toggle a setting. Use /percentage to change the reply ratio, "
        "/subtitlepercentage to set YouTube subtitle augmentation.",
        reply_markup=get_settings_keyboard(session_obj),
    )


# ── /sessions ──────────────────────────────────────────────────────────────


@router.message(Command("sessions"))
async def cmd_sessions(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return

    from bot.handlers.callbacks import build_sessions_keyboard
    async with db_session() as db:
        sessions = await repo.get_sessions(db, message.chat.id)
        counts = {s.id: await repo.get_messages_count(db, s) for s in sessions}
        default = await repo.get_default_session(db, message.chat.id)
        await db.commit()

    keyboard = build_sessions_keyboard(message.chat.id, sessions, default.id, counts)
    sent = await message.answer(
        "*Current sessions in this chat.* Use /delete to remove the current one.",
        reply_markup=keyboard,
    )


# ── /delete ────────────────────────────────────────────────────────────────


@router.message(Command("delete"))
async def cmd_delete(message: Message, command: CommandObject, bot: Bot) -> None:
    if not message.from_user:
        return
    is_pm = message.chat.type == "private"
    if not is_pm and not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return

    if not command.args or command.args.strip().lower() != "confirm":
        await message.answer(
            "⚠️ This will delete the current session and all its messages.\n"
            "Run `/delete confirm` to proceed.",
        )
        return

    async with db_session() as db:
        session_obj = await repo.get_default_session(db, message.chat.id)
        count = await repo.delete_session_messages(db, session_obj)
        await db.commit()

    markovs.pop(message.chat.id, None)
    chat_sessions.pop(message.chat.id, None)
    await message.answer(
        f"✅ Deleted session and `{count}` messages.",
    )


# ── /deletefrom ────────────────────────────────────────────────────────────


@router.message(Command("deletefrom"))
async def cmd_deletefrom(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    if message.chat.type == "private":
        await message.answer("This command only works in groups.")
        return
    if not await _is_group_admin(bot, message):
        await message.answer("Only group admins can use this command.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.answer("Reply to a user's message to delete their data.")
        return

    target_id = message.reply_to_message.from_user.id
    async with db_session() as db:
        session_obj = await repo.get_default_session(db, message.chat.id)
        count = await repo.delete_from_user_in_chat(db, session_obj, target_id)
        await db.commit()

    markovs.pop(message.chat.id, None)
    await message.answer(
        f"✅ Deleted `{count}` messages from that user in the current session.",
    )


# ── /deleteme ──────────────────────────────────────────────────────────────


@router.message(Command("deleteme"))
async def cmd_deleteme(message: Message, command: CommandObject) -> None:
    if not message.from_user:
        return
    if message.chat.type != "private":
        await message.answer("This command only works in private chat.")
        return
    if not command.args or command.args.strip().lower() != "confirm":
        await message.answer(
            "⚠️ This will permanently delete ALL your messages from my database.\n"
            "Run `/deleteme confirm` to proceed."
        )
        return

    async with db_session() as db:
        count = await repo.delete_all_messages_from_user(db, message.from_user.id)
        await db.commit()

    await message.answer(f"✅ Deleted `{count}` messages from my database.")


# ── /admin, /unadmin, /remadmin ────────────────────────────────────────────


@router.message(Command("admin"))
async def cmd_admin(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /admin <user_id>")
        return
    try:
        target = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid user ID.")
        return
    async with db_session() as db:
        await repo.set_admin(db, target, True)
        await db.commit()
    gs.admins.add(target)
    await message.answer(f"✅ User `{target}` is now a bot admin.")


@router.message(Command(commands=["unadmin", "remadmin"]))
async def cmd_unadmin(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /unadmin <user_id>")
        return
    try:
        target = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid user ID.")
        return
    async with db_session() as db:
        await repo.set_admin(db, target, False)
        await db.commit()
    gs.admins.discard(target)
    await message.answer(f"✅ Removed bot admin from `{target}`.")


# ── /botadmins ─────────────────────────────────────────────────────────────


@router.message(Command("botadmins"))
async def cmd_botadmins(message: Message) -> None:
    if not message.from_user or not _is_bot_admin(message.from_user.id):
        return
    async with db_session() as db:
        admins = await repo.get_bot_admins(db)
        await db.commit()
    if not admins:
        await message.answer("No bot admins configured.")
    else:
        lines = "\n".join(f"• `{a.user_id}`" for a in admins)
        await message.answer(f"**Bot admins:**\n{lines}")


# ── /banpeer / /unbanpeer ──────────────────────────────────────────────────


@router.message(Command("banpeer"))
async def cmd_banpeer(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /banpeer <id>  (positive = user, negative = chat)")
        return
    try:
        peer_id = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid ID.")
        return
    async with db_session() as db:
        if peer_id < 0:
            await repo.set_banned_chat(db, peer_id, True)
            gs.banned_chats.add(peer_id)
        else:
            await repo.set_banned_user(db, peer_id, True)
            gs.banned_users.add(peer_id)
        await db.commit()
    await message.answer(f"✅ Banned `{peer_id}`.")


@router.message(Command("unbanpeer"))
async def cmd_unbanpeer(message: Message, command: CommandObject) -> None:
    if not message.from_user or not _is_bot_admin(message.from_user.id):
        return
    if not command.args:
        await message.answer("Usage: /unbanpeer <id>")
        return
    try:
        peer_id = int(command.args.strip())
    except ValueError:
        await message.answer("Invalid ID.")
        return
    async with db_session() as db:
        if peer_id < 0:
            await repo.set_banned_chat(db, peer_id, False)
            gs.banned_chats.discard(peer_id)
        else:
            await repo.set_banned_user(db, peer_id, False)
            gs.banned_users.discard(peer_id)
        await db.commit()
    await message.answer(f"✅ Unbanned `{peer_id}`.")


# ── /stats / /count ────────────────────────────────────────────────────────


@router.message(Command(commands=["stats", "count"]))
async def cmd_stats(message: Message) -> None:
    if not message.from_user or not _is_bot_admin(message.from_user.id):
        return
    async with db_session() as db:
        counts = await repo.get_total_counts(db)
        await db.commit()

    uptime_secs = int(time.time() - gs.start_time)
    h, rem = divmod(uptime_secs, 3600)
    m, s = divmod(rem, 60)
    rss = psutil.Process().memory_info().rss
    rss_mb = rss / 1024 / 1024

    text = (
        f"**📊 Stats**\n"
        f"Users: `{counts['users']}`\n"
        f"Chats: `{counts['chats']}`\n"
        f"Sessions: `{counts['sessions']}`\n"
        f"Messages: `{counts['messages']}`\n"
        f"Uptime: `{h}h {m}m {s}s`\n"
        f"Memory: `{rss_mb:.1f} MB`"
    )
    await message.answer(text)


# ── FSM: receive session name ───────────────────────────────────────────────


@router.message(AddSession.waiting_for_name)
async def receive_session_name(message: Message, state: FSMContext, bot: Bot) -> None:
    from bot.markov_cache import MAX_FREE_SESSIONS, MAX_SESSION_NAME_LENGTH, MAX_SESSIONS
    from bot.handlers.callbacks import build_sessions_keyboard

    data = await state.get_data()
    await state.clear()

    chat_id: int = data["chat_id"]
    prompt_msg_id: int = data["prompt_message_id"]

    text = (message.text or "").strip()

    async def edit_prompt(new_text: str) -> None:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=prompt_msg_id,
                text=new_text,
            )
        except Exception:
            pass

    if not text or text.lower().startswith("/cancel"):
        await edit_prompt("*Operation cancelled.*")
        return

    if len(text) > MAX_SESSION_NAME_LENGTH:
        await edit_prompt(
            f"*Cancelled.* Session name must be ≤ {MAX_SESSION_NAME_LENGTH} characters."
        )
        return

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, chat_id)
        count = await repo.get_sessions_count(db, chat_id)
        limit = MAX_SESSIONS if chat.premium else MAX_FREE_SESSIONS
        if count >= limit:
            await edit_prompt(f"Cannot add more than {limit} sessions per chat.")
            await db.commit()
            return
        await repo.add_session(db, name=text, chat=chat)
        sessions = await repo.get_sessions(db, chat_id)
        counts_map = {s.uuid: await repo.get_messages_count(db, s) for s in sessions}
        default = await repo.get_default_session(db, chat_id)
        await db.commit()

    keyboard = build_sessions_keyboard(chat_id, sessions, default.uuid, counts_map)
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=prompt_msg_id,
            text="*Current sessions in this chat.* Use /delete to remove the current one.",
            reply_markup=keyboard,
        )
    except Exception:
        pass
