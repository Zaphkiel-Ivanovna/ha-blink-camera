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
from ha_blink_camera.setup_ui import SetupState, Stage


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


async def test_a_fresh_install_reaches_the_setup_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No options is a first-run state, not an error: the Web UI collects them.

    This replaces the old contract, where an unconfigured install idled with a
    message telling the user to edit options they should never have to touch.
    """
    monkeypatch.setenv("ADDON_DATA_DIR", str(tmp_path))
    started: list[str] = []

    async def record_relay(config: Config, *_: object) -> None:
        started.append(config.username)

    async def must_not_idle(reason: str, stop: asyncio.Event) -> None:
        raise AssertionError(f"should serve the setup page, not idle: {reason}")

    monkeypatch.setattr(cli, "_relay", record_relay)
    monkeypatch.setattr(cli, "_idle", must_not_idle)

    await cli._main(SecretRedactor())

    assert started == [""], "the relay starts with a blank config"


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

    async def returns_immediately(*_: object) -> None:
        """Stand in for the relay, which otherwise waits for the setup page."""

    monkeypatch.setattr(cli, "_relay", returns_immediately)

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


class _SetupClient:
    """A BlinkClient stand-in for the setup callbacks."""

    def __init__(self, *, needs_code: bool = False, code_ok: bool = True) -> None:
        """Decide up front how this fake account behaves."""
        self.needs_code = needs_code
        self.code_ok = code_ok
        self.raises: Exception | None = None
        self.cameras = ["Office", "Porch"]

    async def begin_login(self, username: str, password: str) -> bool:
        """Succeed, ask for a code, or raise whatever the test set."""
        if self.raises:
            raise self.raises
        return not self.needs_code

    async def submit_two_factor(self, code: str) -> bool:
        """Accept or reject the code, or raise."""
        if self.raises:
            raise self.raises
        return self.code_ok

    def camera_names(self) -> list[str]:
        """The cameras this fake account has."""
        return self.cameras


def _state_and_handlers(client: object, camera_name: str | None = None):
    """Build the setup state and the two callbacks the page drives."""
    config = Config(
        username="",
        password="",
        camera_name=camera_name,
        data_dir=Path("/tmp"),
        config_dir=Path("/tmp"),
    )
    state = SetupState()
    return state, cli._setup_handlers(client, state, config)  # type: ignore[arg-type]


async def test_a_login_needing_a_code_moves_to_the_two_factor_stage() -> None:
    """The page has to know to ask for the code Blink just sent."""
    state, (on_login, _) = _state_and_handlers(_SetupClient(needs_code=True))

    await on_login("me@example.com", "pw")

    assert state.stage is Stage.TWO_FACTOR
    assert "code" in state.message.lower()


async def test_a_login_without_a_code_goes_straight_to_ready() -> None:
    """Some accounts do not challenge; the page should not ask for nothing."""
    state, (on_login, _) = _state_and_handlers(_SetupClient())

    await on_login("me@example.com", "pw")

    assert state.stage is Stage.READY


async def test_a_login_failure_is_shown_not_raised() -> None:
    """An exception reaching aiohttp is a 500 with no explanation."""
    client = _SetupClient()
    client.raises = InvalidCredentialsError("Blink rejected these credentials.")
    state, (on_login, _) = _state_and_handlers(client)

    await on_login("me@example.com", "wrong")

    assert state.stage is Stage.CREDENTIALS
    assert "rejected" in state.error


async def test_a_verified_code_selects_a_camera() -> None:
    """Finishing setup should leave the page able to say what it will relay."""
    state, (_, on_code) = _state_and_handlers(_SetupClient())

    await on_code("123456")

    assert state.stage is Stage.READY
    assert state.cameras == ["Office", "Porch"]
    assert state.camera == "Office", "falls back to the first camera found"


async def test_a_configured_camera_name_wins_over_the_first_found() -> None:
    """An explicit choice in the options must not be overridden."""
    state, (_, on_code) = _state_and_handlers(_SetupClient(), camera_name="Porch")

    await on_code("123456")

    assert state.camera == "Porch"


async def test_a_rejected_code_keeps_the_user_on_the_code_form() -> None:
    """A typo is recoverable; it must not advance or dead-end."""
    state, (_, on_code) = _state_and_handlers(_SetupClient(code_ok=False))
    state.stage = Stage.TWO_FACTOR

    await on_code("000000")

    assert state.stage is Stage.TWO_FACTOR
    assert "not accepted" in state.error


async def test_sign_in_falls_back_to_the_setup_page_when_connect_fails(
    monkeypatch: pytest.MonkeyPatch, fast_heartbeat: None
) -> None:
    """A stale or missing session must lead to the page, not to a dead add-on."""
    monkeypatch.setattr(cli, "SETUP_POLL_S", 0.01)
    config = Config(
        username="me@example.com",
        password="",
        camera_name=None,
        data_dir=Path("/tmp"),
        config_dir=Path("/tmp"),
    )
    state = SetupState()
    stop = asyncio.Event()

    async def connect_never_succeeds(*_: object) -> bool:
        return False

    monkeypatch.setattr(cli, "_connect", connect_never_succeeds)
    asyncio.get_running_loop().call_later(
        0.15, lambda: setattr(state, "stage", Stage.READY)
    )

    assert await cli._sign_in(_SetupClient(), config, state, stop) is True  # type: ignore[arg-type]


async def test_sign_in_routes_a_fatal_error_to_the_setup_page(
    monkeypatch: pytest.MonkeyPatch, fast_heartbeat: None
) -> None:
    """Bad stored credentials should be fixable from the page, not idle forever."""
    monkeypatch.setattr(cli, "SETUP_POLL_S", 0.01)
    config = Config(
        username="me@example.com",
        password="",
        camera_name=None,
        data_dir=Path("/tmp"),
        config_dir=Path("/tmp"),
    )
    state = SetupState()
    stop = asyncio.Event()

    async def connect_is_fatal(*_: object) -> bool:
        raise InvalidCredentialsError("Blink rejected these credentials.")

    monkeypatch.setattr(cli, "_connect", connect_is_fatal)
    asyncio.get_running_loop().call_later(0.15, stop.set)

    assert await cli._sign_in(_SetupClient(), config, state, stop) is False  # type: ignore[arg-type]
