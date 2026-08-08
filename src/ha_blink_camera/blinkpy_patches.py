"""The two upstream blinkpy bug patches, isolated — and nothing else."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import ssl
from collections.abc import Callable, Coroutine
from typing import Any, Final

import aiohttp
from blinkpy import api as blink_api
from blinkpy.auth import UnauthorizedError
from blinkpy.livestream import BlinkLiveStream

_LOGGER = logging.getLogger(__name__)

_HEADER_LENGTH: Final = 9
_PAYLOAD_LENGTH_SLICE: Final = slice(5, 9)
_MSGTYPE_VIDEO: Final = 0x00
_TS_SYNC_BYTE: Final = 0x47
_MAX_PAYLOAD_LENGTH: Final = 1 << 20

POLL_MAX_FAILURES: Final = 5
_COMMAND_RUNNING_STATES: Final = frozenset({"new", "running"})
_STATUS_CODE_IN_PROGRESS: Final = 908
_UNKNOWN_NETWORK: Final = "unknown"

_TRANSIENT_POLL_ERRORS: Final = (
    OSError,
    TimeoutError,
    aiohttp.ClientError,
    KeyError,
    TypeError,
    ValueError,
    UnauthorizedError,
)


async def _recv_readexactly(self: Any) -> None:
    """Replacement for `BlinkLiveStream.recv()` that frames with `readexactly()`.

    Upstream bug: https://github.com/fronzbot/blinkpy/issues/1262
    Upstream fix (open, unmerged): https://github.com/fronzbot/blinkpy/pull/1232

    `recv()` reads IMMI frames with `StreamReader.read(n)`, which returns *up to*
    n bytes, then treats the short read as fatal and breaks out of the loop.
    """
    try:
        while True:
            header = await self.target_reader.readexactly(_HEADER_LENGTH)
            msgtype = header[0]
            payload_length = int.from_bytes(
                header[_PAYLOAD_LENGTH_SLICE], byteorder="big"
            )
            if payload_length <= 0:
                continue
            if payload_length > _MAX_PAYLOAD_LENGTH:
                _LOGGER.warning(
                    "Implausible frame length %d — treating the stream as lost",
                    payload_length,
                )
                return

            payload = await self.target_reader.readexactly(payload_length)
            if msgtype != _MSGTYPE_VIDEO or payload[0] != _TS_SYNC_BYTE:
                continue

            for writer in list(self.clients):
                if not writer.is_closing():
                    writer.write(payload)
                    await writer.drain()
            await asyncio.sleep(0)
    except asyncio.IncompleteReadError:
        _LOGGER.debug("Upstream closed the livestream (EOF)")
    except (ConnectionResetError, BrokenPipeError, ssl.SSLError) as err:
        _LOGGER.debug("Livestream connection closed: %s", err)
    finally:
        if self.target_writer is not None and not self.target_writer.is_closing():
            self.target_writer.close()


async def _poll_tolerant(self: Any) -> None:
    """Replacement for `BlinkLiveStream.poll()` that survives a transient failure.

    No upstream issue filed; the bug is in blinkpy 0.25.9 at
    https://github.com/fronzbot/blinkpy/blob/v0.25.9/blinkpy/livestream.py#L304 —
    `poll()` leaves its loop on the first reply whose `status_code` is not 908,
    and its `finally` then asks Blink to tear the session down.
    """
    network = _network_id(self)
    cancelled: asyncio.CancelledError | None = None
    try:
        await _poll_loop(self, network)
    except asyncio.CancelledError as err:
        cancelled = err
    finally:
        await _release_command(self, network)

    if cancelled is not None:
        raise cancelled


async def _poll_loop(self: Any, network: str) -> None:
    """Poll Blink's command API until the command ends or fails too often."""
    failures = 0
    while not self.target_reader.at_eof():
        response = await _command_status(self, network)

        if not response or response.get("status_code", 0) != _STATUS_CODE_IN_PROGRESS:
            failures += 1
            if failures >= POLL_MAX_FAILURES:
                _LOGGER.warning(
                    "Command polling failed %d times in a row, ending the session",
                    failures,
                )
                return
            await asyncio.sleep(self.polling_interval)
            continue

        failures = 0
        if _command_finished(response, self.command_id):
            _LOGGER.debug("Blink ended the livestream command")
            return

        await asyncio.sleep(self.polling_interval)


async def _command_status(self: Any, network: str) -> dict[str, Any] | None:
    """Ask Blink how the liveview command is doing; None on a transient failure."""
    try:
        response = await blink_api.request_command_status(
            self.camera.sync.blink, network, self.command_id
        )
    except _TRANSIENT_POLL_ERRORS as err:
        _LOGGER.debug("Command polling hiccup: %s", err)
        return None
    return response if isinstance(response, dict) else None


def _command_finished(response: dict[str, Any], command_id: int) -> bool:
    """Whether the response says our command has left the running states."""
    for command in response.get("commands", []):
        if command.get("id") == command_id:
            return command.get("state_condition") not in _COMMAND_RUNNING_STATES
    return False


def _network_id(self: Any) -> str:
    """Resolve the network id, falling back to the sync module's."""
    network = getattr(self.camera, "network_id", None)
    if not network or network == _UNKNOWN_NETWORK:
        network = self.camera.sync.network_id
    return str(network)


async def _release_command(self: Any, network: str) -> None:
    """Tell Blink we are done with the command. Best effort."""
    with contextlib.suppress(*_TRANSIENT_POLL_ERRORS):
        await blink_api.request_command_done(
            self.camera.sync.blink, network, self.command_id
        )


_PATCHES: Final[dict[str, tuple[Callable[..., Coroutine[Any, Any, None]], str]]] = {
    "recv": (_recv_readexactly, "readexactly"),
    "poll": (_poll_tolerant, "failures"),
}


def apply_patches() -> tuple[str, ...]:
    """Patch `BlinkLiveStream` where the installed blinkpy still needs it.

    Returns the methods actually replaced, and never double-patches: a blinkpy
    whose source already carries the fix is left alone and warned about.
    """
    applied: list[str] = []
    for name, (replacement, marker) in _PATCHES.items():
        current = getattr(BlinkLiveStream, name)
        if current is replacement:
            continue
        if _already_fixed(current, marker):
            _LOGGER.warning(
                "blinkpy's BlinkLiveStream.%s() already carries the fix — "
                "the local patch is now dead code and should be removed",
                name,
            )
            continue
        setattr(BlinkLiveStream, name, replacement)
        applied.append(name)

    _LOGGER.debug("Patched BlinkLiveStream: %s", ", ".join(applied) or "nothing")
    return tuple(applied)


def _already_fixed(method: object, marker: str) -> bool:
    """Whether the installed method's source already contains the fix marker."""
    try:
        return marker in inspect.getsource(method)  # type: ignore[arg-type]
    except (OSError, TypeError):
        return False
