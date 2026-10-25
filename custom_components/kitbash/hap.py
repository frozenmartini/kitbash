"""The HomeKit side: one standalone accessory per config entry, on Kitbash's own
HAP-python driver.

Why not Home Assistant's HomeKit bridge: its ``get_accessory`` maps exactly one
entity to one accessory, with no way to put several entities' services into one.
Running our own ``AccessoryDriver`` gives full control of the accessory shape.
Each entry is published as a *standalone* accessory (AID 1), not bridged, so it
pairs on its own and Home shows it as one device.

Threading follows Home Assistant's own HomeKit integration: the driver runs on
``hass.loop``; ``run``/``stop`` and the driver's pair/unpair hooks are marked as
HAP-python callbacks so they execute in the loop instead of an executor thread;
characteristic and service setters are invoked by HAP-python in the loop.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any
from uuid import UUID

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.service import Service
from pyhap.const import (
    CATEGORY_FAN,
    HAP_PERMISSION_NOTIFY,
    HAP_REPR_ACCS,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_STATUS,
    HAP_REPR_VALUE,
    HAP_SERVER_STATUS,
    STANDALONE_AID,
)
from pyhap.util import callback as pyhap_callback

from homeassistant.components import persistent_notification
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MANUFACTURER,
    MAX_NAME_LENGTH,
    MODEL_CEILING_FAN,
    SIGNAL_WRITE,
    VERSION,
)
from .iids import KitbashIIDManager
from .controls import SCALAR_DOMAINS, ScalarControl
from .assembly import mapping_for
from .recovery import SourceRecovery
from .recovery_bindings import configure_recovery
from .const import (
    CONF_VALUE_MAPPINGS, CONF_POWER, CONF_SPEED, CONF_DIRECTION, CONF_OSCILLATE,
    CONF_MODE, CONF_LIGHT,
    CONF_TEMPERATURE,
)
from .trace import Tracer
from .model import (
    NUMBER_DOMAINS,
    SELECT_DOMAIN,
    AssembledFan,
    OscillateSource,
    direction_from_homekit,
    direction_to_homekit,
    homekit_to_speed,
    level_to_percentage,
    oscillate_from_homekit,
    oscillate_to_homekit,
    percentage_to_level,
    speed_to_homekit,
    temperature_to_celsius,
)

_LOGGER = logging.getLogger(__name__)

# Alexa normally refreshes every minute. Allow a fresh poll after power returns.
DIRECTION_RECOVERY_SECONDS = 90

# HAP-python loader names (pyhap/resources/services.json, characteristics.json).
SERV_FANV2 = "Fanv2"
SERV_LIGHTBULB = "Lightbulb"
SERV_TEMPERATURE_SENSOR = "TemperatureSensor"
SERV_SWITCH = "Switch"
CHAR_ON = "On"
CHAR_NAME = "Name"
CHAR_CONFIGURED_NAME = "ConfiguredName"
CHAR_ACTIVE = "Active"
CHAR_TARGET_FAN_STATE = "TargetFanState"
CHAR_ROTATION_SPEED = "RotationSpeed"
CHAR_ROTATION_DIRECTION = "RotationDirection"
CHAR_SWING_MODE = "SwingMode"
CHAR_BRIGHTNESS = "Brightness"
CHAR_CURRENT_TEMPERATURE = "CurrentTemperature"
# CurrentTemperature is Celsius by protocol; widen HAP-python's default range
# the way Home Assistant's bridge does.
PROP_CELSIUS = {"minValue": -273, "maxValue": 999}

# HAP enum values.
ACTIVE_INACTIVE, ACTIVE_ACTIVE = 0, 1
TARGET_MANUAL, TARGET_AUTO = 0, 1
DIRECTION_CLOCKWISE, DIRECTION_COUNTER = 0, 1

# Home Assistant fan/light vocabulary (kept local: no import of those platforms).
FAN_DOMAIN = "fan"
LIGHT_DOMAIN = "light"
ATTR_PERCENTAGE = "percentage"
ATTR_PRESET_MODE = "preset_mode"
ATTR_OSCILLATING = "oscillating"
ATTR_DIRECTION = "direction"
ATTR_BRIGHTNESS = "brightness"
ATTR_BRIGHTNESS_PCT = "brightness_pct"
ATTR_UNIT_OF_MEASUREMENT = "unit_of_measurement"
# works_with_alexa leaves the unit off when Alexa reports a bare "Degrees" and
# puts the scale here instead; without this a Fahrenheit reading would be taken
# for Celsius. Same class of bug as the shower setpoint (2026-09-14).
ATTR_REPORTED_UNIT = "reported_unit"
SERVICE_SET_PERCENTAGE = "set_percentage"
SERVICE_SET_PRESET_MODE = "set_preset_mode"
SERVICE_OSCILLATE = "oscillate"
SERVICE_SET_DIRECTION = "set_direction"

NOTIFICATION_ID = f"{DOMAIN}_pairing_{{entry_id}}"


@callback
def _temperature_unit(state: State) -> str | None:
    """The scale a temperature entity reports in, or None if it says nothing."""
    return state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or state.attributes.get(
        ATTR_REPORTED_UNIT
    )


# --- Pairing notification ---------------------------------------------------------
@callback
def async_show_setup_message(
    hass: HomeAssistant, entry_id: str, name: str, pincode: bytes, uri: str
) -> None:
    """Tell the user how to pair. The pin is all the Home app needs.

    No QR code yet: Home Assistant's bridge serves its QR through an HTTP view
    and the notification markdown cannot embed one inline. In the Home app use
    Add Accessory, "More options…", pick the accessory, then enter the code.
    """
    pin = pincode.decode()
    _LOGGER.info("Kitbash accessory %s is unpaired; setup code %s", name, pin)
    message = (
        f"**{name}** is ready to pair. In the Home app choose Add Accessory, "
        "then *More options…*, select it from the list and enter this code:\n"
        f"### {pin}\n"
        f"Setup URI (for apps that take one): `{uri}`"
    )
    persistent_notification.async_create(
        hass, message, "Kitbash HomeKit pairing", NOTIFICATION_ID.format(entry_id=entry_id)
    )


@callback
def async_dismiss_setup_message(hass: HomeAssistant, entry_id: str) -> None:
    """Remove the pairing notification."""
    persistent_notification.async_dismiss(
        hass, NOTIFICATION_ID.format(entry_id=entry_id)
    )


# --- Driver ----------------------------------------------------------------------
class KitbashDriver(AccessoryDriver):
    """AccessoryDriver that shows/dismisses the pairing notification."""

    def __init__(
        self, hass: HomeAssistant, entry_id: str, title: str, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.hass = hass
        self.entry_id = entry_id
        self.title = title

    @pyhap_callback
    def pair(
        self, client_username_bytes: bytes, client_public: str, client_permissions: int
    ) -> bool:
        """Dismiss the setup message once a controller pairs."""
        success = super().pair(client_username_bytes, client_public, client_permissions)
        if success:
            async_dismiss_setup_message(self.hass, self.entry_id)
        return bool(success)

    @pyhap_callback
    def unpair(self, client_uuid: UUID) -> None:
        """Show the setup message again once the last controller unpairs."""
        super().unpair(client_uuid)
        if self.state.paired:
            return
        async_show_setup_message(
            self.hass,
            self.entry_id,
            self.title,
            self.state.pincode,
            self.accessory.xhm_uri(),
        )

    # -- the wire as a whole --
    # Each accessory traces its own writes and pushes. These three see what the
    # controller itself does -- fetch the database, read, subscribe, write --
    # which is the only way to tell "not published" from "not drawn", and the
    # reason the dummy shower can answer questions the real one cannot.
    @property
    def _trace(self) -> Tracer | None:
        trace = getattr(self.accessory, "trace", None)
        return trace if trace is not None and trace.enabled else None

    def _name_of(self, iid: Any) -> str:
        """``Valve.Active`` for an instance id, so the trace reads without a lookup."""
        obj = self.accessory.iid_manager.get_obj(iid) if self.accessory else None
        if isinstance(obj, Characteristic):
            service = getattr(obj.service, "display_name", None)
            return f"{service}.{obj.display_name}" if service else obj.display_name
        return getattr(obj, "display_name", None) or f"iid {iid}"

    def get_accessories(self, include_value: bool = True) -> dict[str, Any]:
        """GET /accessories: the controller (re)fetching the whole database.

        Called from two places in HAP-python and only one of them is a
        controller. ``async_start`` calls it with ``include_value=False`` to
        hash the database and decide whether to bump the config number; the
        HTTP handler calls it with the default. Tracing both put eleven
        "HK fetched" lines in the log at every publish that no phone had asked
        for (measured 2026-09-14), which is the same trap as reading a
        subscription as a read. Only the real fetch is traced.
        """
        response = super().get_accessories(include_value)
        if include_value and (trace := self._trace) is not None:
            services = sum(
                len(acc.get("services", ())) for acc in response.get(HAP_REPR_ACCS, ())
            )
            trace("HK fetched the accessory database: %s services", services)
        return response

    def get_characteristics(self, char_ids: list[str]) -> dict[str, Any]:
        """GET /characteristics: what the controller asked for and what it got."""
        response = super().get_characteristics(char_ids)
        # A standalone accessory bypasses HAP-python's availability check.
        # Report a part's failure on its own characteristic, preserving power.
        for rep in response.get(HAP_REPR_CHARS, ()):
            if self.accessory.characteristic_fault(rep.get(HAP_REPR_IID)):
                rep.pop(HAP_REPR_VALUE, None)
                rep[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
        if (trace := self._trace) is not None:
            parts = []
            for rep in response.get(HAP_REPR_CHARS, ()):
                name = self._name_of(rep.get(HAP_REPR_IID))
                if HAP_REPR_VALUE in rep:
                    parts.append(f"{name}={rep[HAP_REPR_VALUE]!r}")
                else:
                    parts.append(f"{name} status {rep.get(HAP_REPR_STATUS)}")
            trace("HK read %s", ", ".join(parts))
        return response

    def set_characteristics(self, chars_query: dict[str, Any], client_addr: Any) -> Any:
        """PUT /characteristics: writes and event subscriptions, exactly as sent."""
        if (trace := self._trace) is not None:
            parts = []
            for query in chars_query.get(HAP_REPR_CHARS, ()):
                text = self._name_of(query.get(HAP_REPR_IID))
                if HAP_REPR_VALUE in query:
                    text += f"={query[HAP_REPR_VALUE]!r}"
                if HAP_PERMISSION_NOTIFY in query:
                    text += " events " + ("on" if query[HAP_PERMISSION_NOTIFY] else "off")
                parts.append(text)
            # The address tells a phone from a home hub, which is the only way
            # to know whether two identical writes a second apart were one
            # finger or two devices (2026-09-14: two Faucet.Active=1, unknown).
            who = client_addr[0] if isinstance(client_addr, tuple) and client_addr else "?"
            trace("HK put from %s: %s", who, "; ".join(parts))
        queries = chars_query.get(HAP_REPR_CHARS, ())
        rejected = [query for query in queries if HAP_REPR_VALUE in query
                    and query.get(HAP_REPR_AID) == STANDALONE_AID
                    and self.accessory.write_unavailable(query.get(HAP_REPR_IID), query[HAP_REPR_VALUE])]
        if not rejected:
            return super().set_characteristics(chars_query, client_addr)
        # Reject before HAP-python updates values or runs service setters.
        # Event subscriptions in a rejected query are still honored.
        self._notify([{key: value for key, value in query.items() if key != HAP_REPR_VALUE}
                      for query in rejected if HAP_PERMISSION_NOTIFY in query], client_addr)
        filtered = [query for query in queries if query not in rejected]
        response = super().set_characteristics({**chars_query, HAP_REPR_CHARS: filtered}, client_addr)
        results = {rep[HAP_REPR_IID]: rep for rep in (response or {}).get(HAP_REPR_CHARS, ())}
        return {HAP_REPR_CHARS: [
            {HAP_REPR_AID: query[HAP_REPR_AID], HAP_REPR_IID: query[HAP_REPR_IID],
             HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE}
            if query in rejected else results.get(query[HAP_REPR_IID], {
                HAP_REPR_AID: query[HAP_REPR_AID], HAP_REPR_IID: query[HAP_REPR_IID],
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS})
            for query in queries if HAP_REPR_VALUE in query
        ]}


# --- Accessory base ------------------------------------------------------------------
class KitbashAccessory(Accessory):
    """A standalone accessory fed by one or more Home Assistant entities.

    Subclasses add their services in ``__init__``, then call ``_seed()``; they
    implement ``_update_from_state`` (HA -> HomeKit) and use ``_call`` for
    HomeKit -> HA writes.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        driver: KitbashDriver,
        name: str,
        entry_id: str,
        model: str,
        category: int,
        entity_ids: tuple[str, ...],
        trace: Tracer | None = None,
    ) -> None:
        # Instance ids come from the entry's own store when there is one, so
        # they survive a change of shape -- see iids.py. The driver carries it
        # rather than every subclass taking another argument.
        storage = getattr(driver, "iid_storage", None)
        super().__init__(
            driver,
            name,
            aid=STANDALONE_AID,
            iid_manager=KitbashIIDManager(storage) if storage is not None else None,
        )
        self.trace = trace or Tracer(name, None)
        self.hass = hass
        self._options = getattr(driver, "kitbash_options", {})
        self.category = category
        self.set_info_service(
            manufacturer=MANUFACTURER,
            model=model,
            serial_number=entry_id,
            firmware_revision=VERSION,
        )
        self.entry_id = entry_id
        self._entity_ids = entity_ids
        self._reachable: set[str] = set()
        self._unsub: CALLBACK_TYPE | None = None
        self._last_states: dict[str, State] = {}
        self._recovery = SourceRecovery(
            self, async_call_later, getattr(driver, "kitbash_recovery_storage", None)
        )
        self._recovery_initialized = False

    @callback
    def _record_write(self, service: Service, raw: dict[str, Any]) -> None:
        """Publish one HomeKit write to the diagnostic sensor.

        ``raw`` is exactly what the controller sent (HAP-python hands the
        service setter the request values); ``stored`` is what HAP-python kept
        after snapping to minStep and clamping, i.e. the characteristic's value.
        """
        stored = {
            char.display_name: char.value
            for char in service.characteristics
            if char.display_name in raw
        }
        _LOGGER.debug("%s: HomeKit wrote %s -> stored %s", self.display_name, raw, stored)
        self.trace("HK write %s: raw=%s stored=%s", service.display_name, raw, stored)
        async_dispatcher_send(
            self.hass,
            SIGNAL_WRITE.format(entry_id=self.entry_id),
            {
                "service": service.display_name,
                "raw": dict(raw),
                "stored": stored,
                "received_at": dt_util.utcnow().isoformat(),
            },
        )

    def _seed(self) -> None:
        """Load current HA state so the first GET after start is right."""
        if hasattr(self, "_recovery") and not self._recovery_initialized:
            configure_recovery(self)
            self._recovery.restore()
            self._recovery_initialized = True
        for entity_id in self._entity_ids:
            self._apply_state(entity_id, self.hass.states.get(entity_id))

    # -- lifecycle (called by the driver) --
    @pyhap_callback
    @callback
    def run(self) -> None:
        """Driver started: subscribe to the source entities."""
        self._seed()
        self._unsub = async_track_state_change_event(
            self.hass, list(self._entity_ids), self._async_state_changed
        )

    async def stop(self) -> None:
        """Driver stopping: drop the subscription."""
        if self._unsub:
            self._unsub()
            self._unsub = None
        await self._recovery.stop()

    def setup_message(self) -> None:
        """Silence HAP-python's stdout QR code; the notification covers it."""

    @property
    def available(self) -> bool:
        """The accessory answers while any of its sources does."""
        return bool(self._reachable)

    def characteristic_fault(self, iid: int | None) -> bool:
        """Whether a specific characteristic cannot currently be read."""
        return self._recovery.fault(self.iid_manager.get_obj(iid))

    def write_unavailable(self, iid: int | None, value: Any) -> bool:
        return self._recovery.write_unavailable(self.iid_manager.get_obj(iid), value)

    def _source_state(self, entity_id: str) -> State | None:
        """Cached states feed derived displays; write readiness uses live state."""
        state = self.hass.states.get(entity_id)
        if state is not None and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return state
        return self._last_states.get(entity_id)

    # -- HA -> HomeKit --
    @callback
    def _async_state_changed(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data["new_state"]
        if self.trace.enabled:
            self._trace_entity_change(entity_id, event.data["old_state"], new_state)
        self._apply_state(entity_id, new_state)

    def _trace_entity_change(
        self, entity_id: str, old: State | None, new: State | None
    ) -> None:
        """Log what changed on a source entity, or that nothing did."""
        if new is None:
            self.trace("entity %s: removed (was %s)", entity_id, old.state if old else None)
            return
        if old is None:
            self.trace("entity %s: appeared as %s %s", entity_id, new.state, dict(new.attributes))
            return
        changed_attrs = {
            key: (old.attributes.get(key), new.attributes.get(key))
            for key in set(old.attributes) | set(new.attributes)
            if old.attributes.get(key) != new.attributes.get(key)
        }
        if old.state == new.state and not changed_attrs:
            self.trace("entity %s: unchanged (%s)", entity_id, new.state)
            return
        self.trace(
            "entity %s: %s -> %s attrs %s", entity_id, old.state, new.state, changed_attrs
        )

    @callback
    def _push(self, char: Characteristic | None, value: Any, source: str) -> None:
        """Push a value into a characteristic, tracing old -> new.

        ``None`` is accepted and does nothing: which services an accessory
        publishes depends on its options, so a caller that has a value to
        publish should not also have to know whether there is anywhere to put it.

        Named ``Service.Characteristic``: a valve system has an ``Active`` on
        the Faucet and one per Valve, and a trace that says only "Active"
        cannot be read at all (2026-09-14).
        """
        if char is None:
            return
        recovery = getattr(self, "_recovery", None)
        if recovery is not None and not recovery.accepts_push(char, source):
            return
        old = char.value
        char.set_value(value)
        if recovery is not None:
            recovery.remember(char)
        service = getattr(char.service, "display_name", None)
        name = f"{service}.{char.display_name}" if service else char.display_name
        if char.value == old:
            self.trace("push %s unchanged at %r <- %s", name, old, source)
        else:
            self.trace("push %s %r -> %r <- %s", name, old, char.value, source)

    @callback
    def _apply_state(self, entity_id: str, state: State | None) -> None:
        recovery = getattr(self, "_recovery", None)
        if (state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                or recovery is not None and not recovery.live_available(entity_id)):
            self._reachable.discard(entity_id)
            if recovery is not None:
                recovery.sync()
            return
        self._reachable.add(entity_id)
        if recovery is not None:
            previous = self._last_states.get(entity_id)
            if previous is None or (previous.state, previous.attributes) != (state.state, state.attributes):
                recovery.failed_entities.discard(entity_id)
            self._last_states[entity_id] = state
        try:
            self._update_from_state(entity_id, self._part_state(entity_id, state))
        except (TypeError, ValueError, OverflowError) as err:
            _LOGGER.warning("%s: invalid source reading from %s: %s", self.display_name, entity_id, err)
        if recovery is not None:
            recovery.sync()

    @callback
    def _update_from_state(self, entity_id: str, state: State) -> None:
        """Push a live HA state into the characteristics. Subclasses implement."""
        raise NotImplementedError

    # -- HomeKit -> HA --
    def _mapping(self, entity_id: str, role: str | None = None) -> dict:
        options = getattr(self, "_options", {})
        if role is not None:
            mapping = mapping_for(options, role)
            return mapping if mapping.get("entity_id") == entity_id else {}
        matches = [value for key in (options.get(CONF_VALUE_MAPPINGS) or {})
                   if (value := mapping_for(options, key)).get("entity_id") == entity_id]
        return matches[0] if len(matches) == 1 else {}

    def _control(self, state: State, role: str | None = None) -> ScalarControl:
        control = ScalarControl.from_attributes(state.entity_id.split(".", 1)[0], state.attributes)
        return control.with_mapping(self._mapping(state.entity_id, role))

    def _state_on(self, state: State, role: str | None = None) -> bool:
        if state.entity_id.split(".", 1)[0] in SCALAR_DOMAINS:
            try:
                return self._control(state, role).is_on(state.state) is True
            except ValueError:
                return False
        return state.state == STATE_ON

    def _scalar(self, state: State, role: str | None = None) -> Any:
        if state.entity_id.startswith("select."):
            return self._control(state, role).scalar(state.state)
        return state.state

    def _part_state(self, entity_id: str, state: State) -> State:
        """Supply attributes native entities normally carry for scalar parts."""
        domain = entity_id.split(".", 1)[0]
        if domain not in SCALAR_DOMAINS:
            return state
        attrs = dict(state.attributes)
        brightness = self._control(state, CONF_LIGHT).percentage(state.state)
        if brightness is not None:
            attrs.setdefault(ATTR_BRIGHTNESS, round(brightness * 255 / 100))
        for role in (CONF_TEMPERATURE, "reading"):
            mapping = self._mapping(entity_id, role)
            if mapping.get("unit"):
                attrs[ATTR_UNIT_OF_MEASUREMENT] = mapping["unit"]
        return State(entity_id, state.state, attrs)

    @callback
    def _write_oscillate(self, source: OscillateSource, value: int) -> None:
        role = CONF_OSCILLATE
        if self._mapping(source.entity_id, role):
            self._call(source.domain, SERVICE_OSCILLATE,
                       {ATTR_ENTITY_ID: source.entity_id, ATTR_OSCILLATING: value == 1}, role=role)
            return
        target = oscillate_from_homekit(source, value)
        if source.domain == FAN_DOMAIN:
            service, payload = SERVICE_OSCILLATE, {ATTR_OSCILLATING: target}
        elif source.domain in NUMBER_DOMAINS:
            service, payload = "set_value", {"value": target}
        elif source.domain == SELECT_DOMAIN:
            service, payload = "select_option", {"option": target}
        else:
            service, payload = SERVICE_TURN_ON if target else SERVICE_TURN_OFF, {}
        self._call(source.domain, service, {ATTR_ENTITY_ID: source.entity_id, **payload}, role=role)

    @callback
    def _call(self, domain: str, service: str, data: dict[str, Any], *, role: str | None = None) -> None:
        """Run a service call from a HomeKit write (HAP-python calls us in the loop)."""
        entity_id = data.get(ATTR_ENTITY_ID)
        recovery = getattr(self, "_recovery", None)
        if recovery is not None:
            if recovery.stopped:
                return
            if recovery.stage(domain, service, data, role):
                return
            if isinstance(entity_id, str) and not recovery.live_available(entity_id):
                recovery.failed_entities.add(entity_id)
                recovery.sync()
                _LOGGER.error("%s: cannot write unavailable source %s", self.display_name, entity_id)
                return
        source_domain = entity_id.split(".", 1)[0] if isinstance(entity_id, str) else domain
        if source_domain in SCALAR_DOMAINS:
            state = self.hass.states.get(entity_id)
            try:
                if state is None:
                    raise ValueError("The source entity is unavailable")
                service, data = self._control(state, role).service(service, data)
            except (TypeError, ValueError) as err:
                _LOGGER.error("%s: cannot write %s: %s", self.display_name, entity_id, err)
                self._apply_state(entity_id, state)
                return
            domain = source_domain
        _LOGGER.debug("%s: HomeKit -> %s.%s %s", self.display_name, domain, service, data)
        self.trace("call %s.%s %s", domain, service, data)
        self.hass.async_create_task(
            self._async_call(domain, service, data), eager_start=True
        )

    async def _async_call(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
            self.trace("call %s.%s done", domain, service)
            if (recovery := getattr(self, "_recovery", None)) is not None:
                recovery.failed_entities.discard(data.get(ATTR_ENTITY_ID))
                recovery.sync()
            return True
        except Exception as err:  # noqa: BLE001 - surface any failure, then resync
            _LOGGER.exception("%s: %s.%s %s failed", self.display_name, domain, service, data)
            self.trace("call %s.%s FAILED: %s", domain, service, err)
            entity_id = data.get(ATTR_ENTITY_ID)
            if (recovery := getattr(self, "_recovery", None)) is not None:
                # A queued setting reports failure on its part, rather than
                # faulting the other controls of a shared climate/fan entity.
                if asyncio.current_task() not in recovery.tasks.values():
                    recovery.failed_entities.add(entity_id)
            if isinstance(entity_id, str):
                self._apply_state(entity_id, self.hass.states.get(entity_id))
            return False


# --- Assembled fan ----------------------------------------------------------------
class FanAccessory(KitbashAccessory):
    """A Fanv2 service from parts, plus a Lightbulb from a light entity and a
    TemperatureSensor from a temperature sensor entity.

    Fanv2 characteristics: Active <- the power entity; RotationSpeed <- the
    speed part (fan percentage, number or select, always mapped onto 0-100,
    see ``model.SpeedSource``); RotationDirection <- the direction part, with
    an optional reversal; SwingMode <- the oscillate part; TargetFanState <-
    the mode part (a fan's presets or a select, two of its options named Auto
    and Manual, or a switch: on = Auto). CurrentFanState is deliberately absent: the Home app
    never showed it (observed 2026-09-13). Fanv2 has no temperature setpoint:
    Auto only hands speed control to the device.

    With ``named_speeds`` the accessory also publishes one Switch service per
    speed level, each linked to the Fanv2 -- the only way the Home app shows
    named speeds (Low / Medium / High), since RotationSpeed is a percentage by
    protocol. It is how Home Assistant's own bridge exposes preset modes.
    Exactly one is on at a time; switching the on one off stops the fan.

    Writes arrive as one batch per service (the Home app sends Active and
    RotationSpeed together), handled in HA's own Fan accessory order. Every
    batch is also published to the diagnostic sensor, raw and as stored.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        driver: KitbashDriver,
        fan: AssembledFan,
        entry_id: str,
        model_name: str = MODEL_CEILING_FAN,
        trace: Tracer | None = None,
    ) -> None:
        super().__init__(
            hass, driver, fan.name, entry_id, model_name, CATEGORY_FAN, fan.entity_ids, trace
        )
        self.fan = fan
        self._direction_storage = getattr(driver, "kitbash_direction_storage", None)
        self._direction_confirmed = (
            self._direction_storage.confirmed if self._direction_storage else None
        )
        self._direction_pending = (
            self._direction_storage.pending if self._direction_storage else None
        )
        self._direction_task: asyncio.Task | None = None
        self._direction_timer: CALLBACK_TYPE | None = None
        self._direction_attempted: int | None = None
        self._direction_was_ready = False
        self._direction_fault = False
        self._direction_timed_out = False
        self._direction_stopped = False
        self._direction_power_off_requested = False
        self._power_off_interrupted = False
        self._power_off_timer: CALLBACK_TYPE | None = None

        chars = [CHAR_NAME, CHAR_CONFIGURED_NAME]
        if fan.speed:
            chars.append(CHAR_ROTATION_SPEED)
        if fan.direction:
            chars.append(CHAR_ROTATION_DIRECTION)
        if fan.oscillate:
            chars.append(CHAR_SWING_MODE)
        if fan.mode:
            chars.append(CHAR_TARGET_FAN_STATE)
        self.serv_fan = serv_fan = self.add_preload_service(
            SERV_FANV2, chars, unique_id=fan.power_entity_id
        )
        serv_fan.is_primary_service = True
        serv_fan.configure_char(CHAR_NAME, value=fan.name)
        serv_fan.configure_char(CHAR_CONFIGURED_NAME, value=fan.name)
        self.char_active = serv_fan.configure_char(CHAR_ACTIVE, value=ACTIVE_INACTIVE)
        self.char_speed: Characteristic | None = None
        self.char_direction: Characteristic | None = None
        self.char_swing: Characteristic | None = None
        self.char_target: Characteristic | None = None
        if fan.speed:
            props = fan.speed.homekit_props
            # Start at the top, not 0: HomeKit treats a stored 0 as "off" and
            # would restore 100 on the next turn-on. Real value follows from state.
            self.char_speed = serv_fan.configure_char(
                CHAR_ROTATION_SPEED, value=props["maxValue"], properties=props
            )
        if fan.direction:
            self.char_direction = serv_fan.configure_char(
                CHAR_ROTATION_DIRECTION,
                value=(self._direction_pending if self._direction_pending is not None
                       else self._direction_confirmed if self._direction_confirmed is not None
                       else DIRECTION_CLOCKWISE),
            )
        if fan.oscillate:
            self.char_swing = serv_fan.configure_char(CHAR_SWING_MODE, value=0)
        if fan.mode:
            self.char_target = serv_fan.configure_char(
                CHAR_TARGET_FAN_STATE, value=TARGET_MANUAL
            )
        serv_fan.setter_callback = self._set_fan_chars

        # One Switch per named speed, linked to the fan so the Home app groups
        # them under it (HA does the same for preset modes, type_fans.py).
        self.speed_switches: list[Characteristic] = []
        for index, label in enumerate(fan.speed_names):
            serv_speed = self.add_preload_service(
                SERV_SWITCH, [CHAR_NAME, CHAR_CONFIGURED_NAME], unique_id=f"speed-{index}"
            )
            serv_fan.add_linked_service(serv_speed)
            serv_speed.configure_char(
                CHAR_NAME, value=f"{fan.name} {label}"[:MAX_NAME_LENGTH]
            )
            serv_speed.configure_char(CHAR_CONFIGURED_NAME, value=label)
            self.speed_switches.append(serv_speed.configure_char(CHAR_ON, value=False))
            serv_speed.setter_callback = partial(self._set_speed_switch, index, serv_speed)

        self.serv_light: Service | None = None
        self.char_light_on: Characteristic | None = None
        self.char_brightness: Characteristic | None = None
        if fan.light_entity_id:
            light_chars = [CHAR_NAME, CHAR_CONFIGURED_NAME]
            if fan.brightness:
                light_chars.append(CHAR_BRIGHTNESS)
            self.serv_light = serv_light = self.add_preload_service(
                SERV_LIGHTBULB, light_chars, unique_id=fan.light_entity_id
            )
            serv_light.configure_char(CHAR_NAME, value=fan.light_name)
            serv_light.configure_char(CHAR_CONFIGURED_NAME, value=fan.light_name)
            self.char_light_on = serv_light.configure_char(CHAR_ON, value=False)
            if fan.brightness:
                self.char_brightness = serv_light.configure_char(
                    CHAR_BRIGHTNESS, value=100
                )
            serv_light.setter_callback = self._set_light_chars

        self.char_temperature: Characteristic | None = None
        if fan.temperature_entity_id:
            serv_temp = self.add_preload_service(
                SERV_TEMPERATURE_SENSOR,
                [CHAR_NAME, CHAR_CONFIGURED_NAME],
                unique_id=fan.temperature_entity_id,
            )
            serv_temp.configure_char(CHAR_NAME, value=fan.temperature_name)
            serv_temp.configure_char(CHAR_CONFIGURED_NAME, value=fan.temperature_name)
            self.char_temperature = serv_temp.configure_char(
                CHAR_CURRENT_TEMPERATURE, value=0, properties=PROP_CELSIUS
            )
        self._seed()

    # -- HA -> HomeKit --
    @callback
    def _apply_state(self, entity_id: str, state: State | None) -> None:
        if entity_id == self.fan.power_entity_id and self._direction_power_off_requested:
            power = self._direction_power()
            if power is None:
                self._power_off_interrupted = True
            elif power is False or self._power_off_interrupted:
                self._set_power_off_requested(False)
        super()._apply_state(entity_id, state)
        if self.fan.direction and entity_id in (
            self.fan.power_entity_id, self.fan.direction.entity_id
        ):
            self._sync_direction()

    def characteristic_fault(self, iid: int | None) -> bool:
        return super().characteristic_fault(iid) or bool(
            self.char_direction is not None
            and iid == self.iid_manager.get_iid(self.char_direction)
            and (self._direction_fault or (
                self._direction_power() is None and self._read_direction() is None
            ))
        )

    def _direction_power(self) -> bool | None:
        state = self.hass.states.get(self.fan.power_entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        if state.entity_id.split(".", 1)[0] in SCALAR_DOMAINS:
            return self._control(state, CONF_POWER).is_on(state.state)
        return {STATE_ON: True, STATE_OFF: False}.get(state.state)

    def _read_direction(self) -> int | None:
        direction = self.fan.direction
        if direction is None:
            return None
        state = self.hass.states.get(direction.entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        raw = state.attributes.get(ATTR_DIRECTION) if direction.domain == FAN_DOMAIN else state.state
        if direction.domain in NUMBER_DOMAINS:
            on = self._control(state, CONF_DIRECTION).is_on(raw)
            raw = None if on is None else STATE_ON if on else STATE_OFF
        return direction_to_homekit(direction, raw)

    def _remember_direction(self) -> None:
        if self._direction_storage is not None:
            self._direction_storage.update(self._direction_confirmed, self._direction_pending)

    def _cancel_direction_timer(self) -> None:
        if self._direction_timer is not None:
            self._direction_timer()
            self._direction_timer = None

    def _set_direction_fault(self, fault: bool) -> None:
        if fault == self._direction_fault:
            return
        self._direction_fault = fault
        if fault:
            _LOGGER.warning(
                "%s: direction source %s did not recover or confirm the requested setting; "
                "power remains controllable, pending direction is retained",
                self.display_name, self.fan.direction.entity_id,
            )
        else:
            self.trace("direction source recovered or fan is off")

    @callback
    def _direction_expired(self, _now: Any) -> None:
        self._direction_timer = None
        if self._direction_power() is True and not self._direction_power_off_requested:
            self._direction_timed_out = True
            self._set_direction_fault(True)

    def _sync_direction(self) -> None:
        """Hold a requested choice until both power and direction are usable."""
        if self.fan.direction is None or self._direction_stopped:
            return
        actual = self._read_direction()
        if actual is not None:
            self._direction_confirmed = actual
            if self._direction_pending is None:
                self._push(self.char_direction, actual, self.fan.direction.entity_id)
        powered = self._direction_power() is True and not self._direction_power_off_requested
        ready = powered and actual is not None
        if ready and not self._direction_was_ready:
            self._direction_attempted = None
            self._direction_timed_out = False
            self._cancel_direction_timer()
            self._set_direction_fault(False)
        self._direction_was_ready = ready
        if not powered:
            self._cancel_direction_timer()
            self._direction_timed_out = False
            self._direction_attempted = None
            self._set_direction_fault(False)
        elif ready and actual == self._direction_pending and self._direction_task is None:
            self._direction_pending = None
        if powered and (not ready or self._direction_pending is not None):
            if self._direction_timer is None and not self._direction_timed_out:
                self._direction_timer = async_call_later(
                    self.hass, DIRECTION_RECOVERY_SECONDS, self._direction_expired
                )
            if (ready and self._direction_pending is not None and self._direction_task is None
                    and self._direction_attempted != self._direction_pending):
                value = self._direction_pending
                self._direction_attempted = value
                self._direction_task = self.hass.async_create_task(
                    self._async_write_direction(value), eager_start=False
                )
        elif powered:
            self._cancel_direction_timer()
            self._direction_timed_out = False
            self._set_direction_fault(False)
        self._remember_direction()

    async def _async_write_direction(self, value: int) -> None:
        """Serialize direction writes; only an observed state consumes the queue."""
        try:
            if (self._direction_power() is not True or self._direction_power_off_requested
                    or self._read_direction() is None or self._direction_pending != value):
                return
            direction = self.fan.direction
            target = direction_from_homekit(direction, value)
            domain = direction.domain
            if domain == FAN_DOMAIN:
                service, payload = SERVICE_SET_DIRECTION, {ATTR_DIRECTION: target}
            elif domain == SELECT_DOMAIN:
                service, payload = "select_option", {"option": target}
            else:
                service, payload = SERVICE_TURN_ON if target else SERVICE_TURN_OFF, {}
            data = {ATTR_ENTITY_ID: direction.entity_id, **payload}
            if domain in SCALAR_DOMAINS:
                state = self.hass.states.get(direction.entity_id)
                service, data = self._control(state, CONF_DIRECTION).service(service, data)
            self.trace("call %s.%s %s (deferred direction)", domain, service, data)
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except Exception as err:  # noqa: BLE001 - retain the queued choice on failure
            _LOGGER.error("%s: deferred direction write failed: %s", self.display_name, err)
            if self._direction_pending == value and self._direction_power() is True:
                self._set_direction_fault(True)
        finally:
            self._direction_task = None
            self._sync_direction()

    async def stop(self) -> None:
        self._direction_stopped = True
        self._set_power_off_requested(False)
        self._cancel_direction_timer()
        if self._direction_task is not None:
            task = self._direction_task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._direction_storage is not None:
            await self._direction_storage.async_flush()
        await super().stop()

    async def _async_call(self, domain: str, service: str, data: dict[str, Any]) -> bool:
        success = await super()._async_call(domain, service, data)
        if not success and data.get(ATTR_ENTITY_ID) == self.fan.power_entity_id:
            # A rejected power-off must not leave direction permanently paused.
            self._set_power_off_requested(False)
            if (recovery := getattr(self, "_recovery", None)) is not None:
                recovery.sync()
            self._sync_direction()
        return success

    @callback
    def _update_from_state(self, entity_id: str, state: State) -> None:
        fan = self.fan
        if entity_id == fan.power_entity_id:
            is_on = self._state_on(state, CONF_POWER)
            self._push(self.char_active, ACTIVE_ACTIVE if is_on else ACTIVE_INACTIVE, entity_id)
        if fan.mode and entity_id == fan.mode.entity_id and self.char_target is not None:
            current = (
                state.attributes.get(ATTR_PRESET_MODE)
                if fan.mode.domain == FAN_DOMAIN
                else state.state
            )
            if fan.mode.domain in NUMBER_DOMAINS:
                current = self._scalar(state, CONF_MODE)
                try:
                    is_auto = float(current) == float(fan.mode.auto)
                except (TypeError, ValueError):
                    is_auto = False
            else:
                is_auto = current == fan.mode.auto
            self._push(
                self.char_target,
                TARGET_AUTO if is_auto else TARGET_MANUAL,
                entity_id,
            )
        if fan.speed and entity_id == fan.speed.entity_id and self.char_speed is not None:
            raw = (
                state.attributes.get(ATTR_PERCENTAGE)
                if fan.speed.domain == FAN_DOMAIN
                else state.state
            )
            # While the power entity is off keep the stored speed (turn-on restores it).
            power = self.hass.states.get(fan.power_entity_id)
            if power is not None and self._state_on(power, CONF_POWER):
                value = speed_to_homekit(fan.speed, raw)
                if value is not None:
                    self._push(self.char_speed, value, entity_id)
        if (
            self.speed_switches
            and fan.speed
            and entity_id in (fan.power_entity_id, fan.speed.entity_id)
        ):
            # Exactly one on, none while the fan is off. While it runs but the
            # speed entity reads nothing usable (an Alexa number's 0 sentinel,
            # say) the switches hold, the way the slider holds its last value.
            power = self.hass.states.get(fan.power_entity_id)
            if power is not None and not self._state_on(power, CONF_POWER):
                for char in self.speed_switches:
                    self._push(char, False, entity_id)
            elif (active := self._active_speed_level()) is not None:
                for index, char in enumerate(self.speed_switches):
                    self._push(char, index == active, entity_id)
        if fan.oscillate and entity_id == fan.oscillate.entity_id and self.char_swing is not None:
            raw = (
                state.attributes.get(ATTR_OSCILLATING)
                if fan.oscillate.domain == FAN_DOMAIN else state.state
            )
            value = (int(self._state_on(state, CONF_OSCILLATE)) if self._mapping(entity_id, CONF_OSCILLATE)
                     else oscillate_to_homekit(fan.oscillate, raw))
            if value is not None:
                self._push(self.char_swing, value, entity_id)
        if entity_id == fan.light_entity_id and self.char_light_on is not None:
            is_on = self._state_on(state, CONF_LIGHT)
            self._push(self.char_light_on, is_on, entity_id)
            if self.char_brightness is not None and is_on:
                brightness = state.attributes.get(ATTR_BRIGHTNESS)
                if brightness is not None:
                    self._push(self.char_brightness, max(1, round(int(brightness) * 100 / 255)), entity_id)
        if entity_id == fan.temperature_entity_id and self.char_temperature is not None:
            celsius = temperature_to_celsius(self._scalar(state, CONF_TEMPERATURE), _temperature_unit(state))
            if celsius is not None:
                self._push(self.char_temperature, celsius, entity_id)

    # -- HomeKit -> HA --
    @callback
    def _set_power_off_requested(self, requested: bool) -> None:
        if self._power_off_timer is not None:
            self._power_off_timer()
            self._power_off_timer = None
        self._direction_power_off_requested = requested
        self._power_off_interrupted = False
        if requested:
            self._power_off_timer = async_call_later(
                self.hass, DIRECTION_RECOVERY_SECONDS, self._power_off_expired
            )

    @callback
    def _power_off_expired(self, _now: Any) -> None:
        self._set_power_off_requested(False)
        if self._direction_power() is True:
            _LOGGER.warning("%s: power source %s did not confirm Off",
                            self.display_name, self.fan.power_entity_id)
            self._recovery.failed_entities.add(self.fan.power_entity_id)
        self._recovery.sync()
        if self.fan.direction:
            self._sync_direction()

    @callback
    def _set_power(self, on: bool) -> None:
        self._set_power_off_requested(not on)
        if (recovery := getattr(self, "_recovery", None)) is not None:
            recovery.sync()
        if self.fan.direction:
            self._sync_direction()
        domain = self.fan.power_entity_id.split(".", 1)[0]
        self._call(
            domain, SERVICE_TURN_ON if on else SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: self.fan.power_entity_id}, role=CONF_POWER,
        )

    @callback
    def _active_speed_level(self) -> int | None:
        """Which named speed is on, read from live state; None while the fan is off."""
        speed = self.fan.speed
        if speed is None:
            return None
        power = self.hass.states.get(self.fan.power_entity_id)
        if power is None or not self._state_on(power, CONF_POWER):
            return None
        state = self.hass.states.get(speed.entity_id)
        if state is None:
            return None
        raw = (
            state.attributes.get(ATTR_PERCENTAGE)
            if speed.domain == FAN_DOMAIN
            else state.state
        )
        value = speed_to_homekit(speed, raw)
        return None if value is None else percentage_to_level(speed.count, value)

    @callback
    def _set_speed_switch(self, index: int, service: Service, values: dict[str, Any]) -> None:
        """One named-speed Switch written from a controller."""
        self._record_write(service, values)
        speed = self.fan.speed
        if CHAR_ON not in values or speed is None:
            return
        if not values[CHAR_ON]:
            # Switching off the speed the fan is running at means stop. Any
            # other switch is already off, so that write is a no-op.
            if self._active_speed_level() == index:
                self._set_power(False)
            return
        target = homekit_to_speed(speed, level_to_percentage(speed.count, index))
        if target is None:
            return
        # Start a native fan at the requested speed in one power command;
        # separate speed sources wait for the observed power-on.
        if speed.domain == FAN_DOMAIN and speed.entity_id == self.fan.power_entity_id:
            self._set_power_off_requested(False)
            self._call(FAN_DOMAIN, SERVICE_TURN_ON,
                       {ATTR_ENTITY_ID: speed.entity_id, ATTR_PERCENTAGE: target}, role=CONF_POWER)
        else:
            self._set_power(True)
        self._write_speed(target)

    @callback
    def _set_speed(self, value: float) -> None:
        speed = self.fan.speed
        assert speed is not None
        target = homekit_to_speed(speed, value)
        if target is None:
            self._set_power(False)
            return
        self._write_speed(target)

    @callback
    def _write_speed(self, target: float | str) -> None:
        """Write one speed level to whichever entity carries the speed."""
        speed = self.fan.speed
        assert speed is not None
        if speed.domain == FAN_DOMAIN:
            self._call(
                FAN_DOMAIN, SERVICE_SET_PERCENTAGE,
                {ATTR_ENTITY_ID: speed.entity_id, ATTR_PERCENTAGE: target},
                role=CONF_SPEED,
            )
        elif speed.domain in NUMBER_DOMAINS:
            self._call(speed.domain, "set_value", {ATTR_ENTITY_ID: speed.entity_id, "value": target}, role=CONF_SPEED)
        else:
            self._call("select", "select_option", {ATTR_ENTITY_ID: speed.entity_id, "option": target}, role=CONF_SPEED)

    @callback
    def _set_direction(self, value: int) -> None:
        assert self.fan.direction is not None
        self._direction_pending = int(value)
        self._direction_attempted = None
        self._direction_timed_out = False
        self._cancel_direction_timer()
        self._set_direction_fault(False)
        self._push(self.char_direction, int(value), "HomeKit direction choice")
        self._sync_direction()

    @callback
    def _set_oscillate(self, value: int) -> None:
        oscillate = self.fan.oscillate
        assert oscillate is not None
        self._write_oscillate(oscillate, value)

    @callback
    def _set_fan_chars(self, values: dict[str, Any]) -> None:
        """One batch of Fanv2 writes from a controller."""
        self._record_write(self.serv_fan, values)
        fan = self.fan
        if CHAR_ACTIVE in values:
            if values[CHAR_ACTIVE]:
                # A native fan starts at this percentage in one command.
                # Separate settings wait for the observed power-on.
                speed_is_power = (
                    fan.speed is not None
                    and fan.speed.domain == FAN_DOMAIN
                    and fan.speed.entity_id == fan.power_entity_id
                )
                if speed_is_power and CHAR_ROTATION_SPEED in values:
                    target = homekit_to_speed(fan.speed, values[CHAR_ROTATION_SPEED])
                    if target is not None:
                        self._set_power_off_requested(False)
                        self._call(FAN_DOMAIN, SERVICE_TURN_ON,
                                   {ATTR_ENTITY_ID: fan.power_entity_id, ATTR_PERCENTAGE: target}, role=CONF_POWER)
                else:
                    self._set_power(True)
            else:
                # Off wins; applying the other values would turn it back on.
                self._set_power(False)
                if CHAR_ROTATION_DIRECTION in values and fan.direction:
                    self._set_direction(values[CHAR_ROTATION_DIRECTION])
                return

        if CHAR_SWING_MODE in values and fan.oscillate:
            self._set_oscillate(values[CHAR_SWING_MODE])
        if CHAR_ROTATION_DIRECTION in values and fan.direction:
            self._set_direction(values[CHAR_ROTATION_DIRECTION])
        # Speed last, so the fan ends at the speed asked for.
        if CHAR_ROTATION_SPEED in values and fan.speed:
            self._set_speed(values[CHAR_ROTATION_SPEED])
        if CHAR_TARGET_FAN_STATE in values and fan.mode:
            self._set_target_fan_state(values[CHAR_TARGET_FAN_STATE])

    @callback
    def _set_target_fan_state(self, value: int) -> None:
        mode = self.fan.mode
        assert mode is not None
        target = {ATTR_ENTITY_ID: mode.entity_id}
        option = mode.auto if value == TARGET_AUTO else mode.manual
        if option is None:
            # A fan without a manual preset: leave auto by re-asserting the
            # current speed, which drops the preset.
            state = self.hass.states.get(mode.entity_id)
            percentage = (state.attributes.get(ATTR_PERCENTAGE) if state else None) or 50
            self._call(FAN_DOMAIN, SERVICE_SET_PERCENTAGE, {**target, ATTR_PERCENTAGE: percentage}, role=CONF_MODE)
        elif mode.domain == FAN_DOMAIN:
            self._call(FAN_DOMAIN, SERVICE_SET_PRESET_MODE, {**target, ATTR_PRESET_MODE: option}, role=CONF_MODE)
        elif mode.domain in ("switch", "input_boolean"):
            self._call(mode.domain, SERVICE_TURN_ON if option == STATE_ON else SERVICE_TURN_OFF, target, role=CONF_MODE)
        else:
            self._call("select", "select_option", {**target, "option": option}, role=CONF_MODE)

    @callback
    def _set_light_chars(self, values: dict[str, Any]) -> None:
        """One batch of Lightbulb writes from a controller."""
        assert self.fan.light_entity_id and self.serv_light is not None
        self._record_write(self.serv_light, values)
        data: dict[str, Any] = {ATTR_ENTITY_ID: self.fan.light_entity_id}
        service = SERVICE_TURN_ON
        if CHAR_ON in values and not values[CHAR_ON]:
            service = SERVICE_TURN_OFF
        if CHAR_BRIGHTNESS in values:
            if values[CHAR_BRIGHTNESS] == 0:
                service = SERVICE_TURN_OFF
            else:
                data[ATTR_BRIGHTNESS_PCT] = values[CHAR_BRIGHTNESS]
        if service == SERVICE_TURN_OFF:
            data = {ATTR_ENTITY_ID: self.fan.light_entity_id}
        self._call(LIGHT_DOMAIN, service, data, role=CONF_LIGHT)
