"""Regression tests for the two upstream blinkpy patches.

Rule 00 §2 makes a dedicated test per patch a MUST, and rule 40 says why these
in particular cannot be written with mocks: the bug being patched *is* a short
read, and a `Mock(spec=StreamReader)` returns whatever it was told to. So these
run against a real `asyncio` server that fragments its writes on purpose.

`test_upstream_recv_truncates_the_same_stream` is what makes the rest worth
anything: it pins the actual upstream bug, so a suite that stayed green after
the patch was reverted would fail here instead.

The poll() matrix (single failure tolerated, N consecutive stopping cleanly,
counter reset, command released exactly once) lands in M2 with the shared fake
relay; this file covers recv() and the patch machinery.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

import pytest
from blinkpy.livestream import BlinkLiveStream

from ha_blink_camera.blinkpy_patches import _recv_readexactly, apply_patches

_UPSTREAM_RECV: Final = BlinkLiveStream.recv
_UPSTREAM_POLL: Final = BlinkLiveStream.poll

_MSGTYPE_VIDEO: Final = 0x00
_MSGTYPE_KEEPALIVE: Final = 0x0A
_TS_PACKET: Final = bytes([0x47]) + bytes(range(1, 188))
PAYLOAD: Final = _TS_PACKET * 7

_TIMEOUT_S: Final = 5.0


def frame(msgtype: int, payload: bytes, sequence: int = 1) -> bytes:
    """Build one IMMI frame: msgtype(1) + sequence(4 BE) + length(4 BE) + payload."""
    return (
        bytes([msgtype])
        + sequence.to_bytes(4, "big")
        + len(payload).to_bytes(4, "big")
        + payload
    )


class CollectingSink:
    """A `stream.clients` entry that just remembers everything written to it."""

    def __init__(self) -> None:
        """Start empty and open."""
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        """Record one payload."""
        self.data += data

    async def drain(self) -> None:
        """Nothing to flush."""

    def is_closing(self) -> bool:
        """Open until closed."""
        return self.closed

    def close(self) -> None:
        """Mark closed."""
        self.closed = True


class FakeLiveStream:
    """The attributes `recv()` touches on a `BlinkLiveStream`, and no more."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Point the fake at a real socket pair."""
        self.target_reader = reader
        self.target_writer = writer
        self.clients: list[CollectingSink] = []


Fragmenter = Callable[[bytes], list[bytes]]


def byte_at_a_time(data: bytes) -> list[bytes]:
    """The hostile case: every frame split across as many TCP segments as bytes."""
    return [data[i : i + 1] for i in range(len(data))]


def coalesced(data: bytes) -> list[bytes]:
    """The other hostile case: many frames arriving in one segment."""
    return [data]


def split_at(offset: int) -> Fragmenter:
    """Split once at `offset` — used to cut a header, or a payload, in half."""

    def fragment(data: bytes) -> list[bytes]:
        return [data[:offset], data[offset:]]

    return fragment


