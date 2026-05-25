import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from bot import state as gs
from bot.database import repo
from bot.database.engine import db_session
from bot.database.models import Session
from bot.markov_cache import (
    MAX_FREE_SESSIONS,
    MAX_SESSIONS,
    CacheEntry,
    MarkovWrapper,
    chat_sessions,
    is_admin_in_group,
    markovs,
)
from bot.config import settings
from bot.filters import is_message_ok
from bot.handlers.commands import AddSession
from bot.markov_builder import build_markov_wrapper

router = Router()
logger = logging.getLogger(__name__)


# ── Keyboard builders ──────────────────────────────────────────────────────


def _e(val: bool) -> str:
    return "✅" if val else "❌"


def get_settings_keyboard(session: Session) -> InlineKeyboardMarkup:
    chat_id = session.chat.chat_id
    session_id = session.id

    def cb(action: str) -> str:
        return f"{action}_{chat_id}_{session_id}"

    c = session.chat
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Usernames {_e(not c.block_usernames)}", callback_data=cb("usernames")),
            InlineKeyboardButton(text=f"Links {_e(not c.block_links)}", callback_data=cb("links"))],
        [
            InlineKeyboardButton(text=f"Disable /markov {_e(c.markov_disabled)}", callback_data=cb("markov")),
            InlineKeyboardButton(text=f"Disable polls {_e(c.polls_disabled)}", callback_data=cb("polls")),
        ],
        [InlineKeyboardButton(text="── Session settings ──", callback_data="nothing")],
        [InlineKeyboardButton(text=f"Case sensitive {_e(session.case_sensitive)}", callback_data=cb("casesensivity"))],
        [InlineKeyboardButton(text=f"Always reply to replies {_e(session.always_reply)}", callback_data=cb("alwaysreply"))],
        [InlineKeyboardButton(text=f"Random replies {_e(session.random_replies)}", callback_data=cb("randomreplies"))],
        [InlineKeyboardButton(text=f"Pause learning {_e(session.learning_paused)}", callback_data=cb("pauselearning"))],
    ])


def build_sessions_keyboard(
    chat_id: int,
    sessions: list[Session],
    default_id: int,
    counts: dict[int, int],
) -> InlineKeyboardMarkup:
    rows = []
    for s in sessions:
        label = f"{'🎩 ' if s.id == default_id else ''}{s.name} — {counts.get(s.id, 0)}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"set_{chat_id}_{s.id}")])
    rows.append([InlineKeyboardButton(text="➕ Add session", callback_data=f"addsession_{chat_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ── Auth helper ────────────────────────────────────────────────────────────


async def _check_admin(callback: CallbackQuery, bot: Bot, chat_id: int) -> bool:
    user_id = callback.from_user.id
    if user_id in gs.admins:
        return True
    if callback.message and callback.message.chat.type.endswith("group"):
        if not await is_admin_in_group(bot, chat_id, user_id):
            await callback.answer("You are not allowed to do this.", show_alert=True)
            return False
    return True


# ── nothing ────────────────────────────────────────────────────────────────


@router.callback_query(F.data == "nothing")
async def cb_nothing(callback: CallbackQuery) -> None:
    await callback.answer("This button does nothing ☔️", show_alert=True)


# ── consent ────────────────────────────────────────────────────────────────


@router.callback_query(F.data.in_({"consent_give", "consent_revoke"}))
async def cb_consent(callback: CallbackQuery) -> None:
    consented = callback.data == "consent_give"
    async with db_session() as db:
        user = await repo.set_user_consented(db, callback.from_user.id, consented)
        await db.commit()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Revoke consent" if consented else "Give consent",
            callback_data="consent_revoke" if consented else "consent_give",
        )
    ]])
    await callback.answer("Settings updated!", show_alert=True)
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest:
        pass


# ── Settings toggles ───────────────────────────────────────────────────────


CHAT_TOGGLES = {"usernames": "block_usernames", "links": "block_links",
                "markov": "markov_disabled", "polls": "polls_disabled"}
