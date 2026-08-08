"""The exception hierarchy must stay splittable into retry versus never-retry."""

from __future__ import annotations

import pytest

from ha_blink_camera.exceptions import (
    BlinkCameraError,
    CameraNotFoundError,
    FatalConfigError,
    InvalidCredentialsError,
    NotConfiguredError,
    RelayError,
    TransientBlinkError,
    TwoFactorRequiredError,
)

_FATAL = (
    NotConfiguredError,
    InvalidCredentialsError,
    TwoFactorRequiredError,
    CameraNotFoundError,
)


@pytest.mark.parametrize("error", _FATAL)
def test_fatal_errors_are_fatal(error: type[Exception]) -> None:
    """Every never-retry error is catchable as FatalConfigError."""
    assert issubclass(error, FatalConfigError)


@pytest.mark.parametrize("error", _FATAL)
def test_fatal_errors_are_not_transient(error: type[Exception]) -> None:
    """A retry loop catching TransientBlinkError must never swallow a fatal one."""
    assert not issubclass(error, TransientBlinkError)


def test_every_error_shares_one_root() -> None:
    """cli.py routes on BlinkCameraError; nothing may escape that hierarchy."""
    for error in (*_FATAL, TransientBlinkError, FatalConfigError, RelayError):
        assert issubclass(error, BlinkCameraError)


def test_transient_and_fatal_are_disjoint() -> None:
    """Neither branch may be a subclass of the other, or classification breaks."""
    assert not issubclass(TransientBlinkError, FatalConfigError)
    assert not issubclass(FatalConfigError, TransientBlinkError)
