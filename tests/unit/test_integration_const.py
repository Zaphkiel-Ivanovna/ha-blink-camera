"""URL canonicalisation for the companion integration's unique id.

Only `const.py` is exercised here: the rest of the integration imports Home
Assistant, which is not — and should not be — a dependency of the add-on's
environment. `hassfest` and the HACS action cover the parts that need it.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import ModuleType

import pytest

_CONST = (
    pathlib.Path(__file__).resolve().parents[2]
    / "custom_components"
    / "blink_streamer"
    / "const.py"
)


def _load() -> ModuleType:
    """Import const.py directly, without the integration package around it."""
    spec = importlib.util.spec_from_file_location("blink_streamer_const", _CONST)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


const = _load()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("rtsp://Host.local:8554/blink", "rtsp://host.local:8554/blink"),
        ("RTSP://host.local:8554/blink", "rtsp://host.local:8554/blink"),
        ("rtsp://host.local:554/blink", "rtsp://host.local/blink"),
        ("rtsp://host.local/blink/", "rtsp://host.local/blink"),
        ("  rtsp://host.local/blink  ", "rtsp://host.local/blink"),
        ("tcp://192.168.1.10:9554", "tcp://192.168.1.10:9554"),
        ("https://host.local:443/snap", "https://host.local/snap"),
    ],
    ids=[
        "case-host",
        "case-scheme",
        "default-port",
        "trailing-slash",
        "whitespace",
        "explicit-port-kept",
        "https-default",
    ],
)
def test_equivalent_urls_reduce_to_one_form(raw: str, expected: str) -> None:
    """The unique id is what stops the same stream being added twice."""
    assert const.normalise(raw) == expected


def test_different_streams_stay_different() -> None:
    """Canonicalising must not collapse genuinely distinct streams."""
    a = const.normalise("rtsp://host.local:8554/one")
    b = const.normalise("rtsp://host.local:8554/two")

    assert a != b


def test_a_non_default_port_is_preserved() -> None:
    """9554 is the add-on's published port and is not any scheme's default."""
    assert "9554" in const.normalise("tcp://host.local:9554")


def test_every_documented_scheme_is_accepted() -> None:
    """The config flow rejects anything outside this list, so it must be right."""
    assert set(const.SUPPORTED_SCHEMES) == {"rtsp", "rtsps", "tcp", "http", "https"}
