"""HA Store for source-bound last readings and deferred settings."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN


class RecoveryStorage:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.store: Store[dict[str, Any]] = Store(hass, 1, f"{DOMAIN}.recovery.{entry_id}")
        self.binding: dict[str, Any] = {}
        self.command_binding: dict[str, Any] = {}
        self.values: dict[str, Any] = {}
        self.pending: list[dict[str, Any]] = []

    async def async_initialize(self, binding: dict[str, Any], command_binding: dict[str, Any] | None = None) -> None:
        self.binding = deepcopy(binding)
        self.command_binding = deepcopy(command_binding or {})
        raw = await self.store.async_load()
        if isinstance(raw, dict) and raw.get("binding") == self.binding:
            self.values = raw.get("values") if isinstance(raw.get("values"), dict) else {}
            if raw.get("command_binding") == self.command_binding:
                self.pending = raw.get("pending") if isinstance(raw.get("pending"), list) else []

    def update(self, values: dict, pending: list) -> None:
        if values == self.values and pending == self.pending:
            return
        self.values, self.pending = deepcopy(values), deepcopy(pending)
        self.store.async_delay_save(self._data, 1)

    def _data(self) -> dict[str, Any]:
        return {"binding": self.binding, "command_binding": self.command_binding,
                "values": self.values, "pending": self.pending}

    async def async_flush(self) -> None:
        await self.store.async_save(self._data())

    async def async_remove(self) -> None:
        await self.store.async_remove()
