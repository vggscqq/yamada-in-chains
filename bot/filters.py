import re

URL_RE = re.compile(
    r"(?i)\b((?:https?://|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+))",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(
    r"@([a-zA-Z](_(?!_)|[a-zA-Z0-9]){3,32}[a-zA-Z0-9])", re.IGNORECASE
)


def is_message_ok(session, text: str) -> bool:
    if not text.strip():
        return False
    if session.chat.block_links and URL_RE.search(text):
        return False
    if session.chat.block_usernames and USERNAME_RE.search(text):
        return False
    return True
