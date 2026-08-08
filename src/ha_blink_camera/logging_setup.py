"""Configure stdlib logging for a container, and keep secrets out of what it writes."""

from __future__ import annotations

import logging
import re
import sys
from typing import Final

_LOG_FORMAT: Final = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT: Final = "%Y-%m-%d %H:%M:%S"

_REDACTED: Final = "***"
_MIN_SECRET_LENGTH: Final = 8

_THIRD_PARTY_CEILING: Final = logging.INFO
_THIRD_PARTY_LOGGERS: Final = ("blinkpy", "aiohttp", "asyncio")

_SILENCED_LOGGERS: Final = ("blinkpy.auth", "blinkpy.api")
_SILENCED_CEILING: Final = logging.CRITICAL

_SECRET_KEY: Final = (
    r"access_token|refresh_token|id_token|auth_token|token|authorization"
    r"|code_verifier|csrf_token|password|api_key"
)
_KEYED_SECRET: Final = re.compile(
    rf"(?i)(['\"]?\b(?:{_SECRET_KEY})\b['\"]?\s*[:=]\s*)"
    rf"(?:(['\"])([^'\"]{{{_MIN_SECRET_LENGTH},}})\2"
    rf"|([^\s,;)}}\]]{{{_MIN_SECRET_LENGTH},}}))"
)
_BEARER: Final = re.compile(rf"(?i)\b(bearer\s+)(\S{{{_MIN_SECRET_LENGTH},}})")


def _mask_keyed(match: re.Match[str]) -> str:
    """Replace the value of a credential-looking key, keeping its quoting."""
    prefix, quote = match.group(1), match.group(2)
    return f"{prefix}{quote}{_REDACTED}{quote}" if quote else f"{prefix}{_REDACTED}"


class SecretRedactor:
    """Scrubs known secret values, and values sitting under credential-ish keys.

    The pattern pass is the only defence against a secret we have never seen —
    a token arriving in the very response third-party code is about to log.
    """

    def __init__(self) -> None:
        """Start with nothing known to redact."""
        self._secrets: set[str] = set()

    def add(self, secret: str | None) -> None:
        """Register a value to scrub; too-short values would match ordinary words."""
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            self._secrets.add(secret)

    def redact(self, text: str) -> str:
        """Return `text` with known secrets and keyed secret values removed."""
        for secret in self._secrets:
            text = text.replace(secret, _REDACTED)
        text = _KEYED_SECRET.sub(_mask_keyed, text)
        return _BEARER.sub(rf"\1{_REDACTED}", text)


class RedactingFormatter(logging.Formatter):
    """Formats a record, then scrubs the result.

    Scrubbing here rather than in a `logging.Filter` is what covers tracebacks:
    a filter only sees `record.msg` and `record.args`.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        """Format with the add-on's layout, scrubbing through `redactor`."""
        super().__init__(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        """Render the record, message and traceback alike, then redact it."""
        return self._redactor.redact(super().format(record))


def setup_logging(level: int = logging.INFO) -> SecretRedactor:
    """Install a single stdout handler and return the redactor feeding it."""
    redactor = SecretRedactor()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(redactor))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(max(level, _THIRD_PARTY_CEILING))
    for name in _SILENCED_LOGGERS:
        logging.getLogger(name).setLevel(_SILENCED_CEILING)

    return redactor
