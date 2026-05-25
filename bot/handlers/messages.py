import logging
import random
import time

from aiogram import Bot, Router
from aiogram.types import Message

from bot import state as gs
from bot.antiflood import is_flood
from bot.config import settings
from bot.database import repo
from bot.database.engine import db_session
from bot.filters import is_message_ok
from bot.markov_cache import CacheEntry, MarkovWrapper, chat_sessions, markovs
from bot.transforms import apply_transforms

router = Router()
logger = logging.getLogger(__name__)


def _thread_id(message: Message) -> int | None:
    if message.is_topic_message and message.message_thread_id:
        return message.message_thread_id
    return None


@router.message()
async def on_message(message: Message, bot: Bot) -> None:
    if not message.from_user:
        return
    if message.from_user.is_bot:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    text = message.text or message.caption or ""

    if not text:
        return
    if text.startswith("/"):
        return

    if user_id in gs.banned_users or chat_id in gs.banned_chats:
        return

    async with db_session() as db:
        chat = await repo.get_or_insert_chat(db, chat_id)
        if not chat.enabled:
            await db.commit()
            return

        user = await repo.get_or_insert_user(db, user_id)
        session_obj = await repo.get_default_session(db, chat_id)

        if not is_message_ok(session_obj, text):
            await db.commit()
            return

        # Lazy-init or TTL refresh
        if chat_id not in markovs:
            msgs = await repo.get_latest_messages(db, session_obj, settings.keep_last)
            texts = [m.text for m in msgs if is_message_ok(session_obj, m.text)]
            wrapper = MarkovWrapper(texts, keep_last=settings.keep_last, case_sensitive=session_obj.case_sensitive)
            markovs[chat_id] = CacheEntry(timestamp=time.time(), value=wrapper)
        else:
            markovs[chat_id].timestamp = time.time()

        wrapper: MarkovWrapper = markovs[chat_id].value  # type: ignore[assignment]

        # Feed sample if consented and learning active
        if user.consented and not session_obj.learning_paused:
            sample = text if session_obj.case_sensitive else text.lower()
            wrapper.add_sample(sample)
            await repo.add_message(db, text, user, session_obj)

        await db.commit()

    # Consent notice in PM
    if message.chat.type == "private" and not user.consented:
        await message.answer(
            "❕ You haven't given consent to my learning. Use /manageconsent to opt in."
        )
        return

    # Auto-reply decision
    percentage = chat.percentage
    replied_to_bot = (
        message.reply_to_message is not None
        and message.reply_to_message.from_user is not None
        and message.reply_to_message.from_user.id == bot.id
    )

    if not (replied_to_bot and session_obj.always_reply):
        if replied_to_bot:
            percentage = min(percentage * 2, 100)
        if random.randint(1, 100) > percentage:
            return
        if is_flood(chat_id, rate=10, seconds=30):
            return

    generated = wrapper.generate()
    if generated is None:
        return

    generated = apply_transforms(generated, session_obj)

    try:
        if replied_to_bot or (
            session_obj.random_replies and random.randint(1, 100) <= percentage // 2
        ):
            await message.reply(generated)
        else:
            await bot.send_message(
                chat_id, generated, message_thread_id=_thread_id(message)
            )
    except Exception as e:
        from aiogram.exceptions import TelegramForbiddenError
        if isinstance(e, TelegramForbiddenError):
            try:
                await bot.leave_chat(chat_id)
            except Exception:
                pass
        else:
            logger.error(f"Error sending message: {e}")
