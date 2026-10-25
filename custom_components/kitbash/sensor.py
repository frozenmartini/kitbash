"""One diagnostic sensor per accessory: the last write HomeKit sent.

Made for finding out what the Home app really sends -- the raw value in the
request and the value HAP-python stored after snapping to the characteristic's
step -- without digging through debug logs.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import KitbashConfigEntry
from .const import DOMAIN, SIGNAL_WRITE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: KitbashConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the last-write sensor."""
    async_add_entities([LastHomeKitWriteSensor(entry)])


class LastHomeKitWriteSensor(SensorEntity):
    """State: a one-line summary of the last write; attributes: the full detail."""

    _attr_has_entity_name = True
    _attr_name = "Last HomeKit write"
    _attr_icon = "mdi:home-import-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: KitbashConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}-last_homekit_write"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._attr_native_value: str | None = None
        self._attr_extra_state_attributes: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_WRITE.format(entry_id=self._entry.entry_id), self._async_write
            )
        )

    @callback
    def _async_write(self, payload: dict[str, Any]) -> None:
        raw: dict[str, Any] = payload["raw"]
        summary = ", ".join(f"{name}={value!r}" for name, value in raw.items())
        prefix = f"{payload['accessory']} / " if payload.get("accessory") else ""
        self._attr_native_value = f"{prefix}{payload['service']}: {summary}"[:255]
        self._attr_extra_state_attributes = payload
        self.async_write_ha_state()
