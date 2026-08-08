"""What the add-on does when it cannot work: idle loudly, never exit, never retry."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from ha_blink_camera import cli
from ha_blink_camera.blink_client import BlinkClient
from ha_blink_camera.config import Config
from ha_blink_camera.exceptions import InvalidCredentialsError, TransientBlinkError
from ha_blink_camera.logging_setup import SecretRedactor


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the idle heartbeat so a test can observe two of them."""
    monkeypatch.setattr(cli, "HEARTBEAT_S", 0.02)
    monkeypatch.setattr(cli, "BACKOFF_INITIAL_S", 0.01)
    monkeypatch.setattr(cli, "BACKOFF_MAX_S", 0.05)


async def _stop_after(seconds: float, stop: asyncio.Event) -> None:
    """Let the code under test run for a moment, then ask it to stop."""
    await asyncio.sleep(seconds)
    stop.set()


async def test_idle_never_returns_until_stopped(fast_heartbeat: None) -> None:
    """Exiting would be an S6 respawn loop against Blink's login endpoint."""
    stop = asyncio.Event()
    asyncio.create_task(_stop_after(0.1, stop))

    await asyncio.wait_for(cli._idle("bad credentials", stop), timeout=2.0)

    assert stop.is_set()


async def test_idle_repeats_why_it_is_idle(
    fast_heartbeat: None, caplog: pytest.LogCaptureFixture
) -> None:
    """A silent add-on that says "running" is worse than one that keeps complaining."""
    stop = asyncio.Event()
    asyncio.create_task(_stop_after(0.1, stop))

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(cli._idle("no camera named 'Salon'", stop), timeout=2.0)

    heartbeats = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert heartbeats, "the idle state must keep announcing itself"
    assert "Salon" in heartbeats[0].getMessage()


async def test_idle_states_say_what_to_fix(
    fast_heartbeat: None, caplog: pytest.LogCaptureFixture
) -> None:
    """One actionable line, logged at ERROR, is the whole user interface here."""
    stop = asyncio.Event()
    stop.set()

    with caplog.at_level(logging.ERROR):
        await cli._idle("No Blink account configured yet.", stop)

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert "No Blink account configured yet." in errors


async def test_a_missing_options_file_idles_rather_than_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh install nobody has configured idles; it never reaches the relay."""
    monkeypatch.setenv("ADDON_DATA_DIR", str(tmp_path))
    idled: list[str] = []

    async def record_idle(reason: str, stop: asyncio.Event) -> None:
        idled.append(reason)

    async def must_not_run(*args: object) -> None:
        raise AssertionError("the relay must not start without configuration")

    monkeypatch.setattr(cli, "_idle", record_idle)
    monkeypatch.setattr(cli, "_relay", must_not_run)

    await cli._main(SecretRedactor())

    assert idled and "options file" in idled[0]


class _Client:
    """A BlinkClient stand-in that fails a set number of times, then connects."""

    def __init__(self, failures: int, error: Exception | None = None) -> None:
        """Fail `failures` times with a transient error, then succeed."""
        self.attempts = 0
        self._failures = failures
        self._error = error or TransientBlinkError("Cannot reach Blink right now.")

    async def connect(self) -> None:
        """Count the attempt and fail until the quota is used up."""
        self.attempts += 1
        if self.attempts <= self._failures:
            raise self._error


async def test_a_transient_login_failure_is_retried(fast_heartbeat: None) -> None:
    """Network loss at boot must not be mistaken for bad credentials."""
    client = _Client(failures=2)
    stop = asyncio.Event()

    connected = await cli._connect(client, stop)  # type: ignore[arg-type]

    assert connected is True
    assert client.attempts == 3


async def test_bad_credentials_are_not_retried(fast_heartbeat: None) -> None:
    """Retrying a rejected password is how an account gets locked out."""
    client = _Client(failures=99, error=InvalidCredentialsError("nope"))
    stop = asyncio.Event()

    with pytest.raises(InvalidCredentialsError):
        await cli._connect(client, stop)  # type: ignore[arg-type]

    assert client.attempts == 1


async def test_retrying_stops_when_the_add_on_is_stopping(fast_heartbeat: None) -> None:
    """SIGTERM during a backoff must not wait out the whole delay."""
    client = _Client(failures=99)
    stop = asyncio.Event()
    asyncio.create_task(_stop_after(0.05, stop))

    connected = await asyncio.wait_for(
        cli._connect(client, stop),  # type: ignore[arg-type]
        timeout=2.0,
    )

    assert connected is False


def test_a_fatal_configuration_never_exits_non_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under an S6 longrun, a non-zero exit is an immediate respawn."""
    (tmp_path / "options.json").write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("ADDON_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "HEARTBEAT_S", 0.01)

    async def immediate_idle(reason: str, stop: asyncio.Event) -> None:
        """Stand in for the real idle so the test does not run forever."""
        assert "configured" in reason

    monkeypatch.setattr(cli, "_idle", immediate_idle)

    assert cli.main([]) == 0


def test_closing_the_client_twice_is_safe(tmp_path: Path) -> None:
    """`_relay`'s finally runs even when connect never happened."""
    config = Config(
        username="me@example.com",
        password="hunter2",
        camera_name=None,
        data_dir=tmp_path,
        config_dir=tmp_path / "config",
    )
    client = BlinkClient(config, SecretRedactor())

    async def exercise() -> None:
        await client.aclose()
        await client.aclose()

    asyncio.run(exercise())
