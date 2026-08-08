"""The single exception hierarchy of the add-on, split transient versus fatal."""

__all__ = [
    "BlinkCameraError",
    "CameraNotFoundError",
    "FatalConfigError",
    "InvalidCredentialsError",
    "NotConfiguredError",
    "RelayError",
    "TransientBlinkError",
    "TwoFactorRequiredError",
]


class BlinkCameraError(Exception):
    """Root of every error this add-on raises on purpose."""


class TransientBlinkError(BlinkCameraError):
    """A failure that is expected to clear on its own — retry with backoff.

    Network blips, a Blink session reaching its ~5-6 minute expiry, a throttled
    endpoint. Never fatal: the add-on keeps running and tries again.
    """


class FatalConfigError(BlinkCameraError):
    """A failure no amount of retrying can fix — a human must change something.

    The add-on logs one actionable line and idles. It never exits (S6 would
    respawn it into a login-endpoint hammering loop) and never retries (that
    risks an account lockout).
    """


class NotConfiguredError(FatalConfigError):
    """Required options are missing — a fresh install nobody has filled in yet."""


class InvalidCredentialsError(FatalConfigError):
    """Blink rejected the credentials, and connectivity was proven beforehand."""


class TwoFactorRequiredError(FatalConfigError):
    """Blink wants a 2FA code, which this version cannot obtain on its own.

    Fatal for v0.1, whose only bootstrap path is an imported session file.
    Becomes recoverable in v1.0, once the Ingress form can feed a code to the
    live process (see PLAN.md, M5).
    """


class CameraNotFoundError(FatalConfigError):
    """The configured camera name matches nothing on the account."""


class RelayError(BlinkCameraError):
    """The local TCP re-broadcast side failed, e.g. the port is already taken."""
