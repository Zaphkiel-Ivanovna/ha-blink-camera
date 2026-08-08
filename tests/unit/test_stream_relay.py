"""The session lifecycle rules, tested as pure logic with no sockets involved."""

from __future__ import annotations

import pytest

from ha_blink_camera import stream_relay
from ha_blink_camera.stream_relay import (
    REASON_EXPIRED,
    REASON_NO_DEMAND,
    REASON_NO_FRAMES,
    _SessionGate,
    _SessionWatch,
)


def _watch(now: float = 0.0, relayed: int = 0) -> _SessionWatch:
    """A watch for a session that just started."""
    return _SessionWatch(started=now, last_progress=now, bytes_seen=relayed)


class TestSessionWatch:
    """When a running session should be ended, and why."""

    def test_a_healthy_session_keeps_running(self) -> None:
        """Frames arriving, a consumer attached: nothing to do."""
        watch = _watch()

        assert watch.tick(now=10.0, relayed=5000, consumers=1) is None

    def test_it_renegotiates_before_blink_expires_the_session(self) -> None:
        """Blink drops the liveview after ~5-6 min; we reopen ahead of that."""
        watch = _watch()

        reason = watch.tick(
            now=stream_relay.RENEGOTIATE_AFTER_S, relayed=5000, consumers=1
        )

        assert reason == REASON_EXPIRED

    def test_expiry_wins_even_with_no_consumers(self) -> None:
        """Whichever deadline comes first ends the session."""
        watch = _watch()

        reason = watch.tick(
            now=stream_relay.RENEGOTIATE_AFTER_S + 1, relayed=0, consumers=0
        )

        assert reason == REASON_EXPIRED

    def test_the_session_survives_a_brief_gap_with_no_consumers(self) -> None:
        """A bridge restarting must not cost a whole renegotiation."""
        watch = _watch()

        assert watch.tick(now=1.0, relayed=0, consumers=0) is None
        nearly = stream_relay.IDLE_LINGER_S - 1
        assert watch.tick(now=nearly, relayed=0, consumers=0) is None

    def test_the_session_is_released_once_nobody_is_watching(self) -> None:
        """Holding a Blink liveview nobody consumes is what the ceiling protects."""
        watch = _watch()
        watch.tick(now=1.0, relayed=0, consumers=0)

        reason = watch.tick(
            now=1.0 + stream_relay.IDLE_LINGER_S, relayed=0, consumers=0
        )

        assert reason == REASON_NO_DEMAND

    def test_a_returning_consumer_cancels_the_release(self) -> None:
        """Rapid reconnect inside the linger reuses the session — no new Blink call."""
        watch = _watch()
        watch.tick(now=1.0, relayed=0, consumers=0)
        watch.tick(now=2.0, relayed=0, consumers=1)

        later = 2.0 + stream_relay.IDLE_LINGER_S
        assert watch.tick(now=later, relayed=100, consumers=0) is None

    def test_a_camera_that_sends_nothing_is_given_up_on(self) -> None:
        """A liveview that never produces video must not be held open forever."""
        watch = _watch()

        reason = watch.tick(
            now=stream_relay.NO_FRAMES_TIMEOUT_S, relayed=0, consumers=1
        )

        assert reason == REASON_NO_FRAMES

    def test_arriving_frames_reset_the_no_frames_timer(self) -> None:
        """Progress is measured in bytes actually relayed, not in wall clock."""
        watch = _watch()
        watch.tick(now=stream_relay.NO_FRAMES_TIMEOUT_S - 1, relayed=1316, consumers=1)

        still_fine = stream_relay.NO_FRAMES_TIMEOUT_S + 1
        assert watch.tick(now=still_fine, relayed=2632, consumers=1) is None

    def test_no_frames_is_not_reported_while_nobody_is_attached(self) -> None:
        """With no consumer the relay counts no bytes, so silence proves nothing."""
        watch = _watch()

        reason = watch.tick(
            now=stream_relay.NO_FRAMES_TIMEOUT_S, relayed=0, consumers=0
        )

        assert reason != REASON_NO_FRAMES


