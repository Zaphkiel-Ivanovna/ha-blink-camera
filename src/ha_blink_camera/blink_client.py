"""The only module that talks to blinkpy for API calls."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Final, Protocol

from aiohttp import ClientError, ClientSession, ClientTimeout
from blinkpy.auth import Auth, BlinkTwoFARequiredError, UnauthorizedError
from blinkpy.blinkpy import Blink
from blinkpy.helpers.constants import BASE_URL

from .config import Config
from .exceptions import (
    CameraNotFoundError,
    InvalidCredentialsError,
    TransientBlinkError,
    TwoFactorRequiredError,
)
from .logging_setup import SecretRedactor

_LOGGER = logging.getLogger(__name__)

_SESSION_KEYS: Final = (
    "username",
    "token",
    "expires_in",
    "expiration_date",
    "refresh_token",
    "host",
    "region_id",
    "client_id",
    "account_id",
    "user_id",
    "hardware_id",
    "uid",
    "device_id",
)

_SESSION_FILE_MODE: Final = 0o600
_SECRET_SESSION_KEYS: Final = ("token", "refresh_token")
CONNECTIVITY_TIMEOUT_S: Final = 10.0
_FEED_FAILURES: Final = (OSError, RuntimeError, ValueError, UnauthorizedError)


class StreamSink(Protocol):
    """What `BlinkLiveStream.recv()` requires of an entry in `stream.clients`."""

    def write(self, data: bytes) -> None:
        """Queue bytes for the downstream consumer(s)."""

    async def drain(self) -> None:
        """Wait until the queued bytes have been handed to the OS."""

    def is_closing(self) -> bool:
        """Whether this sink no longer wants data."""

    def close(self) -> None:
        """Detach the sink. Called by blinkpy when a session ends."""


class LiveSession:
    """One Blink liveview session: the upstream feed task and its lifetime."""

    def __init__(self, stream: Any, feed: asyncio.Task[None]) -> None:
        """Wrap a started `BlinkLiveStream` and the task running its `feed()`."""
        self._stream = stream
        self._feed = feed

    async def wait(self) -> None:
        """Block until Blink ends the session, or raise why the feed died."""
        try:
            await self._feed
        except _FEED_FAILURES as err:
            raise TransientBlinkError(f"The Blink feed stopped: {err}") from err

    async def aclose(self) -> None:
        """Stop the session and wait for the feed task to unwind, reporting nothing."""
        self._feed.cancel()
        with contextlib.suppress(asyncio.CancelledError, *_FEED_FAILURES):
            await self._feed
        with contextlib.suppress(OSError, AttributeError):
            self._stream.stop()


class BlinkClient:
    """Authenticated access to one Blink account and one of its cameras."""

    def __init__(self, config: Config, redactor: SecretRedactor) -> None:
        """Hold configuration and the redactor; nothing touches the network yet."""
        self._config = config
        self._redactor = redactor
        self._http: ClientSession | None = None
        self._blink: Blink | None = None
        redactor.add(config.password)

    def adopt_bootstrap_session(self) -> bool:
        """Import a session a human dropped in the add-on's config folder.

        This is the documented first-run path: the add-on's data volume is
        private, so the file has to arrive somewhere reachable. It is moved
        rather than copied — leaving a live refresh_token in a folder every
        other add-on and Samba user can read would undo the point of storing
        the cache at 0600.
        """
        source = self._config.bootstrap_path
        try:
            raw = source.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except OSError as err:
            _LOGGER.warning("Cannot read the session to import: %s", err)
            return False

        try:
            payload = session_payload(json.loads(raw))
        except (json.JSONDecodeError, TypeError, AttributeError) as err:
            _LOGGER.error("The session file to import is not usable: %s", err)
            return False

        if not payload.get("refresh_token"):
            _LOGGER.error("The session file to import has no refresh_token.")
            return False

        try:
            write_session_file(self._config.session_path, payload)
            source.unlink()
        except OSError as err:
            _LOGGER.error("Could not import the session file: %s", err)
            return False

        self._register_secrets(payload)
        _LOGGER.info("Imported the session from %s and removed it", source)
        return True

    async def connect(self) -> None:
        """Authenticate, preferring the cached session over a password login."""
        await self.aclose()
        self._http = ClientSession()
        login_data = self._build_login_data()

        blink = Blink(session=self._http)
        blink.auth = Auth(
            login_data,
            no_prompt=True,
            session=self._http,
            callback=self._persist_session,
        )
        self._blink = blink

        started = await self._start(blink)
        self._persist_session()

        if not started or not blink.available:
            await self._raise_login_failure()

        await blink.refresh(force=True)
        _LOGGER.info("Authenticated; %d camera(s) on the account", len(blink.cameras))

    async def _raise_login_failure(self) -> None:
        """Decide whether a failed login was the credentials or the network.

        blinkpy reports both identically — `Blink.start()` swallows `LoginError`
        and returns False, and `Auth.query()` returns None on a connection error
        — so the only way to tell them apart is to probe the cloud ourselves.
        Guessing wrong either idles forever on a blip, or retries bad credentials
        until the account locks.
        """
        if await self._cloud_reachable():
            raise InvalidCredentialsError(
                "Blink rejected these credentials. Check 'username' and "
                "'password' in the add-on options, or re-import the session."
            )
        raise TransientBlinkError("Cannot reach Blink right now.")

    async def _cloud_reachable(self) -> bool:
        """Whether Blink's API answers at all. Any HTTP status counts as reachable."""
        if self._http is None:
            return False
        try:
            timeout = ClientTimeout(total=CONNECTIVITY_TIMEOUT_S)
            async with self._http.get(BASE_URL, timeout=timeout) as response:
                _LOGGER.debug("Connectivity probe answered %d", response.status)
        except (ClientError, TimeoutError, OSError) as err:
            _LOGGER.debug("Connectivity probe failed: %s", err)
            return False
        return True

    async def aclose(self) -> None:
        """Release the HTTP session."""
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def _start(self, blink: Blink) -> bool:
        """Run blinkpy's setup, mapping its one raising failure mode to ours."""
        try:
            return bool(await blink.start())
        except BlinkTwoFARequiredError as err:
            raise TwoFactorRequiredError(
                "Blink wants a two-factor code, which this version cannot ask "
                "for. Authenticate once elsewhere and import the session with "
                "tools/import_session.py."
            ) from err

    def resolve_camera(self) -> tuple[str, Any]:
        """Find the configured camera, or the only one if none is configured."""
        blink = self._require_blink()
        names = list(blink.cameras.keys())
        wanted = self._config.camera_name

        if not wanted:
            if len(names) == 1:
                return names[0], blink.cameras[names[0]]
            raise CameraNotFoundError(
                f"Set 'camera_name' to one of: {', '.join(names) or '(none found)'}"
            )

        with contextlib.suppress(KeyError):
            return wanted, blink.cameras[wanted]
        for name in names:
            if name.strip().casefold() == wanted.strip().casefold():
                return name, blink.cameras[name]

        raise CameraNotFoundError(
            f"No camera named {wanted!r} on this account. "
            f"Available: {', '.join(names) or '(none found)'}"
        )

    async def open_livestream(self, camera: Any, sink: StreamSink) -> LiveSession:
        """Open a liveview and feed it into `sink`, never calling `stream.start()`."""
        stream = await self._init_livestream(camera)
        stream.clients.append(sink)
        feed = asyncio.create_task(stream.feed(), name="blink-feed")
        return LiveSession(stream, feed)

    async def _init_livestream(self, camera: Any) -> Any:
        """Ask Blink for a liveview, classifying its four unguarded failures."""
        try:
            return await camera.init_livestream()
        except NotImplementedError as err:
            raise CameraNotFoundError(
                f"This camera does not offer a relayable liveview: {err}"
            ) from err
        except TypeError as err:
            raise TransientBlinkError(
                "Blink returned no liveview server — throttled or unreachable."
            ) from err
        except KeyError as err:
            raise TransientBlinkError(
                f"Blink refused the liveview: unexpected response ({err})"
            ) from err
        except UnauthorizedError as err:
            raise TransientBlinkError(
                "Blink rejected the session token while opening the liveview."
            ) from err

    def _require_blink(self) -> Blink:
        """Return the connected Blink handle, or fail loudly."""
        if self._blink is None:
            raise TransientBlinkError("Not connected to Blink yet.")
        return self._blink

    def _build_login_data(self) -> dict[str, Any]:
        """Merge the credentials with a cached session that proves the account."""
        cached = self._read_session()
        login_data: dict[str, Any] = {"username": self._config.username}

        if cached is not None:
            cached_user = str(cached.get("username") or "").strip()
            if cached_user.casefold() == self._config.username.casefold():
                login_data = dict(cached)
                login_data["username"] = self._config.username
            else:
                _LOGGER.warning(
                    "Cached session does not prove it belongs to the configured "
                    "account — discarding it and logging in from scratch"
                )

        if not login_data.get("refresh_token") and self._config.has_password:
            login_data["password"] = self._config.password
        return login_data

    def _read_session(self) -> dict[str, Any] | None:
        """Load the session cache, tolerating absence and corruption."""
        path = self._config.session_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as err:
            _LOGGER.warning("Ignoring unreadable session cache: %s", err)
            return None

        if not isinstance(data, dict):
            _LOGGER.warning("Ignoring malformed session cache at %s", path)
            return None
        self._register_secrets(data)
        return data

    def _persist_session(self) -> None:
        """Write the session cache. Registered as `Auth(callback=)`, so it must
        stay synchronous and must never raise into blinkpy."""
        if self._blink is None:
            return
        attributes = self._blink.auth.login_attributes
        if not attributes.get("token"):
            return

        payload = session_payload(attributes)
        self._register_secrets(payload)
        try:
            write_session_file(self._config.session_path, payload)
        except OSError as err:
            _LOGGER.warning("Could not persist the session cache: %s", err)

    def _register_secrets(self, payload: dict[str, Any]) -> None:
        """Teach the log redactor about the tokens in this payload."""
        for key in _SECRET_SESSION_KEYS:
            value = payload.get(key)
            if isinstance(value, str):
                self._redactor.add(value)


def session_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Reduce a blinkpy login-attributes dict to the keys that may be persisted."""
    return {key: data[key] for key in _SESSION_KEYS if key in data}


def write_session_file(path: Path, payload: dict[str, Any]) -> None:
    """Write `payload` as JSON, mode 0600 before the first byte, atomically."""
    if "password" in payload:
        raise ValueError("refusing to persist a password to the session cache")

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, _SESSION_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    """Make the rename durable, best effort — /data is often an SD card."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
