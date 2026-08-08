"""Owns the local TCP server and the Blink session lifecycle behind it."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from .blink_client import LiveSession, StreamSink
from .exceptions import RelayError, TransientBlinkError

_LOGGER = logging.getLogger(__name__)

RELAY_HOST: Final = "0.0.0.0"
RELAY_PORT: Final = 8554

SESSION_LIFETIME_S: Final = 300.0
RENEGOTIATE_MARGIN_S: Final = 30.0
RENEGOTIATE_AFTER_S: Final = SESSION_LIFETIME_S - RENEGOTIATE_MARGIN_S

IDLE_LINGER_S: Final = 20.0
NO_FRAMES_TIMEOUT_S: Final = 45.0

MIN_REOPEN_INTERVAL_S: Final = 5.0

POST_CLOSE_COOLDOWN_S: Final = 5.0

MAX_OPENS_PER_HOUR: Final = 40
OPEN_WINDOW_S: Final = 3600.0

BACKOFF_INITIAL_S: Final = 5.0
BACKOFF_MAX_S: Final = 300.0

WATCH_TICK_S: Final = 1.0
CLIENT_DRAIN_TIMEOUT_S: Final = 5.0
CLIENT_CLOSE_TIMEOUT_S: Final = 2.0
SHUTDOWN_TIMEOUT_S: Final = 3.0

REASON_EXPIRED: Final = "renegotiating before the Blink session expires"
REASON_UPSTREAM_EOF: Final = "Blink closed the stream"
REASON_NO_DEMAND: Final = "no consumer left, releasing the camera"
REASON_NO_FRAMES: Final = "no video arrived, giving up on this session"

_CLIENT_READ_CHUNK: Final = 1024
_CLIENT_GONE: Final = (ConnectionResetError, BrokenPipeError, TimeoutError, OSError)

SessionOpener = Callable[[StreamSink], Awaitable[LiveSession]]


class _SessionGate:
    """Bounds how often a Blink liveview may be opened.

    Blink publishes no quota, so this is the only hard limit protecting the
    account from a fault that would otherwise reopen in a tight loop.
    """

    def __init__(
        self,
        max_per_window: int | None = None,
        window: float | None = None,
        min_interval: float | None = None,
        cooldown: float | None = None,
    ) -> None:
        """Start with no sessions on record, reading the limits at build time."""
        self._opens: deque[float] = deque()
        self._last_close: float | None = None
        self._max_per_window = max_per_window or MAX_OPENS_PER_HOUR
        self._window = window or OPEN_WINDOW_S
        self._min_interval = (
            MIN_REOPEN_INTERVAL_S if min_interval is None else min_interval
        )
        self._cooldown = POST_CLOSE_COOLDOWN_S if cooldown is None else cooldown

    def delay(self, now: float) -> float:
        """How long to wait before opening another session; 0.0 if allowed now."""
        while self._opens and now - self._opens[0] >= self._window:
            self._opens.popleft()

        wait = 0.0
        if self._opens:
            wait = max(wait, self._opens[-1] + self._min_interval - now)
        if self._last_close is not None:
            wait = max(wait, self._last_close + self._cooldown - now)
        if len(self._opens) >= self._max_per_window:
            wait = max(wait, self._opens[0] + self._window - now)
        return max(0.0, wait)

    def record(self, now: float) -> None:
        """Note that a session was just opened."""
        self._opens.append(now)

    def record_close(self, now: float) -> None:
        """Note that a session was just closed, so the next one is spaced from it."""
        self._last_close = now

    @property
    def opens_in_window(self) -> int:
        """How many sessions are on record for the current window."""
        return len(self._opens)

    @property
    def at_capacity(self) -> bool:
        """Whether the hourly allowance is spent, as opposed to merely spacing out."""
        return len(self._opens) >= self._max_per_window


@dataclass
class _SessionWatch:
    """Decides when a running session should end. Pure, so it tests without sockets."""

    started: float
    last_progress: float
    bytes_seen: int
    empty_since: float | None = field(default=None)

    def tick(self, now: float, relayed: int, consumers: int) -> str | None:
        """Return why the session should end, or None to keep it running."""
        if relayed != self.bytes_seen:
            self.bytes_seen = relayed
            self.last_progress = now

        if now - self.started >= RENEGOTIATE_AFTER_S:
            return REASON_EXPIRED

        if consumers == 0:
            if self.empty_since is None:
                self.empty_since = now
            elif now - self.empty_since >= IDLE_LINGER_S:
                return REASON_NO_DEMAND
            return None

        self.empty_since = None
        if now - self.last_progress >= NO_FRAMES_TIMEOUT_S:
            return REASON_NO_FRAMES
        return None


class _Broadcaster:
    """Holds the attached downstream writers and copies each payload to them all."""

    def __init__(self, drain_timeout: float = CLIENT_DRAIN_TIMEOUT_S) -> None:
        """Start with no consumers attached."""
        self._writers: list[asyncio.StreamWriter] = []
        self._drain_timeout = drain_timeout
        self._bytes_relayed = 0
        self.has_consumers = asyncio.Event()

    @property
    def client_count(self) -> int:
        """How many consumers are currently attached."""
        return len(self._writers)

    @property
    def bytes_relayed(self) -> int:
        """Total payload bytes handed to consumers since startup."""
        return self._bytes_relayed

    def attach(self, writer: asyncio.StreamWriter) -> None:
        """Start sending the stream to `writer`."""
        self._writers.append(writer)
        self.has_consumers.set()

    def detach(self, writer: asyncio.StreamWriter) -> None:
        """Stop sending the stream to `writer`, leaving its socket alone."""
        with contextlib.suppress(ValueError):
            self._writers.remove(writer)
        if not self._writers:
            self.has_consumers.clear()

    def drop(self, writer: asyncio.StreamWriter, reason: str) -> None:
        """Give up on a consumer and close its socket, so it reconnects.

        Detaching alone leaves it holding a connection that never delivers
        another byte and never reaches EOF.
        """
        self.detach(writer)
        if not writer.is_closing():
            _LOGGER.info("Dropping a consumer: %s", reason)
            writer.close()

    def write(self, data: bytes) -> None:
        """Queue one payload for every attached consumer."""
        if not self._writers:
            return
        self._bytes_relayed += len(data)
        for writer in list(self._writers):
            if writer.is_closing():
                self.detach(writer)
                continue
            try:
                writer.write(data)
            except _CLIENT_GONE as err:
                self.drop(writer, f"it stopped reading ({err})")

    async def drain(self) -> None:
        """Let each consumer catch up, dropping any that cannot."""
        for writer in list(self._writers):
            try:
                await asyncio.wait_for(writer.drain(), timeout=self._drain_timeout)
            except TimeoutError:
                self.drop(writer, f"it stalled for {self._drain_timeout:.0f}s")
            except _CLIENT_GONE as err:
                self.drop(writer, f"it went away ({err})")

    async def disconnect_all(self) -> None:
        """Close every attached consumer socket. Only ever called on shutdown."""
        for writer in list(self._writers):
            self.detach(writer)
            await _close_writer(writer)

    def new_sink(self) -> StreamSink:
        """Return a per-session sink to hand to blinkpy's `stream.clients`."""
        return _SessionSink(self)


