"""Persist a fan's last observed direction and any deferred HomeKit choice."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN


class FanDirectionStorage:
    """Settings belong to the bound entities and their direction mapping."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.store: Store[dict[str, Any]] = Store(
            hass, 1, f"{DOMAIN}.fan_direction.{entry_id}"
        )
        self.binding: dict[str, Any] = {}
        self.confirmed: int | None = None
        self.pending: int | None = None

    async def async_initialize(self, binding: dict[str, Any]) -> None:
        self.binding = binding
        raw = await self.store.async_load()
        if not isinstance(raw, dict) or raw.get("binding") != binding:
            return
        for name in ("confirmed", "pending"):
            value = raw.get(name)
            if type(value) is int and value in (0, 1):
                setattr(self, name, value)

    def update(self, confirmed: int | None, pending: int | None) -> None:
        if (confirmed, pending) == (self.confirmed, self.pending):
            return
        self.confirmed, self.pending = confirmed, pending
        self.store.async_delay_save(self._data, 1)

    def _data(self) -> dict[str, Any]:
        return {"binding": self.binding, "confirmed": self.confirmed, "pending": self.pending}

    async def async_flush(self) -> None:
        await self.store.async_save(self._data())

    async def async_remove(self) -> None:
        await self.store.async_remove()
