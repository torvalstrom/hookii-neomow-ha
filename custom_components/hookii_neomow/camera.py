"""Camera entity for Hookii Neomow — shows the last on-demand snapshot.

The mower only produces a photo when asked (REST capture), so this entity does
NOT poll the cloud; it serves the most recent snapshot captured by the
"Camera snapshot" button. Before the first capture (or after a restart, since the
snapshot is held in memory) it serves a "No snapshot yet" placeholder so the
dashboard card shows a clean image instead of a broken/spinning 500.
"""
from __future__ import annotations

from io import BytesIO

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import NeomowCoordinator
from .entity import NeomowEntity


def _make_placeholder() -> bytes | None:
    """A small 'No snapshot yet' JPEG, so an empty camera renders cleanly instead
    of returning None (which HA serves as a 500). Generated once at import."""
    try:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (640, 360), (28, 28, 32))
        ImageDraw.Draw(img).text((248, 172), "No snapshot yet", fill=(140, 140, 150))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=70)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - never let a placeholder failure break setup
        return None


_PLACEHOLDER = _make_placeholder()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: NeomowCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(NeomowCamera(coordinator, label) for label in coordinator.mowers)


class NeomowCamera(NeomowEntity, Camera):
    _attr_translation_key = "snapshot"
    _attr_content_type = "image/jpeg"

    def __init__(self, coordinator: NeomowCoordinator, label: str) -> None:
        Camera.__init__(self)
        NeomowEntity.__init__(self, coordinator, label)
        self._attr_unique_id = f"{self._state.serial}_camera"

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return self._state.snapshot or _PLACEHOLDER
