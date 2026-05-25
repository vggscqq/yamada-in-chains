import asyncio
import logging
import time
from dataclasses import dataclass

import markovify

logger = logging.getLogger(__name__)

MARKOV_TTL = 60 * 30        # 30 minutes
ADMINS_CACHE_TTL = 60 * 5   # 5 minutes
SESSION_TTL = 60 * 30       # 30 minutes

MAX_SESSIONS = 20
MAX_FREE_SESSIONS = 5
MAX_SESSION_NAME_LENGTH = 16

# Maximum words taken from any single message when building the Markov model.
# Prevents one long text from dominating the chain over many short messages.
_MAX_WORDS_PER_MSG = 40


@dataclass
class CacheEntry:
    timestamp: float
    value: object


markovs: dict[int, CacheEntry] = {}
admins_cache: dict[tuple, CacheEntry] = {}
chat_sessions: dict[int, CacheEntry] = {}


class MarkovWrapper:
    def __init__(self, texts: list[str], keep_last: int = 1500, case_sensitive: bool = True):
        self.keep_last = keep_last
        self.case_sensitive = case_sensitive
        self.texts: list[str] = list(texts)[-keep_last:]
        self._model: markovify.Text | None = self._build(self.texts)

    def _build(self, texts: list[str]) -> markovify.Text | None:
        if not texts:
            return None
        # Build one model per message (equal weighting) and cap each message's
        # word count so no single long text can dominate the chain.
        models: list[markovify.Text] = []
        for t in texts:
            t = t.strip()
            if not t:
                continue
            words = t.split()
            if len(words) > _MAX_WORDS_PER_MSG:
                t = " ".join(words[:_MAX_WORDS_PER_MSG])
            try:
                models.append(markovify.Text(t, well_formed=False))
            except Exception:
                pass
        if not models:
            return None
        try:
            return markovify.combine(models)
        except Exception:
            return None

    def add_sample(self, text: str) -> None:
        self.texts.append(text)
        if len(self.texts) > self.keep_last:
            self.texts = self.texts[-self.keep_last:]
        self._model = self._build(self.texts)

    def generate(self, begin: str | None = None) -> str | None:
        if self._model is None:
            return None
        for _ in range(10):
            try:
                if begin:
                    result = self._model.make_sentence_with_start(
                        begin, strict=False, tries=20, max_words=50
                    )
                else:
                    result = self._model.make_sentence(tries=20, max_words=50)
                if result:
                    return result
            except Exception:
                pass
        return None

    @property
    def sample_count(self) -> int:
        return len(self.texts)


async def is_admin_in_group(bot, chat_id: int, user_id: int) -> bool:
    now = time.time()
    key = (chat_id, user_id)
    if key in admins_cache and now - admins_cache[key].timestamp < ADMINS_CACHE_TTL:
        return admins_cache[key].value  # type: ignore[return-value]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        result = member.status in ("creator", "administrator")
    except Exception:
        result = False
    admins_cache[key] = CacheEntry(timestamp=now, value=result)
    return result


async def cleaner_worker() -> None:
    while True:
        try:
            now = time.time()
            for cache, ttl in [(markovs, MARKOV_TTL), (chat_sessions, SESSION_TTL)]:
                expired = [k for k, v in cache.items() if now - v.timestamp > ttl]
                for k in expired:
                    del cache[k]
            expired_admin = [
                k for k, v in admins_cache.items() if now - v.timestamp > ADMINS_CACHE_TTL
            ]
            for k in expired_admin:
                del admins_cache[k]
        except Exception as e:
            logger.error(f"Cleaner error: {e}")
        await asyncio.sleep(30)
