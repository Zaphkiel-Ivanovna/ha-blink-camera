"""Sole reader of the add-on's options file, turning it into a frozen Config."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from .exceptions import NotConfiguredError

DATA_DIR_ENV: Final = "ADDON_DATA_DIR"
DEFAULT_DATA_DIR: Final = Path("/data")

CONFIG_DIR_ENV: Final = "ADDON_CONFIG_DIR"
DEFAULT_CONFIG_DIR: Final = Path("/config")

OPTIONS_FILENAME: Final = "options.json"
SESSION_FILENAME: Final = "session.json"

RELAY_PORT_ENV: Final = "ADDON_RELAY_PORT"


def resolve_data_dir() -> Path:
    """Return the add-on data directory, honouring the development override."""
    override = os.environ.get(DATA_DIR_ENV)
    return Path(override) if override else DEFAULT_DATA_DIR


def resolve_config_dir() -> Path:
    """Return the add-on's user-reachable config directory."""
    override = os.environ.get(CONFIG_DIR_ENV)
    return Path(override) if override else DEFAULT_CONFIG_DIR


def resolve_relay_port(default: int) -> int:
    """Return the port to listen on, honouring the development override."""
    override = os.environ.get(RELAY_PORT_ENV)
    if not override:
        return default
    try:
        return int(override)
    except ValueError as err:
        raise NotConfiguredError(f"{RELAY_PORT_ENV} must be a port number") from err


@dataclass(frozen=True, slots=True)
class Config:
    """The add-on's runtime configuration, read once at startup."""

    username: str
    password: str = field(repr=False)
    camera_name: str | None
    data_dir: Path
    config_dir: Path

    @property
    def session_path(self) -> Path:
        """Where the blinkpy session cache lives."""
        return self.data_dir / SESSION_FILENAME

    @property
    def bootstrap_path(self) -> Path:
        """Where a user drops a session to import on first run.

        The data volume is private to this add-on, so a human cannot put a file
        there. This folder is reachable over Samba, SSH or the file editor.
        """
        return self.config_dir / SESSION_FILENAME

    @property
    def has_password(self) -> bool:
        """Whether a password login is even possible."""
        return bool(self.password)


def load_config(data_dir: Path, config_dir: Path | None = None) -> Config:
    """Read `<data_dir>/options.json` into a Config.

    Every unusable state — missing, unreadable, no username — raises
    `NotConfiguredError`, which the caller turns into one friendly line.
    """
    options = _read_options(data_dir / OPTIONS_FILENAME)

    username = _as_text(options.get("username"))
    if not username:
        raise NotConfiguredError(
            "No Blink account configured yet. Open the add-on's Configuration "
            "tab, fill in 'username' and 'password', and restart."
        )

    camera_name = _as_text(options.get("camera_name"))
    return Config(
        username=username,
        password=_as_text(options.get("password")),
        camera_name=camera_name or None,
        data_dir=data_dir,
        config_dir=config_dir if config_dir is not None else resolve_config_dir(),
    )


def load_config_or_blank(data_dir: Path, config_dir: Path | None = None) -> Config:
    """Like `load_config`, but an unconfigured install yields a blank Config.

    A fresh install has nothing to load and no error worth showing: the setup
    page collects the credentials instead, so there is nothing for the user to
    fix and nothing to idle over.
    """
    try:
        return load_config(data_dir, config_dir)
    except NotConfiguredError:
        return Config(
            username="",
            password="",
            camera_name=None,
            data_dir=data_dir,
            config_dir=config_dir if config_dir is not None else resolve_config_dir(),
        )


def _read_options(path: Path) -> dict[str, Any]:
    """Parse the options file, mapping every failure to NotConfiguredError."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as err:
        raise NotConfiguredError(f"No options file at {path}.") from err
    except OSError as err:
        raise NotConfiguredError(f"Cannot read {path}: {err}") from err

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as err:
        raise NotConfiguredError(f"{path} is not valid JSON: {err}") from err

    if not isinstance(parsed, dict):
        raise NotConfiguredError(f"{path} must contain a JSON object.")
    return parsed


def _as_text(value: object) -> str:
    """Coerce an option to a stripped string; anything unusable becomes empty."""
    return value.strip() if isinstance(value, str) else ""
