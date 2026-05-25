"""
Migration: pre-commit schema → current schema.

  1. chats.subtitle_percentage  INTEGER DEFAULT 0
  2. chat_videos                table (did not exist pre-commit); or add
                                chat_videos.title / chat_videos.channel if the
                                table was created manually without them
  3. subtitle_lines             table
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("data/markov.db")


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def migrate(db_path: Path) -> None:
    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    con = sqlite3.connect(db_path)
    applied: list[str] = []

    # 1. chats.subtitle_percentage
    if "subtitle_percentage" not in columns(con, "chats"):
        con.execute(
            "ALTER TABLE chats ADD COLUMN subtitle_percentage INTEGER NOT NULL DEFAULT 0"
        )
        applied.append("chats.subtitle_percentage")

    # 2. chat_videos table (did not exist pre-commit)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "chat_videos" not in tables:
        con.execute(
            """CREATE TABLE chat_videos (
                id      INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                video_id VARCHAR(32) NOT NULL,
                title   VARCHAR(256),
                channel VARCHAR(256)
            )"""
        )
        con.execute(
            "CREATE INDEX ix_chat_videos_chat_id ON chat_videos(chat_id)"
        )
        applied.append("chat_videos (table created)")
    else:
        cv_cols = columns(con, "chat_videos")
        if "title" not in cv_cols:
            con.execute("ALTER TABLE chat_videos ADD COLUMN title VARCHAR(256)")
            applied.append("chat_videos.title")
        if "channel" not in cv_cols:
            con.execute("ALTER TABLE chat_videos ADD COLUMN channel VARCHAR(256)")
            applied.append("chat_videos.channel")

    # 3. subtitle_lines table
    if "subtitle_lines" not in tables:
        con.execute(
            """CREATE TABLE subtitle_lines (
                id       INTEGER PRIMARY KEY,
                video_id VARCHAR(32) NOT NULL,
                text     TEXT NOT NULL,
                weight   REAL NOT NULL DEFAULT 1.0
            )"""
        )
        con.execute(
            "CREATE INDEX ix_subtitle_lines_video_id ON subtitle_lines(video_id)"
        )
        applied.append("subtitle_lines (table created)")

    con.commit()
    con.close()

    if applied:
        print("Applied migrations:")
        for col in applied:
            print(f"  + {col}")
    else:
        print("Nothing to do — schema is already up to date.")


if __name__ == "__main__":
    migrate(DB_PATH)
