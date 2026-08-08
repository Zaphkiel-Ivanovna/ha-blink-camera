"""Config flow for the Blink Camera Streamer companion integration."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME

from .const import CONF_STREAM_URL, DEFAULT_NAME, DOMAIN

_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
        vol.Required(CONF_STREAM_URL): str,
    }
)

_SUPPORTED_SCHEMES = ("rtsp", "rtsps", "tcp", "http", "https")


class BlinkStreamerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the stream the add-on is re-broadcasting."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect a stream URL and create the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_STREAM_URL].strip()
            parsed = urlparse(url)
            if parsed.scheme not in _SUPPORTED_SCHEMES or not parsed.netloc:
                errors[CONF_STREAM_URL] = "invalid_url"
            else:
                await self.async_set_unique_id(url)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_NAME],
                    data={CONF_NAME: user_input[CONF_NAME], CONF_STREAM_URL: url},
                )

        return self.async_show_form(step_id="user", data_schema=_SCHEMA, errors=errors)
