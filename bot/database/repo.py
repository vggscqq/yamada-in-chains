from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bot.database.models import Chat, Message, Session, User


# ── Users ──────────────────────────────────────────────────────────────────


async def get_or_insert_user(db: AsyncSession, user_id: int) -> User:
    result = await db.execute(select(User).where(User.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(user_id=user_id)
        db.add(user)
        await db.flush()
        await db.refresh(user)
    return user


async def get_bot_admins(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).where(User.admin == True))  # noqa: E712
    return list(result.scalars().all())


async def get_banned_users(db: AsyncSession) -> list[User]:
    result = await db.execute(select(User).where(User.banned == True))  # noqa: E712
    return list(result.scalars().all())


async def set_admin(db: AsyncSession, user_id: int, admin: bool) -> User:
    user = await get_or_insert_user(db, user_id)
    user.admin = admin
    await db.flush()
    return user


async def set_banned_user(db: AsyncSession, user_id: int, banned: bool) -> User:
    user = await get_or_insert_user(db, user_id)
    user.banned = banned
    await db.flush()
    return user


async def set_user_consented(db: AsyncSession, user_id: int, consented: bool) -> User:
    user = await get_or_insert_user(db, user_id)
    user.consented = consented
    await db.flush()
    return user


# ── Chats ──────────────────────────────────────────────────────────────────


async def get_or_insert_chat(db: AsyncSession, chat_id: int, is_private: bool = False) -> Chat:
    result = await db.execute(
        select(Chat).where(Chat.chat_id == chat_id).options(selectinload(Chat.sessions))
    )
    chat = result.scalar_one_or_none()
    if chat is None:
        chat = Chat(
            chat_id=chat_id,
            percentage=100 if is_private else 30,
        )
        db.add(chat)
        await db.flush()
        await db.refresh(chat, ["sessions"])
    return chat


async def set_enabled(db: AsyncSession, chat_id: int, enabled: bool, is_private: bool = False) -> Chat:
    chat = await get_or_insert_chat(db, chat_id, is_private=is_private)
    chat.enabled = enabled
    if enabled and chat.percentage == 0:
        chat.percentage = 100 if is_private else 30
    await db.flush()
    return chat


async def set_banned_chat(db: AsyncSession, chat_id: int, banned: bool) -> Chat:
    chat = await get_or_insert_chat(db, chat_id)
    chat.banned = banned
    await db.flush()
    return chat


# ── Sessions ───────────────────────────────────────────────────────────────


async def get_sessions(db: AsyncSession, chat_id: int) -> list[Session]:
    chat = await get_or_insert_chat(db, chat_id)
    result = await db.execute(
        select(Session)
        .where(Session.chat_id == chat.id)
        .options(selectinload(Session.chat))
    )
    return list(result.scalars().all())


async def get_sessions_count(db: AsyncSession, chat_id: int) -> int:
    chat = await get_or_insert_chat(db, chat_id)
    result = await db.execute(
        select(func.count()).select_from(Session).where(Session.chat_id == chat.id)
    )
    return result.scalar_one()


async def get_default_session(db: AsyncSession, chat_id: int) -> Session:
    chat = await get_or_insert_chat(db, chat_id)

    result = await db.execute(
        select(Session)
        .where(Session.chat_id == chat.id, Session.is_default == True)  # noqa: E712
        .options(selectinload(Session.chat))
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if session:
        return session

    # No default — try to promote the first existing session
    result = await db.execute(
        select(Session)
        .where(Session.chat_id == chat.id)
        .options(selectinload(Session.chat))
        .limit(1)
    )
    session = result.scalar_one_or_none()
    if session:
        session.is_default = True
        await db.flush()
        return session

    # No sessions at all — create the first one
    session = Session(
        name="default",
        uuid=str(uuid4()),
        chat_id=chat.id,
        is_default=True,
    )
    db.add(session)
    await db.flush()
    await db.refresh(session, ["chat"])
    return session


async def get_session_by_id(db: AsyncSession, session_id: int) -> Session | None:
    result = await db.execute(
        select(Session)
        .where(Session.id == session_id)
        .options(selectinload(Session.chat))
    )
    return result.scalar_one_or_none()


async def set_default_session(db: AsyncSession, chat_id: int, session_id: int) -> list[Session]:
    sessions = await get_sessions(db, chat_id)
    for s in sessions:
        s.is_default = s.id == session_id
    await db.flush()
    return sessions


async def add_session(db: AsyncSession, name: str, chat: Chat) -> Session:
    session = Session(name=name, uuid=str(uuid4()), chat_id=chat.id, is_default=False)
    db.add(session)
    await db.flush()
    await db.refresh(session, ["chat"])
    return session


# ── Messages ───────────────────────────────────────────────────────────────


async def get_latest_messages(
    db: AsyncSession, session_obj: Session, limit: int = 1500
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_obj.id)
        .order_by(Message.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_messages_count(db: AsyncSession, session_obj: Session) -> int:
    result = await db.execute(
        select(func.count()).select_from(Message).where(Message.session_id == session_obj.id)
    )
    return result.scalar_one()


async def get_user_messages_count(
    db: AsyncSession, session_obj: Session, user_id: int
) -> int:
    user_result = await db.execute(select(User).where(User.user_id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        return 0
    result = await db.execute(
        select(func.count())
        .select_from(Message)
        .where(Message.session_id == session_obj.id, Message.sender_id == user.id)
    )
    return result.scalar_one()


async def add_message(
    db: AsyncSession, text: str, sender: User, session_obj: Session
) -> None:
    msg = Message(text=text, sender_id=sender.id, session_id=session_obj.id)
    db.add(msg)
    await db.flush()


async def delete_session_messages(db: AsyncSession, session_obj: Session) -> int:
    count = await get_messages_count(db, session_obj)
    await db.execute(delete(Message).where(Message.session_id == session_obj.id))
    await db.execute(delete(Session).where(Session.id == session_obj.id))
    await db.flush()
    return count


async def delete_from_user_in_chat(
    db: AsyncSession, session_obj: Session, user_id: int
) -> int:
    count = await get_user_messages_count(db, session_obj, user_id)
    user_result = await db.execute(select(User).where(User.user_id == user_id))
    user = user_result.scalar_one_or_none()
    if user:
        await db.execute(
            delete(Message).where(
                Message.session_id == session_obj.id, Message.sender_id == user.id
            )
        )
        await db.flush()
    return count


async def delete_all_messages_from_user(db: AsyncSession, user_id: int) -> int:
    user_result = await db.execute(select(User).where(User.user_id == user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        return 0
    result = await db.execute(
        select(func.count()).select_from(Message).where(Message.sender_id == user.id)
    )
    count = result.scalar_one()
    await db.execute(delete(Message).where(Message.sender_id == user.id))
    await db.flush()
    return count


# ── Stats ──────────────────────────────────────────────────────────────────


async def get_total_counts(db: AsyncSession) -> dict:
    users = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    chats = (await db.execute(select(func.count()).select_from(Chat))).scalar_one()
    sessions = (await db.execute(select(func.count()).select_from(Session))).scalar_one()
    messages = (await db.execute(select(func.count()).select_from(Message))).scalar_one()
    return {"users": users, "chats": chats, "sessions": sessions, "messages": messages}
