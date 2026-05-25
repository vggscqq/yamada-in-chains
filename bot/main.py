import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.config import settings
from bot.database import repo
from bot.database.engine import db_session, init_db
from bot.handlers import callbacks, commands, messages
from bot.markov_cache import cleaner_worker
from bot import state as gs

logger = logging.getLogger(__name__)


async def on_startup(bot: Bot) -> None:
    async with db_session() as db:
        for u in await repo.get_bot_admins(db):
            gs.admins.add(u.user_id)
        for u in await repo.get_banned_users(db):
            gs.banned_users.add(u.user_id)
        await db.commit()

    if settings.admin_id:
        gs.admins.add(settings.admin_id)
        async with db_session() as db:
            await repo.set_admin(db, settings.admin_id, True)
            await db.commit()

    asyncio.create_task(cleaner_worker())
    logger.info("Bot started. Admins: %s", gs.admins)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO if settings.logging_enabled else logging.WARNING,
        format="%(levelname)s | %(asctime)s | %(name)s: %(message)s",
    )

    await init_db()

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.startup.register(on_startup)

    dp.include_router(commands.router)
    dp.include_router(callbacks.router)
    dp.include_router(messages.router)  # must be last — catches all messages

    while True:
        try:
            await dp.start_polling(
                bot,
                allowed_updates=["message", "callback_query"],
            )
        except Exception as e:
            logger.error("Fatal error: %s. Restarting in 5s…", e)
            await asyncio.sleep(5)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
