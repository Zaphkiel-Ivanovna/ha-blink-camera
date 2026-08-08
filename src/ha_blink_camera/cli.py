"""Entrypoint: wire configuration, logging, the Blink client and the relay together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from typing import Final

from .blink_client import BlinkClient, LiveSession, StreamSink
from .blinkpy_patches import apply_patches
from .config import (
    Config,
    load_config_or_blank,
    resolve_data_dir,
    resolve_relay_port,
)
from .exceptions import BlinkCameraError, FatalConfigError, TransientBlinkError
from .logging_setup import SecretRedactor, setup_logging
from .setup_ui import SetupState, Stage, build_app, serve
from .stream_relay import (
    BACKOFF_INITIAL_S,
    BACKOFF_MAX_S,
    RELAY_PORT,
    StreamRelay,
    wait_or_stop,
)

_LOGGER = logging.getLogger("ha_blink_camera")

_STOP_SIGNALS: Final = (signal.SIGINT, signal.SIGTERM)
HEARTBEAT_S: Final = 900.0
SETUP_POLL_S: Final = 2.0


async def _idle(reason: str, stop: asyncio.Event) -> None:
    """Stay alive, doing nothing, until someone fixes the configuration.

    Exiting instead would be an immediate S6 respawn, and a respawn loop against
    Blink's login endpoint is how an account gets locked out.
    """
    _LOGGER.error("%s", reason)
    _LOGGER.error("Nothing further will be tried until this is fixed.")
    while not stop.is_set():
        await wait_or_stop(HEARTBEAT_S, stop)
        if not stop.is_set():
            _LOGGER.warning("Still idle: %s", reason)


async def _connect(client: BlinkClient, stop: asyncio.Event) -> bool:
    """Authenticate, retrying a transient failure for as long as it takes."""
    backoff = BACKOFF_INITIAL_S
    while not stop.is_set():
        try:
            await client.connect()
        except TransientBlinkError as err:
            _LOGGER.warning("%s Retrying in %.0fs", err, backoff)
            await wait_or_stop(backoff, stop)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
            continue
        return True
    return False


async def _await_setup(state: SetupState, stop: asyncio.Event) -> bool:
    """Hold until someone completes the setup page, or the add-on stops."""
    _LOGGER.error("ACTION REQUIRED: open this add-on's Web UI tab and sign in.")
    while not stop.is_set():
        await wait_or_stop(SETUP_POLL_S, stop)
        if state.stage is Stage.READY:
            return True
    return False


async def _relay(config: Config, redactor: SecretRedactor, stop: asyncio.Event) -> None:
    """Authenticate and relay until stopped, or idle if something is misconfigured."""
    client = BlinkClient(config, redactor)
    relay = StreamRelay(port=resolve_relay_port(RELAY_PORT))
    state = SetupState()
    runner = await serve(build_app(state, *_setup_handlers(client, state, config)))
    try:
        client.adopt_bootstrap_session()
        if not await _sign_in(client, config, state, stop):
            return
        name, camera = client.resolve_camera()
        state.stage, state.camera = Stage.READY, name
        state.cameras = client.camera_names()
        _LOGGER.info("Relaying camera %r on demand", name)

        def open_session(sink: StreamSink) -> Awaitable[LiveSession]:
            return client.open_livestream(camera, sink)

        await relay.run(open_session, stop)
    except FatalConfigError as err:
        await _idle(str(err), stop)
    finally:
        await runner.cleanup()
        await client.aclose()


def _setup_handlers(
    client: BlinkClient, state: SetupState, config: Config
) -> tuple[Callable[[str, str], Awaitable[None]], Callable[[str], Awaitable[None]]]:
    """Build the two callbacks the setup page drives."""

    async def on_login(username: str, password: str) -> None:
        state.error = state.message = ""
        try:
            done = await client.begin_login(username, password)
        except BlinkCameraError as err:
            state.error = str(err)
            return
        state.stage = Stage.READY if done else Stage.TWO_FACTOR
        state.message = "" if done else "Blink sent you a code."

    async def on_code(code: str) -> None:
        state.error = state.message = ""
        try:
            if not await client.submit_two_factor(code):
                state.error = "That code was not accepted. Try the newest one."
                return
        except BlinkCameraError as err:
            state.error = str(err)
            return
        state.stage = Stage.READY
        state.cameras = client.camera_names()
        state.camera = config.camera_name or (
            state.cameras[0] if state.cameras else None
        )

    return on_login, on_code


async def _sign_in(
    client: BlinkClient, config: Config, state: SetupState, stop: asyncio.Event
) -> bool:
    """Authenticate from the stored session, or hand over to the setup page."""
    if config.username:
        try:
            if await _connect(client, stop):
                state.stage = Stage.READY
                return True
        except FatalConfigError as err:
            _LOGGER.warning("%s", err)
    return await _await_setup(state, stop)


async def _main(redactor: SecretRedactor) -> None:
    """Load configuration and hand off to the relay, or idle if there is none."""
    stop = _install_stop_handlers()
    config = load_config_or_blank(resolve_data_dir())

    redactor.add(config.username)
    apply_patches()
    await _relay(config, redactor, stop)


def _install_stop_handlers() -> asyncio.Event:
    """Return an Event set on SIGINT/SIGTERM, handled inside the loop."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in _STOP_SIGNALS:
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    return stop


def main(argv: list[str] | None = None) -> int:
    """Run the add-on. Never returns non-zero for a condition a human must fix."""
    del argv
    redactor = setup_logging()
    _LOGGER.info("Starting Blink camera streamer")

    try:
        asyncio.run(_main(redactor))
    except KeyboardInterrupt:
        return 0
    except Exception:
        _LOGGER.exception("Unexpected failure")
        return 1

    _LOGGER.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