@pytest.fixture
async def serve() -> AsyncIterator[Callable[[bytes, Fragmenter], Any]]:
    """Serve a scripted byte sequence over a real socket, fragmented on demand."""
    servers: list[asyncio.Server] = []
    writers: list[asyncio.StreamWriter] = []

    async def start(data: bytes, fragmenter: Fragmenter) -> FakeLiveStream:
        async def handle(_: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            for chunk in fragmenter(data):
                writer.write(chunk)
                await writer.drain()
                await asyncio.sleep(0)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(writer)
        return FakeLiveStream(reader, writer)

    yield start

    for writer in writers:
        writer.close()
    for server in servers:
        server.close()
        await server.wait_closed()


async def _relay(stream: FakeLiveStream, recv: Any) -> CollectingSink:
    """Run one `recv()` implementation to completion and return what it relayed."""
    sink = CollectingSink()
    stream.clients.append(sink)
    async with asyncio.timeout(_TIMEOUT_S):
        await recv(stream)
    return sink


@pytest.mark.parametrize(
    "fragmenter",
    [byte_at_a_time, coalesced, split_at(4), split_at(9), split_at(600)],
    ids=["byte-at-a-time", "coalesced", "header-split", "at-boundary", "payload-split"],
)
async def test_recv_relays_every_byte_however_it_is_fragmented(
    serve: Any, fragmenter: Fragmenter
) -> None:
    """The patch's whole purpose: a short read is normal and must not end the stream."""
    wire = b"".join(frame(_MSGTYPE_VIDEO, PAYLOAD, i) for i in range(3))
    stream = await serve(wire, fragmenter)

    sink = await _relay(stream, _recv_readexactly)

    assert bytes(sink.data) == PAYLOAD * 3


async def test_upstream_recv_truncates_the_same_stream(serve: Any) -> None:
    """Pins the bug: without the patch, fragmentation loses data.

    If this ever passes, upstream has fixed `read(n)` -> `readexactly(n)` and the
    patch — along with these tests — should be deleted.
    """
    wire = b"".join(frame(_MSGTYPE_VIDEO, PAYLOAD, i) for i in range(3))
    stream = await serve(wire, byte_at_a_time)

    sink = await _relay(stream, _UPSTREAM_RECV)

    assert bytes(sink.data) != PAYLOAD * 3


async def test_recv_drops_non_video_frames(serve: Any) -> None:
    """Keep-alives and latency stats share the wire and are not video."""
    wire = (
        frame(_MSGTYPE_KEEPALIVE, b"\x47" + b"\x00" * 20)
        + frame(_MSGTYPE_VIDEO, PAYLOAD)
        + frame(0x12, b"\x47" + b"\x00" * 23)
    )
    stream = await serve(wire, byte_at_a_time)

    sink = await _relay(stream, _recv_readexactly)

    assert bytes(sink.data) == PAYLOAD


async def test_recv_drops_payloads_that_are_not_transport_stream(serve: Any) -> None:
    """A video payload not starting with the 0x47 sync byte is not usable."""
    wire = frame(_MSGTYPE_VIDEO, b"\x00" * 1316) + frame(_MSGTYPE_VIDEO, PAYLOAD)
    stream = await serve(wire, coalesced)

    sink = await _relay(stream, _recv_readexactly)

    assert bytes(sink.data) == PAYLOAD


async def test_recv_skips_header_only_frames(serve: Any) -> None:
    """A zero-length payload is a normal keep-alive shape, not an error."""
    wire = frame(_MSGTYPE_VIDEO, b"") + frame(_MSGTYPE_VIDEO, PAYLOAD)
    stream = await serve(wire, byte_at_a_time)

    sink = await _relay(stream, _recv_readexactly)

    assert bytes(sink.data) == PAYLOAD


async def test_recv_returns_cleanly_on_eof(serve: Any) -> None:
    """A truncated final frame is the end of the stream, not a hang or a crash."""
    wire = frame(_MSGTYPE_VIDEO, PAYLOAD) + frame(_MSGTYPE_VIDEO, PAYLOAD)[:400]
    stream = await serve(wire, coalesced)

    sink = await _relay(stream, _recv_readexactly)

    assert bytes(sink.data) == PAYLOAD


async def test_recv_gives_up_on_an_implausible_frame_length(serve: Any) -> None:
    """`readexactly` waits forever, so a desynced length must not park the loop.

    Upstream's bug was ending the stream on a short read; the cure must not
    introduce the opposite failure, where recv() blocks silently until Blink's
    own expiry closes the socket.
    """
    absurd = (
        bytes([_MSGTYPE_VIDEO]) + (1).to_bytes(4, "big") + (2**31).to_bytes(4, "big")
    )
    stream = await serve(frame(_MSGTYPE_VIDEO, PAYLOAD) + absurd, coalesced)

    sink = await _relay(stream, _recv_readexactly)

    assert bytes(sink.data) == PAYLOAD


async def test_recv_closes_the_target_writer_on_exit(serve: Any) -> None:
    """`send()` loops until the writer closes; without this the keep-alives never stop."""
    stream = await serve(frame(_MSGTYPE_VIDEO, PAYLOAD), coalesced)

    await _relay(stream, _recv_readexactly)

    assert stream.target_writer.is_closing()


async def test_recv_stops_writing_to_a_closed_sink(serve: Any) -> None:
    """A sink blinkpy has closed must not keep receiving data."""
    wire = b"".join(frame(_MSGTYPE_VIDEO, PAYLOAD, i) for i in range(3))
    stream = await serve(wire, coalesced)
    sink = CollectingSink()
    sink.close()
    stream.clients.append(sink)

    async with asyncio.timeout(_TIMEOUT_S):
        await _recv_readexactly(stream)

    assert bytes(sink.data) == b""


@pytest.fixture
def unpatched() -> AsyncIterator[None]:
    """Restore the class after a test that patches it."""
    BlinkLiveStream.recv = _UPSTREAM_RECV
    BlinkLiveStream.poll = _UPSTREAM_POLL
    yield
    BlinkLiveStream.recv = _UPSTREAM_RECV
    BlinkLiveStream.poll = _UPSTREAM_POLL


def test_both_patches_apply_to_the_pinned_blinkpy(unpatched: None) -> None:
    """If this stops reporting both, the pinned blinkpy has changed underneath us."""
    assert apply_patches() == ("recv", "poll")


def test_applying_twice_patches_nothing_the_second_time(unpatched: None) -> None:
    """Rule 00 §2: warn, never double-patch."""
    apply_patches()

    assert apply_patches() == ()


def test_the_guard_leaves_a_fixed_blinkpy_alone(unpatched: None) -> None:
    """A blinkpy whose source already contains the fix must not be patched over."""

    async def recv_with_the_fix(self: Any) -> None:
        """Pretend upstream merged it: the marker is `readexactly`."""
        await self.target_reader.readexactly(9)

    BlinkLiveStream.recv = recv_with_the_fix

    assert "recv" not in apply_patches()
    assert BlinkLiveStream.recv is recv_with_the_fix
