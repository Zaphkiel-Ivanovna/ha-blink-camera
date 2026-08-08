"""Entrypoint: wire configuration, logging, the Blink client and the relay together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from collections.abc import Awaitable
from typing import Final

from .blink_client import BlinkClient, LiveSession, StreamSink
from .blinkpy_patches import apply_patches
from .config import Config, load_config, resolve_data_dir, resolve_relay_port
from .exceptions import FatalConfigError, TransientBlinkError
from .logging_setup import SecretRedactor, setup_logging
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


async def _relay(config: Config, redactor: SecretRedactor, stop: asyncio.Event) -> None:
    """Authenticate and relay until stopped, or idle if something is misconfigured."""
    client = BlinkClient(config, redactor)
    relay = StreamRelay(port=resolve_relay_port(RELAY_PORT))
    try:
        client.adopt_bootstrap_session()
        if not await _connect(client, stop):
            return
        name, camera = client.resolve_camera()
        _LOGGER.info("Relaying camera %r on demand", name)

        def open_session(sink: StreamSink) -> Awaitable[LiveSession]:
            return client.open_livestream(camera, sink)

        await relay.run(open_session, stop)
    except FatalConfigError as err:
        await _idle(str(err), stop)
    finally:
        await client.aclose()


async def _main(redactor: SecretRedactor) -> None:
    """Load configuration and hand off to the relay, or idle if there is none."""
    stop = _install_stop_handlers()
    try:
        config = load_config(resolve_data_dir())
    except FatalConfigError as err:
        await _idle(str(err), stop)
        return

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
