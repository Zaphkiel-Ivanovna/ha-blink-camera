"""Reading options.json: every unusable state becomes one friendly error."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ha_blink_camera.config import (
    DATA_DIR_ENV,
    DEFAULT_DATA_DIR,
    OPTIONS_FILENAME,
    RELAY_PORT_ENV,
    SESSION_FILENAME,
    Config,
    load_config,
    resolve_data_dir,
    resolve_relay_port,
)
from ha_blink_camera.exceptions import NotConfiguredError


def _write_options(data_dir: Path, payload: object) -> Path:
    """Drop an options file into `data_dir`, JSON-encoding anything given."""
    path = data_dir / OPTIONS_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_reads_all_three_options(tmp_path: Path) -> None:
    """The happy path: the three v1 options land in a frozen Config."""
    _write_options(
        tmp_path,
        {"username": "me@example.com", "password": "hunter2", "camera_name": "Salon"},
    )

    config = load_config(tmp_path)

    assert config.username == "me@example.com"
    assert config.password == "hunter2"
    assert config.camera_name == "Salon"
    assert config.session_path == tmp_path / SESSION_FILENAME


def test_blank_camera_name_means_pick_the_only_one(tmp_path: Path) -> None:
    """An empty camera_name is None, not "", so the client can auto-select."""
    _write_options(tmp_path, {"username": "me@example.com", "camera_name": "  "})

    assert load_config(tmp_path).camera_name is None


def test_empty_password_is_allowed(tmp_path: Path) -> None:
    """A session-file install has no password, and that is a valid state."""
    config = _config_with(tmp_path, {"username": "me@example.com", "password": ""})

    assert config.has_password is False


def test_whitespace_is_stripped(tmp_path: Path) -> None:
    """Supervisor's text fields keep whatever the user pasted, spaces included."""
    config = _config_with(tmp_path, {"username": "  me@example.com \n"})

    assert config.username == "me@example.com"


def _config_with(tmp_path: Path, payload: dict[str, object]) -> Config:
    """Write `payload` and load it."""
    _write_options(tmp_path, payload)
    return load_config(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"username": ""},
        {"username": "   "},
        {"username": None},
        {"username": 42},
        {"password": "hunter2"},
    ],
    ids=["empty", "blank", "spaces", "null", "wrong-type", "password-only"],
)
def test_missing_username_is_not_configured(tmp_path: Path, payload: object) -> None:
    """A fresh install nobody filled in must be a named state, not a traceback."""
    _write_options(tmp_path, payload)

    with pytest.raises(NotConfiguredError):
        load_config(tmp_path)


def test_missing_file_is_not_configured(tmp_path: Path) -> None:
    """Supervisor has not written options yet, or the volume is empty."""
    with pytest.raises(NotConfiguredError):
        load_config(tmp_path)


def test_invalid_json_is_not_configured(tmp_path: Path) -> None:
    """A truncated or hand-edited file must not crash the add-on."""
    (tmp_path / OPTIONS_FILENAME).write_text("{not json", encoding="utf-8")

    with pytest.raises(NotConfiguredError):
        load_config(tmp_path)


def test_non_object_json_is_not_configured(tmp_path: Path) -> None:
    """Valid JSON that is not an object is still unusable."""
    _write_options(tmp_path, ["username"])

    with pytest.raises(NotConfiguredError):
        load_config(tmp_path)


def test_data_dir_defaults_to_supervisor_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    """In a container, options live at /data — that is Supervisor's contract."""
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)

    assert resolve_data_dir() == DEFAULT_DATA_DIR


def test_data_dir_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """Development runs outside a container need somewhere writable."""
    monkeypatch.setenv(DATA_DIR_ENV, "/tmp/blinkdata")

    assert resolve_data_dir() == Path("/tmp/blinkdata")


def test_relay_port_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside the container nothing sets this, and 8554 must stay the literal."""
    monkeypatch.delenv(RELAY_PORT_ENV, raising=False)

    assert resolve_relay_port(8554) == 8554


def test_relay_port_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev machine may already have something on 8554 — Docker often does."""
    monkeypatch.setenv(RELAY_PORT_ENV, "18554")

    assert resolve_relay_port(8554) == 18554


def test_relay_port_ignores_an_empty_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """An exported-but-empty variable means "unset", not "port zero"."""
    monkeypatch.setenv(RELAY_PORT_ENV, "")

    assert resolve_relay_port(8554) == 8554


def test_relay_port_rejects_nonsense(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must be a named error, not a confusing socket failure later on."""
    monkeypatch.setenv(RELAY_PORT_ENV, "eight-thousand")

    with pytest.raises(NotConfiguredError):
        resolve_relay_port(8554)