class TestSessionGate:
    """The hard limit on how often a Blink liveview may be opened."""

    def test_the_first_session_is_never_delayed(self) -> None:
        """Nothing on record, nothing to wait for."""
        assert _SessionGate().delay(now=0.0) == 0.0

    def test_reopening_respects_a_minimum_interval(self) -> None:
        """Stops a fault from reopening in a tight loop."""
        gate = _SessionGate(min_interval=5.0)
        gate.record(now=0.0)

        assert gate.delay(now=1.0) == pytest.approx(4.0)

    def test_the_interval_elapses(self) -> None:
        """A normal renegotiation is not held up."""
        gate = _SessionGate(min_interval=5.0)
        gate.record(now=0.0)

        assert gate.delay(now=5.0) == 0.0

    def test_the_hourly_ceiling_holds_the_next_session_back(self) -> None:
        """Blink publishes no quota, so this is the only account-level protection."""
        gate = _SessionGate(max_per_window=3, window=100.0, min_interval=0.0)
        for opened_at in (0.0, 10.0, 20.0):
            gate.record(opened_at)

        assert gate.delay(now=30.0) == pytest.approx(70.0)

    def test_the_ceiling_frees_up_as_the_window_slides(self) -> None:
        """It is a rolling window, not a fixed bucket that stays full."""
        gate = _SessionGate(max_per_window=3, window=100.0, min_interval=0.0)
        for opened_at in (0.0, 10.0, 20.0):
            gate.record(opened_at)

        assert gate.delay(now=101.0) == 0.0
        assert gate.opens_in_window == 2

    def test_continuous_viewing_fits_under_the_ceiling(self) -> None:
        """A consumer attached all day renegotiates ~13x/hour; that must not trip."""
        gate = _SessionGate()
        renegotiations = int(3600 / stream_relay.RENEGOTIATE_AFTER_S) + 1

        for index in range(renegotiations):
            now = index * stream_relay.RENEGOTIATE_AFTER_S
            assert gate.delay(now) == 0.0, f"blocked at renegotiation {index}"
            gate.record(now)

    def test_spacing_out_is_not_the_same_as_running_out(self) -> None:
        """The log says different things for each, so the gate must tell them apart."""
        gate = _SessionGate(max_per_window=3, window=100.0, min_interval=5.0)
        gate.record(now=0.0)

        assert gate.delay(now=1.0) > 0
        assert gate.at_capacity is False

    def test_at_capacity_once_the_allowance_is_spent(self) -> None:
        """This is the state worth a WARNING: the camera is about to go dark."""
        gate = _SessionGate(max_per_window=3, window=100.0, min_interval=0.0)
        for opened_at in (0.0, 10.0, 20.0):
            gate.record(opened_at)

        assert gate.delay(now=30.0) > 0
        assert gate.at_capacity is True

    def test_a_reopen_is_spaced_from_the_previous_close(self) -> None:
        """Measured on 2026-08-08: reopening in the same second as the close got
        the replacement session killed by Blink after 2s and 0 bytes, twice out
        of three scheduled renegotiations. The open-side interval cannot catch
        this, because the previous *open* was minutes earlier."""
        gate = _SessionGate(min_interval=5.0, cooldown=5.0)
        gate.record(now=0.0)
        gate.record_close(now=270.0)

        assert gate.delay(now=270.0) == pytest.approx(5.0)

    def test_the_cooldown_expires(self) -> None:
        """It spaces the reopen; it does not block it."""
        gate = _SessionGate(min_interval=5.0, cooldown=5.0)
        gate.record(now=0.0)
        gate.record_close(now=270.0)

        assert gate.delay(now=275.0) == 0.0

    def test_continuous_viewing_fits_under_the_raised_ceiling(self) -> None:
        """Measured: 24.1 opens/hour with a consumer attached the whole time."""
        gate = _SessionGate(cooldown=0.0)
        measured_rate = 25

        for index in range(measured_rate):
            now = index * (3600 / measured_rate)
            assert gate.delay(now) == 0.0, f"throttled at open {index}"
            gate.record(now)
