import random

from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database import repo
from bot.filters import is_message_ok
from bot.markov_cache import MarkovWrapper
from bot.subtitles import get_pool_for_videos, invalidate_video_cache  # noqa: F401


async def build_markov_wrapper(db: AsyncSession, session_obj) -> MarkovWrapper:
    """Load messages from DB and construct a MarkovWrapper.

    Subtitle augmentation uses the per-chat video list stored in the DB.
    Lines are sampled with weights from the YouTube most-replayed heatmap.
    """
    msgs = await repo.get_latest_messages(db, session_obj, settings.keep_last)
    texts = [m.text for m in msgs if is_message_ok(session_obj, m.text)]

    chat_id: int = session_obj.chat.chat_id
    video_rows = await repo.get_chat_videos(db, chat_id)
    video_ids = [v.video_id for v in video_rows]

    subtitle_pct: int = getattr(session_obj.chat, "subtitle_percentage", 0)
    if video_ids:
        pool = await get_pool_for_videos(video_ids)
        if pool:
            texts_pool = [t for t, _ in pool]
            weights = [w for _, w in pool]
            if not texts:
                texts = random.choices(texts_pool, weights=weights, k=settings.keep_last)
            elif subtitle_pct > 0:
                n_swap = min(round(len(texts) * subtitle_pct / 100), len(texts))
                indices = random.sample(range(len(texts)), n_swap)
                replacements = random.choices(texts_pool, weights=weights, k=n_swap)
                for i, replacement in zip(indices, replacements):
                    texts[i] = replacement

    return MarkovWrapper(
        texts,
        keep_last=settings.keep_last,
        case_sensitive=session_obj.case_sensitive,
    )
