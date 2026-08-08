"""The relay lifecycle over real sockets: demand, renegotiation, shutdown.

Per rule 40 these go through an actual `asyncio` server and an actual client
connection. A mock writer would accept anything and prove nothing about
framing, ordering or who closes whose socket.

The Blink side is faked — no test ever calls the real cloud — but the lifecycle
rules are the production ones, with their timings shrunk by `fast_lifecycle`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterator

import pytest

from ha_blink_camera import stream_relay
from ha_blink_camera.blink_client import LiveSession, StreamSink
from ha_blink_camera.exceptions import CameraNotFoundError, TransientBlinkError
from ha_blink_camera.stream_relay import StreamRelay

_TS_PACKET = bytes([0x47]) + bytes(range(1, 188))
PAYLOAD = _TS_PACKET * 7

_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 5.0


@pytest.fixture
def fast_lifecycle(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Shrink the production timings so a renegotiation takes milliseconds."""
    monkeypatch.setattr(stream_relay, "RENEGOTIATE_AFTER_S", 0.30)
    monkeypatch.setattr(stream_relay, "IDLE_LINGER_S", 0.15)
    monkeypatch.setattr(stream_relay, "NO_FRAMES_TIMEOUT_S", 30.0)
    monkeypatch.setattr(stream_relay, "MIN_REOPEN_INTERVAL_S", 0.0)
    monkeypatch.setattr(stream_relay, "POST_CLOSE_COOLDOWN_S", 0.0)
    monkeypatch.setattr(stream_relay, "WATCH_TICK_S", 0.01)
    monkeypatch.setattr(stream_relay, "BACKOFF_INITIAL_S", 0.05)
    yield


class FakeBlinkStream:
    """Stands in for `BlinkLiveStream`, with the two behaviours that matter.

    It exposes a `clients` list the same way, and its `stop()` calls `close()`
    on every entry — which is exactly what blinkpy does at
    `livestream.py:353-356` and the reason the relay must own its own sockets.
    """

    def __init__(self) -> None:
        """Start with no sinks attached."""
        self.clients: list[StreamSink] = []
        self.stopped = False

    def stop(self) -> None:
        """End the session, closing every attached sink."""
        self.stopped = True
        for sink in self.clients:
            sink.close()


class FakeCloud:
    """The Blink side of the relay: counts sessions and emits payloads."""

    def __init__(
        self,
        *,
        fails: int = 0,
        fatal: Exception | None = None,
        silent: bool = False,
    ) -> None:
        """Fail the first `fails` opens, raise `fatal`, or open but send nothing."""
        self.opens = 0
        self.streams: list[FakeBlinkStream] = []
        self._fails = fails
        self._fatal = fatal
        self._silent = silent

    async def open_session(self, sink: StreamSink) -> LiveSession:
        """Open one fake liveview that streams until its sink is closed."""
        self.opens += 1
        if self._fatal is not None:
            raise self._fatal
        if self.opens <= self._fails:
            raise TransientBlinkError("fake cloud is unavailable")

        stream = FakeBlinkStream()
        stream.clients.append(sink)
        self.streams.append(stream)

        async def feed() -> None:
            try:
                while not sink.is_closing():
                    if not self._silent:
                        sink.write(PAYLOAD)
                        await sink.drain()
                    await asyncio.sleep(0.005)
            finally:
                stream.stop()

        return LiveSession(stream, asyncio.create_task(feed()))


def _start(
    port: int, cloud: FakeCloud
) -> tuple[StreamRelay, asyncio.Task[None], asyncio.Event]:
    """Bring a relay up on `port`, fed by `cloud`."""
    relay = StreamRelay(host="127.0.0.1", port=port)
    stop = asyncio.Event()
    task = asyncio.create_task(relay.run(cloud.open_session, stop))
    return relay, task, stop


