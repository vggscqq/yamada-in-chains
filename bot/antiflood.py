import time

anti_flood: dict[int, list[float]] = {}


def is_flood(chat_id: int, rate: int = 6, seconds: int = 10) -> bool:
    now = time.time()
    timestamps = anti_flood.get(chat_id, [])
    timestamps = [t for t in timestamps if now - t < seconds]
    timestamps.append(now)
    anti_flood[chat_id] = timestamps
    return len(timestamps) > rate