SESSION_TOGGLES = {"casesensivity": "case_sensitive", "alwaysreply": "always_reply",
                   "randomreplies": "random_replies", "pauselearning": "learning_paused"}


@router.callback_query(F.data.regexp(r"^(usernames|links|markov|polls|casesensivity|alwaysreply|randomreplies|pauselearning)_"))
async def cb_toggle(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split("_", 1)
    action = parts[0]
    rest = parts[1]

    if action in SESSION_TOGGLES:
        # format: action_chatId_sessionId
        sub = rest.split("_", 1)
        chat_id = int(sub[0])
        session_id = int(sub[1])
    else:
        chat_id = int(rest.split("_")[0])
        session_id = None

    if not await _check_admin(callback, bot, chat_id):
        return

    async with db_session() as db:
        session_obj = await repo.get_default_session(db, chat_id)
        if session_id and session_obj.id != session_id:
            # find the specific session
            specific = await repo.get_session_by_id(db, session_id)
            if specific:
                session_obj = specific

        if action in CHAT_TOGGLES:
            field = CHAT_TOGGLES[action]
            setattr(session_obj.chat, field, not getattr(session_obj.chat, field))
        elif action in SESSION_TOGGLES:
            field = SESSION_TOGGLES[action]
            setattr(session_obj, field, not getattr(session_obj, field))

        await db.commit()
        # Reload with relationships
        session_obj = await repo.get_default_session(db, chat_id)
        await db.commit()

    # Invalidate session cache
    chat_sessions.pop(chat_id, None)

    keyboard = get_settings_keyboard(session_obj)
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer("Done!")


# ── Sessions panel ─────────────────────────────────────────────────────────


@router.callback_query(F.data.startswith("set_"))
async def cb_set_session(callback: CallbackQuery, bot: Bot) -> None:
    _, chat_id_str, session_id_str = callback.data.split("_", 2)
    chat_id = int(chat_id_str)
    session_id = int(session_id_str)

    if not await _check_admin(callback, bot, chat_id):
        return

    async with db_session() as db:
        default = await repo.get_default_session(db, chat_id)
        if default.id == session_id:
            await callback.answer("This is already the default session.", show_alert=True)
            await db.commit()
            return

        sessions = await repo.set_default_session(db, chat_id, session_id)
        new_default = next((s for s in sessions if s.is_default), None)
        if new_default is None:
            new_default = await repo.get_default_session(db, chat_id)

        # Rebuild Markov for new session
        markovs[chat_id] = CacheEntry(
            timestamp=time.time(),
            value=await build_markov_wrapper(db, new_default),
        )
        chat_sessions[chat_id] = CacheEntry(timestamp=time.time(), value=new_default)

        counts = {s.id: await repo.get_messages_count(db, s) for s in sessions}
        await db.commit()

    keyboard = build_sessions_keyboard(chat_id, sessions, session_id, counts)
    try:
        await callback.message.edit_text(
            "*Current sessions in this chat.* Use /delete to remove the current one.",
            reply_markup=keyboard,
        )
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await callback.answer("Done!")


@router.callback_query(F.data.startswith("addsession_"))
async def cb_add_session(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    chat_id = int(callback.data.split("_", 1)[1])

    if not await _check_admin(callback, bot, chat_id):
        return

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, chat_id)
        count = await repo.get_sessions_count(db, chat_id)
        limit = MAX_SESSIONS if chat.premium else MAX_FREE_SESSIONS
        if count >= limit:
            await callback.answer(f"Cannot add more than {limit} sessions.", show_alert=True)
            await db.commit()
            return
        await db.commit()

    await callback.answer()
    try:
        await callback.message.edit_text(
            "*Send me the name for the new session.* Send /cancel to cancel."
        )
    except TelegramBadRequest:
        pass

    await state.set_state(AddSession.waiting_for_name)
    await state.update_data(
        chat_id=chat_id,
        prompt_message_id=callback.message.message_id,
    )
