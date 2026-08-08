"""Add-on stdout reaches Home Assistant support bundles verbatim — scrub it."""

from __future__ import annotations

import logging

import pytest

from ha_blink_camera.logging_setup import (
    RedactingFormatter,
    SecretRedactor,
    setup_logging,
)


def _record(message: str, *args: object) -> logging.LogRecord:
    """Build a log record the way a real logger call would."""
    return logging.LogRecord(
        name="blinkpy.auth",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=args or None,
        exc_info=None,
    )


def _format(redactor: SecretRedactor, record: logging.LogRecord) -> str:
    """Render a record exactly as the installed handler would."""
    return RedactingFormatter(redactor).format(record)


def test_known_secret_in_the_message_is_redacted() -> None:
    """The simple case: a secret interpolated into the format string."""
    redactor = SecretRedactor()
    redactor.add("refresh-token-value")

    output = _format(redactor, _record("Login response: refresh-token-value"))

    assert "refresh-token-value" not in output
    assert "***" in output


def test_known_secret_hiding_in_an_argument_is_redacted() -> None:
    """blinkpy logs whole response dicts as %s — the secret lives in the args."""
    redactor = SecretRedactor()
    redactor.add("refresh-token-value")
    record = _record("Login response: %s", {"t": "refresh-token-value"})

    assert "refresh-token-value" not in _format(redactor, record)


def test_the_account_email_never_appears() -> None:
    """Rule: the account address is not logged at any level."""
    redactor = SecretRedactor()
    redactor.add("me@example.com")

    assert "me@example.com" not in redactor.redact("Logging in as me@example.com")


def test_short_and_empty_values_are_ignored() -> None:
    """Redacting a 2-character "secret" would mangle unrelated lines."""
    redactor = SecretRedactor()
    redactor.add("ab")
    redactor.add("")
    redactor.add(None)

    assert redactor.redact("abstract") == "abstract"


def test_a_never_before_seen_access_token_is_redacted() -> None:
    """The reachable leak: extract_login_info() KeyErrors, auth.py:169 fires."""
    body = {"access_token": "FRESH-TOKEN-ISSUED-SECONDS-AGO", "expires_in": 3600}
    record = _record("Malformed login response: %s", body)

    output = _format(SecretRedactor(), record)

    assert "FRESH-TOKEN-ISSUED-SECONDS-AGO" not in output
    assert "***" in output


@pytest.mark.parametrize(
    "text",
    [
        "{'refresh_token': 'SECRETVALUE1234'}",
        '{"access_token": "SECRETVALUE1234"}',
        "token=SECRETVALUE1234",
        "authorization: SECRETVALUE1234",
        "Authorization: Bearer SECRETVALUE1234",
        "{'password': 'SECRETVALUE1234'}",
        "code_verifier=SECRETVALUE1234",
    ],
    ids=[
        "python-repr",
        "json",
        "query-string",
        "header-ish",
        "bearer",
        "password",
        "pkce",
    ],
)
def test_credential_shaped_values_are_redacted(text: str) -> None:
    """Whatever quoting or separator the dumping code happened to use."""
    assert "SECRETVALUE1234" not in SecretRedactor().redact(text)


def test_ordinary_text_survives_the_pattern_pass() -> None:
    """The pattern pass must not mangle lines that carry no credential."""
    redactor = SecretRedactor()
    line = "Relay listening on tcp://0.0.0.0:8554, token count: 3, password: None"

    assert redactor.redact(line) == line


def test_a_secret_in_a_traceback_is_redacted() -> None:
    """A `logging.Filter` cannot see this: the traceback is rendered later.

    cli.py's top-level `_LOGGER.exception` is exactly this path, and aiohttp
    error messages routinely carry the request URL.
    """
    redactor = SecretRedactor()
    redactor.add("refresh-token-value")
    try:
        raise RuntimeError("failed for refresh-token-value")
    except RuntimeError:
        import sys

        record = _record("Unexpected failure")
        record.exc_info = sys.exc_info()

    output = _format(redactor, record)

    assert "Traceback" in output, "the traceback must still be there"
    assert "refresh-token-value" not in output


def test_setup_installs_exactly_one_handler() -> None:
    """Repeated setup must not duplicate every line in the Supervisor viewer."""
    setup_logging()
    setup_logging()

    assert len(logging.getLogger().handlers) == 1


def test_setup_wires_the_returned_redactor_into_the_formatter() -> None:
    """The caller registers secrets on it as they are learned."""
    redactor = setup_logging()
    formatter = logging.getLogger().handlers[0].formatter
    redactor.add("refresh-token-value")

    assert isinstance(formatter, RedactingFormatter)
    assert "refresh-token-value" not in formatter.format(
        _record("token is refresh-token-value")
    )


def test_setup_caps_blinkpy_below_debug() -> None:
    """blinkpy logs OAuth and token detail at DEBUG; that must stay unreachable."""
    setup_logging(level=logging.DEBUG)

    assert logging.getLogger("blinkpy").level == logging.INFO


def test_setup_silences_the_body_dumping_modules() -> None:
    """auth.py and api.py dump whole HTTP bodies at WARNING and ERROR.

    The INFO ceiling does nothing about those, so these two are capped outright.
    """
    setup_logging()

    assert logging.getLogger("blinkpy.auth").level == logging.CRITICAL
    assert logging.getLogger("blinkpy.api").level == logging.CRITICAL
