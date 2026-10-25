"""Instance ids that survive a change of shape.

HAP instance ids are positional in HAP-python: ``Accessory.add_service`` hands
each service and then each of its characteristics to the IID manager in turn,
and the default manager simply counts. That is fine until an accessory's shape
changes. Drop one outlet from a three-outlet shower and every service after it
slides down -- measured 2026-09-14:

    3 valves: Valve 16, 24, 32 | TemperatureSensor 40 | HeaterCooler 44
    2 valves: Valve 16, 24     | TemperatureSensor 32 | HeaterCooler 36

A paired controller has those numbers cached. After that reload it asks for
instance id 44, which no longer exists, and the water disappears from the tile;
instance id 32, which it remembers as the third outlet, is now a thermometer.
The specification is blunt about it (HAP R2 7.4.4.4): "Accessories must not
change its service signature once it is paired with a controller."

So kitbash remembers which instance id it gave each service and characteristic,
per config entry, and hands the same one back for the life of the entry. An id
freed by a removed service is never reused -- a controller that still remembers
it gets nothing rather than something else wearing its number. This is the same
approach Home Assistant's own HomeKit bridge takes in
``homeassistant/components/homekit/iidmanager.py``.

**Allocation order is unchanged**, so the first run against an empty store
reproduces exactly the ids the accessory had before, and an accessory whose
shape has not changed stays paired across the upgrade.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from pyhap.characteristic import Characteristic
from pyhap.iid_manager import IIDManager
from pyhap.service import Service

from .const import DOMAIN

IID_STORE_VERSION = 1
IID_STORE_KEY = DOMAIN + ".iids.{entry_id}"


def iid_store_key(entry_id: str) -> str:
    """The store this entry's allocations live in."""
    return IID_STORE_KEY.format(entry_id=entry_id)


class KitbashIIDStorage:
    """The instance ids one config entry has handed out, and what they were for.

    An allocation is keyed by what the thing *is*, never by where it sits: a
    service by its type and the ``unique_id`` kitbash gives it, a characteristic
    by its service's key plus its own type. Reordering, adding or removing
    services therefore leaves every survivor's id alone.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.store: Store[dict[str, Any]] = Store(
            hass, IID_STORE_VERSION, iid_store_key(entry_id)
        )
        self.allocations: dict[str, int] = {}
        # Sorted, so the next id is always one past the highest ever given out.
        self.allocated_iids: list[int] = []

    async def async_initialize(self) -> None:
        """Load what this entry has already handed out."""
        raw = await self.store.async_load()
        if not raw:
            return
        allocations = raw.get("allocations") or {}
        self.allocations = {str(key): int(iid) for key, iid in allocations.items()}
        self.allocated_iids = sorted(self.allocations.values())

    @callback
    def get_or_allocate_iid(
        self,
        service_type: str,
        service_unique_id: str | None,
        char_type: str | None,
        char_unique_id: str | None,
    ) -> int:
        """The id this thing has always had, or the next one never yet used."""
        service_key = f"{service_type}_{service_unique_id or ''}"
        key = f"{service_key}_{char_type or ''}_{char_unique_id or ''}"
        if (iid := self.allocations.get(key)) is not None:
            return iid
        # One past the highest ever allocated, not the highest in use: an id
        # that belonged to a service the user has since removed stays spent.
        iid = self.allocated_iids[-1] + 1 if self.allocated_iids else 1
        self.allocations[key] = iid
        self.allocated_iids.append(iid)
        return iid

    @callback
    def async_save(self) -> None:
        """Persist, after a delay, the way Home Assistant's own stores do."""
        self.store.async_delay_save(self._data_to_save, 5)

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {"allocations": self.allocations}

    async def async_remove(self) -> None:
        """Entry deleted: forget its numbers with it."""
        await self.store.async_remove()


class KitbashIIDManager(IIDManager):
    """HAP-python's IID manager, answering from the entry's own store."""

    def __init__(self, storage: KitbashIIDStorage) -> None:
        super().__init__()
        self.storage = storage

    def get_iid_for_obj(self, obj: Service | Characteristic) -> int:
        """Look the object up by what it is rather than by when it arrived."""
        if isinstance(obj, Characteristic):
            service: Service = obj.service
            return self.storage.get_or_allocate_iid(
                service.type_id, service.unique_id, obj.type_id, obj.unique_id
            )
        return self.storage.get_or_allocate_iid(obj.type_id, obj.unique_id, None, None)
