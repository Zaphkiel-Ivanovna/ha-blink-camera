"""The session cache must never hold a password, and never cross accounts."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ha_blink_camera.blink_client import (
    BlinkClient,
    session_payload,
    write_session_file,
)
from ha_blink_camera.config import Config
from ha_blink_camera.logging_setup import SecretRedactor

_LOGIN_ATTRIBUTES = {
    "username": "me@example.com",
    "password": "hunter2-in-the-clear",
    "token": "access-token-value",
    "refresh_token": "refresh-token-value",
    "expires_in": 3600,
    "expiration_date": 1_800_000_000.0,
    "host": "rest-prod.immedia-semi.com",
    "region_id": "prod",
    "client_id": 123,
    "account_id": 456,
    "user_id": 789,
    "hardware_id": "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
    "uid": "BlinkCamera_deadbeef",
    "device_id": "Blink",
}


def _config(tmp_path: Path, *, username: str = "me@example.com") -> Config:
    """A Config pointing its session cache into `tmp_path`."""
    return Config(
        username=username,
        password="hunter2",
        camera_name=None,
        data_dir=tmp_path,
        config_dir=tmp_path / "config",
    )


def test_password_is_not_in_the_persisted_payload() -> None:
    """The headline guarantee: `blink.save()` would write this to disk; we do not."""
    payload = session_payload(_LOGIN_ATTRIBUTES)

    assert "password" not in payload
    assert "hunter2-in-the-clear" not in json.dumps(payload)


def test_unknown_keys_are_dropped() -> None:
    """An allowlist, so a future blinkpy field cannot leak by simply appearing."""
    payload = session_payload({**_LOGIN_ATTRIBUTES, "surprise_secret": "nope"})

    assert "surprise_secret" not in payload


def test_the_session_is_still_resumable() -> None:
    """Stripping must not remove what a restart needs to skip the password login."""
    payload = session_payload(_LOGIN_ATTRIBUTES)

    assert payload["refresh_token"] == "refresh-token-value"
    assert payload["hardware_id"] == _LOGIN_ATTRIBUTES["hardware_id"]
    assert payload["username"] == "me@example.com"


def test_written_file_is_owner_only(tmp_path: Path) -> None:
    """A live refresh_token at rest must never be group- or world-readable."""
    path = tmp_path / "session.json"

    write_session_file(path, session_payload(_LOGIN_ATTRIBUTES))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    """The write is atomic: a rename, with no leftover token-bearing scratch file."""
    path = tmp_path / "session.json"

    write_session_file(path, session_payload(_LOGIN_ATTRIBUTES))

    assert [p.name for p in tmp_path.iterdir()] == ["session.json"]


def test_rewrite_keeps_the_mode(tmp_path: Path) -> None:
    """Token rotation rewrites this file repeatedly; 0600 must survive every time."""
    path = tmp_path / "session.json"
    write_session_file(path, session_payload(_LOGIN_ATTRIBUTES))
    path.chmod(0o644)

    write_session_file(path, session_payload(_LOGIN_ATTRIBUTES))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writing_a_password_is_refused(tmp_path: Path) -> None:
    """A belt-and-braces guard, in case a caller skips `session_payload()`."""
    with pytest.raises(ValueError, match="password"):
        write_session_file(tmp_path / "session.json", {"password": "hunter2"})


def test_parent_directory_is_created(tmp_path: Path) -> None:
    """A first run on an empty /data must not fail on a missing directory."""
    path = tmp_path / "nested" / "session.json"

    write_session_file(path, session_payload(_LOGIN_ATTRIBUTES))

    assert path.exists()


def _client(config: Config) -> BlinkClient:
    """A client that has not touched the network."""
    return BlinkClient(config, SecretRedactor())


def test_cached_session_is_reused_for_the_same_account(tmp_path: Path) -> None:
    """The whole point of the cache: no password login, no 2FA, on restart."""
    write_session_file(tmp_path / "session.json", session_payload(_LOGIN_ATTRIBUTES))

    login_data = _client(_config(tmp_path))._build_login_data()

    assert login_data["refresh_token"] == "refresh-token-value"
    assert "password" not in login_data


def test_cached_session_of_another_account_is_discarded(tmp_path: Path) -> None:
    """Otherwise the stored refresh_token silently logs the *previous* user in."""
    write_session_file(tmp_path / "session.json", session_payload(_LOGIN_ATTRIBUTES))

    config = _config(tmp_path, username="someone.else@example.com")
    login_data = _client(config)._build_login_data()

    assert "refresh_token" not in login_data
    assert login_data["username"] == "someone.else@example.com"
    assert login_data["password"] == "hunter2"


def test_cache_without_a_username_is_discarded(tmp_path: Path) -> None:
    """Absent must mean "cannot be proven", not "no opinion".

    Blink decides the account from the refresh_token alone — blinkpy never
    cross-checks it against the username. So a cache carrying a token but no
    username would silently authenticate as whoever created it, and stream that
    person's camera into this user's Home Assistant.
    """
    anonymous = session_payload(_LOGIN_ATTRIBUTES)
    del anonymous["username"]
    write_session_file(tmp_path / "session.json", anonymous)

    login_data = _client(_config(tmp_path))._build_login_data()

    assert "refresh_token" not in login_data
    assert login_data["password"] == "hunter2"


def test_cache_with_a_blank_username_is_discarded(tmp_path: Path) -> None:
    """Same reasoning for a present-but-empty value."""
    blank = {**session_payload(_LOGIN_ATTRIBUTES), "username": "   "}
    write_session_file(tmp_path / "session.json", blank)

    login_data = _client(_config(tmp_path))._build_login_data()

    assert "refresh_token" not in login_data


def test_no_stray_cache_keys_survive_a_discard(tmp_path: Path) -> None:
    """A discard must drop the whole cache, not just the token."""
    write_session_file(tmp_path / "session.json", session_payload(_LOGIN_ATTRIBUTES))

    config = _config(tmp_path, username="someone.else@example.com")
    login_data = _client(config)._build_login_data()

    assert set(login_data) == {"username", "password"}


def test_account_match_ignores_case(tmp_path: Path) -> None:
    """Email addresses are not case-sensitive; a re-typed one must still match."""
    write_session_file(tmp_path / "session.json", session_payload(_LOGIN_ATTRIBUTES))

    config = _config(tmp_path, username="Me@Example.COM")
    login_data = _client(config)._build_login_data()

    assert login_data["refresh_token"] == "refresh-token-value"


def test_missing_cache_falls_back_to_the_password(tmp_path: Path) -> None:
    """A first run with no session file still has to be able to log in."""
    login_data = _client(_config(tmp_path))._build_login_data()

    assert login_data["password"] == "hunter2"


def test_corrupt_cache_is_ignored_not_fatal(tmp_path: Path) -> None:
    """A half-written file must degrade to a password login, not crash the add-on."""
    (tmp_path / "session.json").write_text("{truncated", encoding="utf-8")

    login_data = _client(_config(tmp_path))._build_login_data()

    assert login_data["password"] == "hunter2"


def test_tokens_from_the_cache_are_registered_for_redaction(tmp_path: Path) -> None:
    """Loading a cache teaches the log filter about the tokens it just read."""
    write_session_file(tmp_path / "session.json", session_payload(_LOGIN_ATTRIBUTES))
    redactor = SecretRedactor()

    BlinkClient(_config(tmp_path), redactor)._build_login_data()

    assert redactor.redact("token=refresh-token-value") == "token=***"


def _bootstrap_client(tmp_path: Path) -> BlinkClient:
    """A client whose import folder exists and is separate from its data dir."""
    (tmp_path / "config").mkdir(exist_ok=True)
    return _client(_config(tmp_path))


def test_a_dropped_session_is_imported(tmp_path: Path) -> None:
    """The documented first-run path: the data volume is unreachable by a human."""
    client = _bootstrap_client(tmp_path)
    payload = session_payload(_LOGIN_ATTRIBUTES)
    (tmp_path / "config" / "session.json").write_text(json.dumps(payload))

    assert client.adopt_bootstrap_session() is True
    assert json.loads((tmp_path / "session.json").read_text())["refresh_token"]


def test_the_dropped_file_is_removed_after_import(tmp_path: Path) -> None:
    """The config folder is readable by every add-on and by Samba users."""
    client = _bootstrap_client(tmp_path)
    source = tmp_path / "config" / "session.json"
    source.write_text(json.dumps(session_payload(_LOGIN_ATTRIBUTES)))

    client.adopt_bootstrap_session()

    assert not source.exists(), "a live refresh_token must not be left lying about"


def test_the_imported_session_is_owner_only(tmp_path: Path) -> None:
    """It goes through the same atomic 0600 writer as any other session write."""
    client = _bootstrap_client(tmp_path)
    (tmp_path / "config" / "session.json").write_text(
        json.dumps(session_payload(_LOGIN_ATTRIBUTES))
    )

    client.adopt_bootstrap_session()

    assert stat.S_IMODE((tmp_path / "session.json").stat().st_mode) == 0o600


def test_a_dropped_password_is_not_imported(tmp_path: Path) -> None:
    """A file straight from the prototype still carries the plaintext password."""
    client = _bootstrap_client(tmp_path)
    (tmp_path / "config" / "session.json").write_text(json.dumps(_LOGIN_ATTRIBUTES))

    client.adopt_bootstrap_session()

    assert "password" not in json.loads((tmp_path / "session.json").read_text())


def test_no_dropped_file_is_not_an_error(tmp_path: Path) -> None:
    """Every restart after the first takes this path."""
    assert _bootstrap_client(tmp_path).adopt_bootstrap_session() is False


def test_an_unusable_dropped_file_is_refused(tmp_path: Path) -> None:
    """Half-copied or hand-edited files must not replace a working cache."""
    client = _bootstrap_client(tmp_path)
    (tmp_path / "config" / "session.json").write_text("{truncated")

    assert client.adopt_bootstrap_session() is False
    assert not (tmp_path / "session.json").exists()


def test_a_dropped_file_without_a_token_is_refused(tmp_path: Path) -> None:
    """Without a refresh_token the session cannot be resumed at all."""
    client = _bootstrap_client(tmp_path)
    useless = {k: v for k, v in _LOGIN_ATTRIBUTES.items() if k != "refresh_token"}
    (tmp_path / "config" / "session.json").write_text(json.dumps(useless))

    assert client.adopt_bootstrap_session() is False