async def _connect(
    relay: StreamRelay, port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect a consumer and wait until the relay has actually attached it."""
    async with asyncio.timeout(_CONNECT_TIMEOUT_S):
        while True:
            try:
                streams = await asyncio.open_connection("127.0.0.1", port)
                break
            except OSError:
                await asyncio.sleep(0.02)
        while relay._broadcaster.client_count == 0:
            await asyncio.sleep(0.005)
    return streams


async def _read_payloads(reader: asyncio.StreamReader, count: int) -> bytes:
    """Read exactly `count` payloads, failing the test rather than hanging."""
    async with asyncio.timeout(_READ_TIMEOUT_S):
        return await reader.readexactly(len(PAYLOAD) * count)


async def _reaches_eof(reader: asyncio.StreamReader) -> bool:
    """Drain whatever is still buffered and report whether the peer closed.

    Reading a single byte would not do: a consumer that was mid-stream still has
    payloads queued, and those arrive before the EOF does.
    """
    async with asyncio.timeout(_READ_TIMEOUT_S):
        await reader.read(-1)
    return reader.at_eof()


async def _connect_raw(port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect without waiting to be attached, for relays about to tear down."""
    async with asyncio.timeout(_CONNECT_TIMEOUT_S):
        while True:
            try:
                return await asyncio.open_connection("127.0.0.1", port)
            except OSError:
                await asyncio.sleep(0.02)


async def _until(predicate: Callable[[], bool]) -> None:
    """Wait for a condition, failing the test rather than hanging."""
    async with asyncio.timeout(_READ_TIMEOUT_S):
        while not predicate():
            await asyncio.sleep(0.01)


async def _stop_relay(task: asyncio.Task[None], stop: asyncio.Event) -> None:
    """Ask the relay to stop and insist that it actually does."""
    stop.set()
    await asyncio.wait_for(task, timeout=_READ_TIMEOUT_S)


async def _drop(writer: asyncio.StreamWriter) -> None:
    """Disconnect a consumer the way ffmpeg exiting would."""
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


async def test_no_consumer_means_no_blink_call_at_all(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """Demand-gating: an idle add-on must cost the Blink account nothing."""
    cloud = FakeCloud()
    _, task, stop = _start(unused_tcp_port, cloud)

    await asyncio.sleep(0.4)
    await _stop_relay(task, stop)

    assert cloud.opens == 0


async def test_a_session_opens_only_once_a_consumer_connects(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """The liveview is opened on demand, not at startup."""
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    await asyncio.sleep(0.1)
    assert cloud.opens == 0

    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        received = await _read_payloads(reader, 2)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert cloud.opens == 1
    assert received == PAYLOAD * 2


async def test_renegotiation_keeps_the_consumer_connected(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """The point of M3: the Blink session cycles, the downstream socket does not.

    blinkpy ends a session by closing every entry in `stream.clients`, so the
    relay owns the consumer sockets and hands each new session a fresh sink.
    """
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        await _until(lambda: cloud.opens >= 3)
        received = await _read_payloads(reader, 20)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert cloud.opens >= 3, "the session never cycled"
    assert received == PAYLOAD * 20, "the byte stream broke across a renegotiation"
    assert all(stream.stopped for stream in cloud.streams[:-1])


async def test_the_session_is_released_when_the_last_consumer_leaves(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """Nobody watching means no reason to hold a Blink liveview open."""
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)
    await _read_payloads(reader, 1)

    await _drop(writer)
    try:
        await _until(lambda: bool(cloud.streams) and cloud.streams[-1].stopped)
        opens_after_release = cloud.opens
        await asyncio.sleep(0.3)
    finally:
        await _stop_relay(task, stop)

    assert cloud.opens == opens_after_release, "it reopened with nobody attached"


async def test_a_reconnect_within_the_linger_reuses_the_session(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """A bridge restarting must not cost a fresh liveview command."""
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)
    await _read_payloads(reader, 1)
    assert cloud.opens == 1

    await _drop(writer)
    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        received = await _read_payloads(reader, 2)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert cloud.opens == 1, "the session should have been reused"
    assert received == PAYLOAD * 2


async def test_a_transient_failure_is_retried_and_never_fatal(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """Network loss must back off and recover on its own."""
    cloud = FakeCloud(fails=2)
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        received = await _read_payloads(reader, 2)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert cloud.opens == 3, "it should have retried past both failures"
    assert received == PAYLOAD * 2


async def test_a_fatal_error_ends_the_relay(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """A wrong camera name never fixes itself, so it must not be retried."""
    cloud = FakeCloud(fatal=CameraNotFoundError("no camera named 'Salon'"))
    _, task, stop = _start(unused_tcp_port, cloud)
    _, writer = await _connect_raw(unused_tcp_port)

    with pytest.raises(CameraNotFoundError):
        await asyncio.wait_for(task, timeout=_READ_TIMEOUT_S)

    await _drop(writer)
    assert stop.is_set() is False
    assert cloud.opens == 1, "a fatal error must not be retried"


async def test_shutdown_completes_with_a_consumer_still_connected(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """SIGTERM while go2rtc is attached must not wedge the process.

    Since CPython 3.12.1 `Server.wait_closed()` also waits for every accepted
    connection to detach, and a consumer parked on `read()` never does. The
    consumer here is never closed by the test — only the relay can end this.
    """
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)

    await _stop_relay(task, stop)

    assert await _reaches_eof(reader), "the consumer should have been closed"
    writer.close()


async def test_a_closed_session_sink_does_not_drop_the_consumer(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """blinkpy closing its sink must never reach a downstream socket."""
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        await _read_payloads(reader, 1)
        cloud.streams[0].stop()

        received = await _read_payloads(reader, 2)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert cloud.streams[0].stopped
    assert received == PAYLOAD * 2, "the consumer kept receiving across the swap"


async def test_a_stalled_consumer_is_dropped_and_disconnected(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """Detaching is not enough: the consumer has to learn it was dropped."""
    cloud = FakeCloud()
    relay, task, stop = _start(unused_tcp_port, cloud)
    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        broadcaster = relay._broadcaster
        for attached in list(broadcaster._writers):
            broadcaster.drop(attached, "stalled")

        closed = await _reaches_eof(reader)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert closed, "a dropped consumer must receive EOF, not silence"


async def test_the_port_is_released_afterwards(
    unused_tcp_port: int, fast_lifecycle: None
) -> None:
    """The relay must not leave the listen socket bound, or a restart fails."""
    for _ in range(2):
        _, task, stop = _start(unused_tcp_port, FakeCloud())
        await asyncio.sleep(0.05)
        await _stop_relay(task, stop)


@pytest.mark.parametrize(
    "payload",
    [PAYLOAD, PAYLOAD[:188], PAYLOAD * 2],
    ids=["one-frame", "single-ts-packet", "two-frames"],
)
async def test_payload_sizes_are_relayed_unchanged(
    unused_tcp_port: int, fast_lifecycle: None, payload: bytes
) -> None:
    """The relay is a copier: it must not reframe, buffer-merge or pad."""
    relay = StreamRelay(host="127.0.0.1", port=unused_tcp_port)
    stop = asyncio.Event()

    async def open_session(sink: StreamSink) -> LiveSession:
        stream = FakeBlinkStream()
        stream.clients.append(sink)

        async def feed() -> None:
            while not sink.is_closing():
                sink.write(payload)
                await sink.drain()
                await asyncio.sleep(0.005)

        return LiveSession(stream, asyncio.create_task(feed()))

    task = asyncio.create_task(relay.run(open_session, stop))
    reader, writer = await _connect(relay, unused_tcp_port)
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S):
            received = await reader.readexactly(len(payload))
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    assert received == payload


async def test_a_session_that_delivers_nothing_is_backed_off(
    unused_tcp_port: int, fast_lifecycle: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A camera that opens but never sends must not burn the hourly allowance.

    Without a backoff it would reopen at the minimum interval and spend all
    twenty of the hour's sessions in a couple of minutes.
    """
    monkeypatch.setattr(stream_relay, "NO_FRAMES_TIMEOUT_S", 0.05)
    monkeypatch.setattr(stream_relay, "BACKOFF_INITIAL_S", 0.2)
    cloud = FakeCloud(silent=True)
    relay, task, stop = _start(unused_tcp_port, cloud)
    _, writer = await _connect(relay, unused_tcp_port)

    try:
        await asyncio.sleep(1.0)
    finally:
        await _drop(writer)
        await _stop_relay(task, stop)

    unthrottled = 1.0 / 0.05
    assert cloud.opens < unthrottled / 2, f"{cloud.opens} opens is not backing off"
    assert cloud.opens >= 2, "it should still have retried"