class _SessionSink:
    """The object blinkpy writes into — one per Blink session, not per consumer."""

    def __init__(self, broadcaster: _Broadcaster) -> None:
        """Bind this sink to the relay's broadcaster."""
        self._broadcaster = broadcaster
        self._closed = False

    def write(self, data: bytes) -> None:
        """Forward one MPEG-TS payload to the attached consumers."""
        if not self._closed:
            self._broadcaster.write(data)

    async def drain(self) -> None:
        """Apply the consumers' backpressure to the upstream read loop."""
        if not self._closed:
            await self._broadcaster.drain()

    def is_closing(self) -> bool:
        """Whether this session's tap has been closed."""
        return self._closed

    def close(self) -> None:
        """Detach this session's tap. Downstream consumers are left untouched."""
        self._closed = True


class StreamRelay:
    """The local MPEG-TS re-broadcast server and the session loop feeding it."""

    def __init__(self, host: str = RELAY_HOST, port: int = RELAY_PORT) -> None:
        """Configure the listen address; nothing is bound until `run()`."""
        self._host = host
        self._port = port
        self._broadcaster = _Broadcaster()
        self._gate = _SessionGate()

    async def run(self, open_session: SessionOpener, stop: asyncio.Event) -> None:
        """Accept consumers and keep a Blink session running for as long as they ask."""
        server = await self._bind()
        try:
            await _first_of(self._supervise(open_session, stop), stop.wait())
        finally:
            await self._shutdown(server)

    async def _supervise(
        self, open_session: SessionOpener, stop: asyncio.Event
    ) -> None:
        """Open, run and reopen sessions for as long as a consumer wants one."""
        backoff = BACKOFF_INITIAL_S
        while not stop.is_set():
            await self._broadcaster.has_consumers.wait()
            setback = await self._attempt(open_session)
            if setback is None:
                backoff = BACKOFF_INITIAL_S
                continue
            _LOGGER.warning("%s — waiting %.0fs before trying again", setback, backoff)
            await wait_or_stop(backoff, stop)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    async def _attempt(self, open_session: SessionOpener) -> str | None:
        """Run one session, returning why it was unproductive, or None if it was fine.

        A session that opens and delivers nothing counts as a setback, not a
        success. Otherwise a camera that always fails to send would reopen at the
        minimum interval and spend the whole hourly allowance in a couple of
        minutes.
        """
        try:
            if await self._one_session(open_session) > 0:
                return None
        except TransientBlinkError as err:
            return f"Session failed ({err})"
        return "That session delivered no video"

    async def _one_session(self, open_session: SessionOpener) -> int:
        """Open one Blink session, pump it, and report how many bytes it relayed."""
        await self._wait_for_slot()
        loop = asyncio.get_running_loop()
        self._gate.record(loop.time())
        _LOGGER.info(
            "Opening a Blink liveview (%d/%d this hour)",
            self._gate.opens_in_window,
            MAX_OPENS_PER_HOUR,
        )

        session = await open_session(self._broadcaster.new_sink())
        started_bytes = self._broadcaster.bytes_relayed
        try:
            reason = await self._pump(session)
        finally:
            await session.aclose()
            self._gate.record_close(loop.time())

        relayed = self._broadcaster.bytes_relayed - started_bytes
        _LOGGER.info(
            "Session ended after %.1f MiB: %s", relayed / (1024 * 1024), reason
        )
        return relayed

    async def _wait_for_slot(self) -> None:
        """Hold off until the rate limit allows another session.

        The two reasons to wait are reported differently on purpose. Spacing out
        a quick reopen is routine; running out of the hour's allowance means the
        camera is about to go dark and someone should know why.
        """
        loop = asyncio.get_running_loop()
        delay = self._gate.delay(loop.time())
        if delay <= 0:
            return
        if self._gate.at_capacity:
            _LOGGER.warning(
                "Hourly session limit reached (%d) — no video for %.0fs",
                self._gate.opens_in_window,
                delay,
            )
        else:
            _LOGGER.info("Reopening too soon after the last session; %.0fs", delay)
        await asyncio.sleep(delay)

    async def _pump(self, session: LiveSession) -> str:
        """Run a session until the watch says stop, returning why."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        watch = _SessionWatch(
            started=now,
            last_progress=now,
            bytes_seen=self._broadcaster.bytes_relayed,
        )

        ended = asyncio.ensure_future(session.wait())
        try:
            while True:
                done, _ = await asyncio.wait({ended}, timeout=WATCH_TICK_S)
                if ended in done:
                    ended.result()
                    return REASON_UPSTREAM_EOF
                reason = watch.tick(
                    loop.time(),
                    self._broadcaster.bytes_relayed,
                    self._broadcaster.client_count,
                )
                if reason is not None:
                    return reason
        finally:
            ended.cancel()
            await asyncio.gather(ended, return_exceptions=True)

    async def _shutdown(self, server: asyncio.Server) -> None:
        """Stop accepting, close the consumers, *then* wait for the server.

        Since CPython 3.12.1 `Server.wait_closed()` also waits for every accepted
        connection to detach, and a consumer parked on `read()` never does — so
        waiting before closing them hangs until S6 resorts to SIGKILL.
        """
        server.close()
        await self._broadcaster.disconnect_all()
        if await _wait_closed(server):
            return
        _LOGGER.warning("A consumer socket would not close; aborting it")
        server.abort_clients()
        await _wait_closed(server)

    async def _bind(self) -> asyncio.Server:
        """Bind the listen socket, or explain why it could not be bound."""
        try:
            server = await asyncio.start_server(
                self._handle_client, self._host, self._port
            )
        except OSError as err:
            raise RelayError(
                f"Cannot listen on {self._host}:{self._port}: {err}"
            ) from err
        _LOGGER.info("Relay listening on tcp://%s:%d", self._host, self._port)
        return server

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Attach one downstream consumer for as long as it stays connected."""
        peer = writer.get_extra_info("peername")
        self._broadcaster.attach(writer)
        _LOGGER.info(
            "Consumer connected from %s (%d attached)",
            peer,
            self._broadcaster.client_count,
        )
        try:
            while await reader.read(_CLIENT_READ_CHUNK):
                pass
        except _CLIENT_GONE as err:
            _LOGGER.debug("Consumer %s dropped: %s", peer, err)
        finally:
            self._broadcaster.detach(writer)
            await _close_writer(writer)
            _LOGGER.info(
                "Consumer %s disconnected (%d attached)",
                peer,
                self._broadcaster.client_count,
            )


async def _wait_closed(server: asyncio.Server) -> bool:
    """Wait for the server to finish closing; False if it took too long."""
    try:
        async with asyncio.timeout(SHUTDOWN_TIMEOUT_S):
            await server.wait_closed()
    except TimeoutError:
        return False
    return True


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    """Close a downstream socket, without ever absorbing a cancel request."""
    if not writer.is_closing():
        writer.close()
    try:
        async with asyncio.timeout(CLIENT_CLOSE_TIMEOUT_S):
            await writer.wait_closed()
    except _CLIENT_GONE:
        _LOGGER.debug("Consumer socket did not close cleanly")


async def wait_or_stop(seconds: float, stop: asyncio.Event) -> None:
    """Sleep, cutting it short if a stop is requested."""
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await stop.wait()


async def _first_of(*awaitables: Awaitable[object]) -> None:
    """Wait for the first awaitable, cancel the rest, re-raise whatever it raised."""
    tasks = [asyncio.ensure_future(item) for item in awaitables]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    for task in done:
        task.result()
