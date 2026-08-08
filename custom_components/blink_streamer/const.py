"""Constants and URL handling shared across the integration."""

from typing import Final
from urllib.parse import urlparse

DOMAIN: Final = "blink_streamer"

CONF_STREAM_URL: Final = "stream_url"

DEFAULT_PORT: Final = 9554
DEFAULT_NAME: Final = "Blink camera"

MANUFACTURER: Final = "Blink Camera Streamer"

SUPPORTED_SCHEMES: Final = ("rtsp", "rtsps", "tcp", "http", "https")
_DEFAULT_PORTS: Final = {"rtsp": 554, "rtsps": 322, "http": 80, "https": 443}


def normalise(url: str) -> str:
    """Reduce a stream URL to a canonical form.

    Two entries differing only in case or an explicit default port are the same
    stream, and the unique id is what stops them being added twice.
    """
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        port = None
    netloc = f"{host}:{port}" if port else host
    return f"{scheme}://{netloc}{parsed.path.rstrip('/')}"
