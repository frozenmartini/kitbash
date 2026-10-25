"""Per-source availability and deferred settings, independent of HA imports."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .controls import SCALAR_DOMAINS
from .model import percentage_to_level

_LOGGER = logging.getLogger(__name__)
RECOVERY_SECONDS = 90
SETTING_SERVICES = frozenset({
    "set_value", "select_option", "set_percentage", "oscillate",
    "set_preset_mode", "set_temperature", "turn_on", "turn_off",
    "set_humidity",
})
THRESHOLD_FIELDS = frozenset({"target_temp_low", "target_temp_high"})


def finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


@dataclass(eq=False)
class Part:
    entity_id: str
    role: str | None
    chars: list[Any] = field(default_factory=list)
    standby: str | None = None
    standby_role: str = "power"
    power_check: Any = None
    attribute: str | None = None
    numeric: bool = False
    setting: bool = False
    validator: Any = None
    cancel: Any = None
    expired: bool = False
    was_ready: bool = False
    fault: bool = False


@dataclass(eq=False)
class Command:
    part: Part
    domain: str
    service: str
    data: dict[str, Any]
    attempted: bool = False
    failed: bool = False
    dispatched: bool = False
    force: bool = False
    confirmation_state: Any = None


class SourceRecovery:
    """Keep unavailable sources distinct from off, zero or a healthy sibling."""

    def __init__(self, accessory: Any, later: Any, storage: Any = None) -> None:
        self.accessory, self.later, self.storage = accessory, later, storage
        self.parts: list[Part] = []
        self.by_char: dict[Any, list[Part]] = {}
        self.inactive_sources: dict[Any, tuple] = {}
        self.pending: dict[Part, Command] = {}
        self.tasks: dict[Part, asyncio.Task] = {}
        self.failed_entities: set[str] = set()
        self.stopped = False
        self.values: dict[str, Any] = {}

    def bind(self, entity_id: str | None, role: str | None, chars: tuple,
             **kwargs: Any) -> None:
        chars = tuple(char for char in chars if char is not None)
        if not entity_id or not chars:
            return
        part = Part(entity_id, role, list(chars), **kwargs)
        existing = next((p for p in self.parts if (p.entity_id, p.role, p.standby, p.attribute,
                          p.numeric, p.setting, p.validator, p.standby_role, p.power_check) == (part.entity_id, part.role,
                          part.standby, part.attribute, part.numeric, part.setting, part.validator,
                          part.standby_role, part.power_check)), None)
        if existing is not None:
            part = existing
            part.chars.extend(char for char in chars if char not in part.chars)
        else:
            self.parts.append(part)
        for char in chars:
            self.by_char.setdefault(char, []).append(part)

    def depend(self, char: Any, sources: tuple) -> None:
        if char is None:
            return
        for source in sources:
            for part in self.by_char.get(source, ()):
                if part not in self.by_char.setdefault(char, []):
                    self.by_char[char].append(part)

    def inactive(self, char: Any, sources: tuple) -> None:
        """A known off state needs no temperature, speed or threshold reading."""
        if char is not None:
            self.inactive_sources[char] = sources

    def _parts_for(self, char: Any) -> list[Part]:
        sources = self.inactive_sources.get(char)
        if sources and all(source.value == 0 for source in sources):
            parts = [part for source in sources for part in self.by_char.get(source, ())]
            if all(self.ready(part) for part in parts):
                return parts
        return self.by_char.get(char, [])

    def live_available(self, entity_id: str) -> bool:
        state = self.accessory.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return False
        domain = entity_id.split(".", 1)[0]
        if domain == "select":
            return state.state in (state.attributes.get("options") or ())
        if domain in ("number", "input_number"):
            return finite(state.state)
        if domain in ("switch", "input_boolean", "binary_sensor", "light", "fan"):
            return state.state in ("on", "off")
        return True

    def ready(self, part: Part) -> bool:
        if not self.live_available(part.entity_id):
            return False
        state = self.accessory.hass.states.get(part.entity_id)
        if part.validator is not None:
            return bool(part.validator(state))
        if part.attribute is not None:
            value = state.attributes.get(part.attribute)
            return finite(value) if part.numeric else value is not None
        if part.numeric:
            return finite(self.accessory._scalar(state, part.role))
        return True

    def powered(self, part: Part) -> bool | None:
        if part.power_check is not None:
            return part.power_check()
        if part.standby is None:
            return True
        if not self.live_available(part.standby):
            return None
        if (part.standby == getattr(getattr(self.accessory, "fan", None), "power_entity_id", None)
                and getattr(self.accessory, "_direction_power_off_requested", False)):
            return False
        state = self.accessory.hass.states.get(part.standby)
        domain = part.standby.split(".", 1)[0]
        if domain in ("climate", "water_heater"):
            return state.state != "off"
        return self.accessory._state_on(state, part.standby_role)

    def _cancel(self, part: Part) -> None:
        if part.cancel is not None:
            part.cancel()
            part.cancel = None

    def _expired(self, part: Part, _now: Any) -> None:
        part.cancel = None
        part.expired = True
        self.sync()

    def writable(self, part: Part) -> bool:
        return self.live_available(part.entity_id) and (
            part.standby is None and part.power_check is None or self.powered(part) is True
        )

    def _busy(self, part: Part) -> bool:
        # Native climate roles share one physical controller. An older write
        # can still change its state, even when a newer intent matches it now.
        return part in self.tasks or (part.entity_id.startswith("climate.") and any(
            running.entity_id == part.entity_id for running in self.tasks
        ))

    def sync(self) -> None:
        if self.stopped:
            return
        for part in self.parts:
            ready, power = self.ready(part), self.powered(part)
            writable = self.writable(part)
            command = self.pending.get(part)
            if writable and not part.was_ready:
                part.expired = False
                self._cancel(part)
                if command is not None:
                    command.attempted = command.failed = False
                self.failed_entities.discard(part.entity_id)
            part.was_ready = writable
            if command is not None and writable and not self._busy(part):
                if self._matches(command):
                    self.pending.pop(part)
                    command = None
                elif not command.attempted:
                    command.attempted = True
                    self.tasks[part] = self.accessory.hass.async_create_task(
                        self._execute(command), eager_start=False
                    )
            waiting = not ready or command is not None
            standby_off = (part.standby is not None or part.power_check is not None) and power is False
            if standby_off:
                self._cancel(part)
                part.expired = False
            elif waiting and (part.setting or part.standby is not None):
                if part.cancel is None and not part.expired:
                    part.cancel = self.later(
                        self.accessory.hass, RECOVERY_SECONDS,
                        lambda now, part=part: self._expired(part, now),
                    )
            else:
                self._cancel(part)
                part.expired = False
            fault = (
                part.entity_id in self.failed_entities
                or bool(command and command.failed and not standby_off)
                or (waiting and not standby_off and (
                    part.expired or not (part.setting or part.standby is not None)
                    or power is None
                ))
            )
            if fault and not part.fault:
                _LOGGER.warning("%s: source %s (%s) is unavailable or did not confirm a setting",
                                self.accessory.display_name, part.entity_id, part.role or "state")
            part.fault = fault
        self.save()

    def fault(self, char: Any) -> bool:
        return any(part.fault for part in self._parts_for(char))

    def accepts_push(self, char: Any, source: str) -> bool:
        if self.stopped:
            return False
        parts = self._parts_for(char)
        if any(not self.ready(part) for part in parts):
            return False
        return source == "HomeKit deferred setting" or not any(part in self.pending for part in parts)

    def write_unavailable(self, char: Any, value: Any) -> bool:
        if self.stopped:
            return True
        parts = self.by_char.get(char, ())
        # Locally stored settings and direction have their own recovery queues.
        if parts and all(part.setting for part in parts):
            return False
        return any(not self.live_available(part.entity_id) for part in parts if not part.setting)

    def stage(self, domain: str, service: str, data: dict, role: str | None) -> bool:
        part = next((part for part in self.parts if part.setting
                     and part.entity_id == data.get("entity_id") and part.role == role), None)
        if part is None or service not in SETTING_SERVICES:
            return False
        # Turn-on/off are settings only for switch-backed mode/oscillation.
        if service in ("turn_on", "turn_off") and role not in ("mode", "oscillate", "swing"):
            return False
        data = dict(data)
        force = (domain == "climate" or role == "mode") and any(
            previous.part.entity_id == part.entity_id
            and (domain == "climate" or previous.part.role == role)
            and (previous.dispatched or previous.force)
            for previous in self.pending.values()
        )
        if domain == "climate" and service == "set_temperature" and THRESHOLD_FIELDS.intersection(data):
            incoming = THRESHOLD_FIELDS.intersection(data)
            for previous_part, previous in list(self.pending.items()):
                if (previous_part.entity_id != part.entity_id or previous.service != service
                        or not THRESHOLD_FIELDS.intersection(previous.data)):
                    continue
                if previous_part is part:
                    # A batch may have staged both thresholds under this role.
                    for key in THRESHOLD_FIELDS - incoming:
                        if key in previous.data:
                            data[key] = previous.data[key]
                else:
                    # Latest intent wins across heating/cooling/midpoint roles.
                    for key in incoming:
                        previous.data.pop(key, None)
                    if not THRESHOLD_FIELDS.intersection(previous.data):
                        self.pending.pop(previous_part)
        self.pending[part] = Command(part, domain, service, data, force=force)
        self.failed_entities.discard(part.entity_id)
        part.expired = False
        self._cancel(part)
        for char in part.chars:
            self.values[str(self.accessory.iid_manager.get_iid(char))] = char.value
        self.sync()
        return True

    def _prepared(self, command: Command) -> tuple[str, str, dict]:
        entity_id = command.part.entity_id
        domain = entity_id.split(".", 1)[0]
        service, data = command.service, dict(command.data)
        if domain in SCALAR_DOMAINS:
            state = self.accessory.hass.states.get(entity_id)
            service, data = self.accessory._control(state, command.part.role).service(service, data)
        elif domain == "climate" and service == "set_temperature" and THRESHOLD_FIELDS.intersection(data):
            # Persist explicit intent only; fill the other threshold at dispatch
            # from outstanding choices, then the latest confirmed HA state.
            for pending in self.pending.values():
                if pending.part.entity_id == entity_id and pending.service == service:
                    data.update({key: pending.data[key] for key in THRESHOLD_FIELDS if key in pending.data})
            state = self.accessory.hass.states.get(entity_id)
            for key in THRESHOLD_FIELDS - data.keys():
                if state is not None and finite(state.attributes.get(key)):
                    data[key] = state.attributes[key]
        return domain, service, data

    def _matches(self, command: Command) -> bool:
        if command.force and (not command.dispatched or self.accessory.hass.states.get(
                command.part.entity_id) is command.confirmation_state):
            # A replacement matching the old reading still needs a write and
            # a fresh confirmation: the preceding command may arrive later.
            return False
        try:
            if command.domain == "climate" and command.service == "set_temperature":
                service, data = command.service, command.data
            else:
                _, service, data = self._prepared(command)
        except (KeyError, TypeError, ValueError):
            return False
        state = self.accessory.hass.states.get(command.part.entity_id)
        if service == "select_option":
            return state.state == data["option"]
        if service == "set_value":
            actual, desired = state.state, data["value"]
        elif service == "set_percentage":
            if command.part.role == "mode":
                return state.attributes.get("preset_mode") is None
            actual, desired = state.attributes.get("percentage"), data["percentage"]
            owner = getattr(self.accessory, "fan", None)
            source = getattr(owner, "speed", None)
            if command.part.role == "speed" and source is not None and source.domain == "fan":
                return (finite(actual) and finite(desired)
                        and float(actual) > 0 and float(desired) > 0
                        and percentage_to_level(source.count, float(actual))
                        == percentage_to_level(source.count, float(desired)))
        elif service == "set_temperature":
            return all(finite(state.attributes.get(key)) and finite(value)
                       and math.isclose(float(state.attributes[key]), float(value), abs_tol=1e-6)
                       for key, value in data.items() if key != "entity_id")
        elif service == "set_humidity":
            actual, desired = state.attributes.get("humidity"), data["humidity"]
        elif service == "oscillate":
            return state.attributes.get("oscillating") is data["oscillating"]
        elif service == "set_preset_mode":
            return state.attributes.get("preset_mode") == data["preset_mode"]
        elif service in ("turn_on", "turn_off"):
            return state.state == ("on" if service == "turn_on" else "off")
        else:
            return False
        return finite(actual) and finite(desired) and math.isclose(float(actual), float(desired), abs_tol=1e-6)

    async def _execute(self, command: Command) -> None:
        part = command.part
        try:
            if self.pending.get(part) is not command or not self.writable(part):
                return
            domain, service, data = self._prepared(command)
            command.confirmation_state = self.accessory.hass.states.get(part.entity_id)
            command.dispatched = True
            success = await self.accessory._async_call(domain, service, data)
            if self.pending.get(part) is command:
                command.failed = not success
        except (KeyError, TypeError, ValueError) as err:
            command.failed = True
            _LOGGER.error("%s: deferred setting failed: %s", self.accessory.display_name, err)
        finally:
            self.tasks.pop(part, None)
            self.sync()
            refresh = getattr(self.accessory, "_recovery_refreshed", None)
            if refresh is not None and not self.stopped:
                refresh()

    def remember(self, char: Any) -> None:
        if char in self.by_char:
            self.values[str(self.accessory.iid_manager.get_iid(char))] = char.value
            self.save()

    def save(self) -> None:
        if self.storage is not None:
            pending = [{"entity_id": part.entity_id, "role": part.role,
                        "domain": command.domain, "service": command.service,
                        "data": command.data, "force": command.force} for part, command in self.pending.items()]
            self.storage.update(self.values, pending)

    def restore(self) -> None:
        if self.storage is None:
            return
        for char in self.by_char:
            key = str(self.accessory.iid_manager.get_iid(char))
            if key in self.storage.values:
                try:
                    char.set_value(self.storage.values[key], should_notify=False)
                    self.values[key] = char.value
                except (TypeError, ValueError):
                    pass
        for raw in self.storage.pending:
            if not isinstance(raw, dict) or raw.get("service") not in SETTING_SERVICES:
                continue
            part = next((p for p in self.parts if p.setting
                         and p.entity_id == raw.get("entity_id") and p.role == raw.get("role")), None)
            data = raw.get("data")
            if part is None or not isinstance(data, dict) or data.get("entity_id") != part.entity_id:
                continue
            if raw["service"] in ("turn_on", "turn_off") and part.role not in ("mode", "oscillate", "swing"):
                continue
            fields = {
                "set_value": {"value"}, "select_option": {"option"},
                "set_percentage": {"percentage"}, "oscillate": {"oscillating"},
                "set_preset_mode": {"preset_mode"}, "set_temperature": {"temperature"},
                "turn_on": set(), "turn_off": set(),
                "set_humidity": {"humidity"},
            }[raw["service"]]
            if raw["service"] == "set_temperature" and "temperature" not in data:
                fields = set(data) - {"entity_id"}
                if not fields or not fields <= {"target_temp_low", "target_temp_high"}:
                    continue
            if set(data) != {"entity_id", *fields}:
                continue
            self.pending[part] = Command(part, part.entity_id.split(".", 1)[0], raw["service"], dict(data),
                                         force=raw.get("force") is True)

    async def stop(self) -> None:
        self.stopped = True
        for part in self.parts:
            self._cancel(part)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.save()
        if self.storage is not None:
            await self.storage.async_flush()
