"""Small in-memory diagnostic snapshot for the owner-only /diag command."""
import logging
import re
import time
from collections import deque

_STARTED = time.time()
_EVENTS = deque(maxlen=20)
_INSTALLED = False
_SECRET_PATTERNS = [
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._-]+"), r"\1[REDACTED]"),
    (re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"), "[BOT_TOKEN_REDACTED]"),
]


def record_error(message: str):
    value = str(message)[:500]
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    _EVENTS.append(value)


def recent_errors():
    return list(_EVENTS)


def uptime_seconds():
    return max(0, int(time.time() - _STARTED))


class _Capture(logging.Handler):
    def emit(self, record):
        message = record.getMessage()
        lower = message.lower()
        if record.levelno >= logging.ERROR or any(
            term in lower for term in ("conflict", "429", "wrong session id", "floodwait", "persistenttimestampoutdated")
        ):
            record_error(f"{record.levelname}: {message}")


def install_error_capture():
    global _INSTALLED
    if _INSTALLED:
        return
    logging.getLogger().addHandler(_Capture())
    _INSTALLED = True
