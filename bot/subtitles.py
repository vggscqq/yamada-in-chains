"""Subtitle fetching, heatmap weighting, and per-video pool caching.

This module is the single place that talks to youtube-transcript-api and yt-dlp.
markov_builder imports only the public helpers from here.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings

logger = logging.getLogger(__name__)

# ── Video ID parsing ───────────────────────────────────────────────────────

_YT_PATTERNS = [
    r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})",
]


def extract_video_id(text: str) -> str | None:
    """Return the 11-char YouTube video ID from a URL or bare ID, or None."""
    text = text.strip()
    for pattern in _YT_PATTERNS:
        m = re.search(pattern, text)
        if m:
            return m.group(1)
    # Bare ID
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    return None


# ── Disk cache ─────────────────────────────────────────────────────────────


def _cache_path() -> Path:
    return Path(settings.db_path).parent / "subtitles_cache.json"


def _load_cache_sync() -> dict:
    p = _cache_path()
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return {}


def _save_cache_sync(data: dict) -> None:
    p = _cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f)


# ── Fetchers (blocking, run in executor) ──────────────────────────────────


def _fetch_transcript_sync(video_id: str) -> list[dict]:
    """Returns [{"text": str, "start": float}, ...]."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    transcript = next(iter(api.list(video_id)))
    fetched = transcript.fetch()
    return [
        {"text": snippet.text.strip(), "start": snippet.start}
        for snippet in fetched
        if snippet.text.strip()
    ]


def _fetch_video_info_sync(video_id: str) -> tuple[str | None, str | None, list[dict]]:
    """Returns (title_or_None, channel_or_None, heatmap_segments)."""
    try:
        import yt_dlp

        ydl_opts = {"skip_download": True, "quiet": True, "no_warnings": True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={video_id}", download=False
            )
            title: str | None = info.get("title")
            channel: str | None = info.get("channel") or info.get("uploader")
            heatmap: list[dict] = info.get("heatmap") or []
            logger.info(
                "Fetched title=%r channel=%r and %d heatmap segments for %s",
                title, channel, len(heatmap), video_id,
            )
            return title, channel, heatmap
    except Exception:
        logger.warning("Failed to fetch video info for %s", video_id, exc_info=True)
        return None, None, []


# ── Heatmap weighting ──────────────────────────────────────────────────────


def _weight_for_start(start: float, heatmap: list[dict]) -> float:
    for seg in heatmap:
        if seg["start_time"] <= start < seg["end_time"]:
            return max(float(seg["value"]), 0.01)
    return 1.0


# ── In-memory pool cache ───────────────────────────────────────────────────

_video_pool_cache: dict[str, list[tuple[str, float]]] = {}


def invalidate_video_cache(video_id: str) -> None:
    _video_pool_cache.pop(video_id, None)


async def get_pool_for_video(
    video_id: str, db: AsyncSession | None = None
) -> list[tuple[str, float]]:
    """Return (text, weight) pairs for a video, fetching and caching as needed."""
    if video_id in _video_pool_cache:
        return _video_pool_cache[video_id]

    # Try DB first when a session is available
    if db is not None:
        from bot.database import repo
        db_lines = await repo.get_subtitle_lines(db, video_id)
        if db_lines:
            _video_pool_cache[video_id] = db_lines
            return db_lines

    loop = asyncio.get_event_loop()
    cache: dict = await loop.run_in_executor(None, _load_cache_sync)

    if video_id not in cache:
        try:
            lines = await loop.run_in_executor(None, _fetch_transcript_sync, video_id)
            title, channel, heatmap = await loop.run_in_executor(
                None, _fetch_video_info_sync, video_id
            )
            cache[video_id] = {"lines": lines, "heatmap": heatmap, "title": title, "channel": channel}
            logger.info(
                "Cached %d lines for %s (%r)", len(lines), video_id, title
            )
            await loop.run_in_executor(None, _save_cache_sync, cache)
        except Exception:
            logger.warning("Failed to fetch subtitles for %s", video_id, exc_info=True)
            cache[video_id] = {"lines": [], "heatmap": [], "title": None, "channel": None}

    entry = cache.get(video_id, {})
    lines: list[dict] = entry.get("lines", [])
    heatmap: list[dict] = entry.get("heatmap", [])
    pool = [
        (line["text"], _weight_for_start(line["start"], heatmap) if heatmap else 1.0)
        for line in lines
    ]
    _video_pool_cache[video_id] = pool

    if db is not None and pool:
        from bot.database import repo
        await repo.save_subtitle_lines(db, video_id, pool)

    return pool


async def get_pool_for_videos(
    video_ids: list[str], db: AsyncSession | None = None
) -> list[tuple[str, float]]:
    """Combine per-video pools with equal weight contribution per video.

    Each video's weights are rescaled so their sum equals 1.0 before merging,
    ensuring a long video with thousands of lines doesn't drown out a short one.
    """
    pool: list[tuple[str, float]] = []
    for vid in video_ids:
        per_video = await get_pool_for_video(vid, db=db)
        if not per_video:
            continue
        total = sum(w for _, w in per_video)
        pool.extend((text, w / total) for text, w in per_video)
    return pool


async def fetch_video_info(
    video_id: str, db: AsyncSession | None = None
) -> tuple[str | None, str | None]:
    """Return (title, channel) from disk cache, fetching everything if needed."""
    loop = asyncio.get_event_loop()
    cache: dict = await loop.run_in_executor(None, _load_cache_sync)
    if video_id in cache:
        return cache[video_id].get("title"), cache[video_id].get("channel")
    # Not in cache yet — fetch everything now so it's ready for sampling later.
    await get_pool_for_video(video_id, db=db)
    cache = await loop.run_in_executor(None, _load_cache_sync)
    entry = cache.get(video_id, {})
    return entry.get("title"), entry.get("channel")
