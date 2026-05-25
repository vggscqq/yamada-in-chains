from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    admin: Mapped[bool] = mapped_column(Boolean, default=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    consented: Mapped[bool] = mapped_column(Boolean, default=True)

    messages: Mapped[list["Message"]] = relationship(back_populates="sender")


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    percentage: Mapped[int] = mapped_column(Integer, default=30)
    premium: Mapped[bool] = mapped_column(Boolean, default=False)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    block_links: Mapped[bool] = mapped_column(Boolean, default=False)
    block_usernames: Mapped[bool] = mapped_column(Boolean, default=False)
    keep_sfw: Mapped[bool] = mapped_column(Boolean, default=False)  # kept for DB compat, unused
    markov_disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    polls_disabled: Mapped[bool] = mapped_column(Boolean, default=True)
    subtitle_percentage: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    sessions: Mapped[list["Session"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )
    videos: Mapped[list["ChatVideo"]] = relationship(
        back_populates="chat", cascade="all, delete-orphan"
    )


class ChatVideo(Base):
    __tablename__ = "chat_videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chats.id", ondelete="CASCADE"), index=True
    )
    video_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    channel: Mapped[str | None] = mapped_column(String(256), nullable=True, default=None)
    chat: Mapped["Chat"] = relationship(back_populates="videos")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    uuid: Mapped[str] = mapped_column(
        String(36), unique=True, index=True, default=lambda: str(uuid4())
    )
    chat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chats.id", ondelete="CASCADE")
    )
    chat: Mapped[Chat] = relationship(back_populates="sessions")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    learning_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, default=True)
    always_reply: Mapped[bool] = mapped_column(Boolean, default=False)
    random_replies: Mapped[bool] = mapped_column(Boolean, default=False)

    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("sessions.id", ondelete="CASCADE")
    )
    session: Mapped[Session] = relationship(back_populates="messages")
    sender_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"))
    sender: Mapped[User] = relationship(back_populates="messages")
    text: Mapped[str] = mapped_column(Text)
