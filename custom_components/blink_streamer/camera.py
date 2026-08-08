"""The camera entity fed by the Blink Camera Streamer add-on."""

from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_STREAM_URL, DOMAIN, MANUFACTURER


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the single camera this entry describes."""
    async_add_entities([BlinkStreamerCamera(entry)])


class BlinkStreamerCamera(Camera):
    """A camera whose frames come from the add-on's re-broadcast."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: ConfigEntry) -> None:
        """Bind the entity to one configured stream."""
        super().__init__()
        self._entry = entry
        self._stream_url: str = entry.data[CONF_STREAM_URL]
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.data.get(CONF_NAME) or entry.title,
            manufacturer=MANUFACTURER,
            model="Blink liveview relay",
            configuration_url="https://github.com/Zaphkiel-Ivanovna/ha-blink-camera",
        )

    async def stream_source(self) -> str:
        """Where Home Assistant should pull the live stream from."""
        return self._stream_url
