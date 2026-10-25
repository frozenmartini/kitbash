"""Read what the source entities can do, from live state or the registry.

Registry rows survive the source integration not being loaded yet, so these
work at startup; live state wins when present because it carries attributes
the registry does not keep (a fan's ``percentage_step``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .controls import SCALAR_DOMAINS, ScalarControl
from .assembly import EntityDescription
from .model import (
    FAN_DOMAIN,
    LIGHT_DOMAIN,
    NUMBER_DOMAIN,
    NUMBER_DOMAINS,
    INPUT_NUMBER_DOMAIN,
    INPUT_BOOLEAN_DOMAIN,
    SELECT_DOMAIN,
    SENSOR_DOMAIN,
    SWITCH_DOMAIN,
    FanInfo,
    LightInfo,
    NumberInfo,
    SelectInfo,
)

ATTR_SUPPORTED_FEATURES = "supported_features"
ATTR_PRESET_MODES = "preset_modes"
ATTR_PERCENTAGE_STEP = "percentage_step"
ATTR_SUPPORTED_COLOR_MODES = "supported_color_modes"
COLOR_MODE_ONOFF = "onoff"


@callback
def fan_info(hass: HomeAssistant, entity_id: str | None) -> FanInfo:
    """Features, presets and speed step of a fan entity."""
    if not entity_id:
        return FanInfo()
    state = hass.states.get(entity_id)
    entry = er.async_get(hass).async_get(entity_id)
    features = 0
    presets: tuple[str, ...] = ()
    step = 1.0
    if state is not None:
        features = int(state.attributes.get(ATTR_SUPPORTED_FEATURES) or 0)
        presets = tuple(state.attributes.get(ATTR_PRESET_MODES) or ())
        step = float(state.attributes.get(ATTR_PERCENTAGE_STEP) or 1.0)
    elif entry is not None:
        features = int(entry.supported_features or 0)
        presets = tuple((entry.capabilities or {}).get(ATTR_PRESET_MODES) or ())
    return FanInfo(supported_features=features, preset_modes=presets, percentage_step=step)


@callback
def light_info(hass: HomeAssistant, entity_id: str | None) -> LightInfo:
    """Whether a light entity dims."""
    if not entity_id:
        return LightInfo()
    if entity_id.split(".", 1)[0] in SCALAR_DOMAINS:
        return LightInfo(brightness=True)
    modes: list[str] = []
    if (state := hass.states.get(entity_id)) is not None:
        modes = list(state.attributes.get(ATTR_SUPPORTED_COLOR_MODES) or ())
    elif (entry := er.async_get(hass).async_get(entity_id)) is not None:
        modes = list((entry.capabilities or {}).get(ATTR_SUPPORTED_COLOR_MODES) or ())
    return LightInfo(brightness=any(mode != COLOR_MODE_ONOFF for mode in modes))


ATTR_MIN = "min"
ATTR_MAX = "max"
ATTR_STEP = "step"
ATTR_OPTIONS = "options"
ATTR_UNIT_OF_MEASUREMENT = "unit_of_measurement"
# works_with_alexa puts the scale here when Alexa reports a bare "Degrees" and
# the entity therefore carries no unit of its own.
ATTR_REPORTED_UNIT = "reported_unit"


@callback
def number_info(hass: HomeAssistant, entity_id: str | None, mapping: dict | None = None) -> NumberInfo:
    """Range of a number entity."""
    if not entity_id:
        return NumberInfo()
    attrs: dict = {}
    if (state := hass.states.get(entity_id)) is not None:
        attrs = dict(state.attributes)
    elif (entry := er.async_get(hass).async_get(entity_id)) is not None:
        attrs = dict(entry.capabilities or {})
    unit = attrs.get(ATTR_UNIT_OF_MEASUREMENT) or attrs.get(ATTR_REPORTED_UNIT)
    if entity_id.startswith("select."):
        control = ScalarControl.from_attributes("select", attrs)
        info = NumberInfo(control.minimum, control.maximum, control.step, str(unit) if unit else None)
        return _mapped_number(info, mapping)
    try:
        return _mapped_number(NumberInfo(
            minimum=float(attrs.get(ATTR_MIN, 0.0)),
            maximum=float(attrs.get(ATTR_MAX, 100.0)),
            step=float(attrs.get(ATTR_STEP, 1.0)),
            unit=str(unit) if unit else None,
        ), mapping)
    except (TypeError, ValueError):
        return NumberInfo()


def _mapped_number(info: NumberInfo, mapping: dict | None) -> NumberInfo:
    mapping = mapping or {}
    return replace(info, minimum=float(mapping.get("minimum", info.minimum)),
                   maximum=float(mapping.get("maximum", info.maximum)),
                   unit=mapping.get("unit", info.unit), levels=tuple(mapping.get("levels") or ()))


@callback
def mode_options(hass: HomeAssistant, entity_id: str | None) -> tuple[str, ...]:
    """What a mode entity offers: a fan's presets or a select's options."""
    if not entity_id:
        return ()
    if entity_id.startswith(f"{FAN_DOMAIN}."):
        return fan_info(hass, entity_id).preset_modes
    if entity_id.split(".", 1)[0] in NUMBER_DOMAINS:
        info = number_info(hass, entity_id)
        return tuple(f"{value:g}" for value in (info.minimum, info.maximum))
    return select_info(hass, entity_id).options


# --- The flow's dropdowns: every entity of the power entity's device ----------------
CANDIDATE_DOMAINS = (
    FAN_DOMAIN, SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN, SELECT_DOMAIN,
    NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN, SENSOR_DOMAIN, LIGHT_DOMAIN,
)


@dataclass(frozen=True)
class Candidate:
    """One entity offered in the part dropdowns."""

    entity_id: str
    label: str


def _friendly_name(hass: HomeAssistant, entity_id: str) -> str:
    if (state := hass.states.get(entity_id)) is not None:
        return state.name
    entry = er.async_get(hass).async_get(entity_id)
    if entry is not None and (entry.name or entry.original_name):
        return entry.name or entry.original_name or entity_id
    return entity_id


@callback
def entity_label(hass: HomeAssistant, entity_id: str) -> str:
    """'Fan Speed: 0–6 [number.example_fan_speed]' -- name, what it offers, id."""
    domain = entity_id.split(".", 1)[0]
    name = _friendly_name(hass, entity_id)
    detail = domain
    if domain in NUMBER_DOMAINS:
        info = number_info(hass, entity_id)
        detail = f"{info.minimum:g}–{info.maximum:g}"
        if info.step not in (0, 1):
            detail += f" step {info.step:g}"
    elif domain == SELECT_DOMAIN:
        options = select_info(hass, entity_id).options
        detail = " / ".join(options[:4]) + (" / …" if len(options) > 4 else "") if options else "select"
    elif domain == FAN_DOMAIN:
        presets = fan_info(hass, entity_id).preset_modes
        detail = "fan" + (f", presets {' / '.join(presets)}" if presets else "")
    elif domain == SENSOR_DOMAIN:
        if (state := hass.states.get(entity_id)) is not None:
            unit = state.attributes.get("unit_of_measurement")
            detail = f"{state.state} {unit}".strip() if unit else state.state
    return f"{name}: {detail} [{entity_id}]"


@callback
def device_candidates(hass: HomeAssistant, power_entity_id: str) -> list[Candidate]:
    """The power entity and every other entity of its device, power first.

    Ordered by domain (fan, switch, select, number, sensor, light) then by id,
    so the dropdowns read the same for every part. Disabled entities are left
    out. An entity without a device yields itself alone.
    """
    registry = er.async_get(hass)
    ids: list[str] = [power_entity_id]
    entry = registry.async_get(power_entity_id)
    if entry is not None and entry.device_id:
        siblings = [
            e.entity_id
            for e in er.async_entries_for_device(registry, entry.device_id)
            if e.entity_id != power_entity_id and e.domain in CANDIDATE_DOMAINS
        ]
        siblings.sort(key=lambda eid: (CANDIDATE_DOMAINS.index(eid.split(".", 1)[0]), eid))
        ids.extend(siblings)
    return [Candidate(entity_id, entity_label(hass, entity_id)) for entity_id in ids]


@callback
def select_info(hass: HomeAssistant, entity_id: str | None, mapping: dict | None = None) -> SelectInfo:
    """Ordered options of a select entity."""
    if not entity_id:
        return SelectInfo()
    if mapping and mapping.get("levels"):
        return SelectInfo(tuple(mapping["levels"]))
    if mapping and "off" in mapping and "on" in mapping:
        return SelectInfo((mapping["off"], mapping["on"]))
    if (state := hass.states.get(entity_id)) is not None:
        return SelectInfo(tuple(str(o) for o in state.attributes.get(ATTR_OPTIONS) or ()))
    if (entry := er.async_get(hass).async_get(entity_id)) is not None:
        return SelectInfo(tuple(str(o) for o in (entry.capabilities or {}).get(ATTR_OPTIONS) or ()))
    return SelectInfo()


@callback
def describe_entities(hass: HomeAssistant, entity_ids: list[str]) -> list[EntityDescription]:
    registry = er.async_get(hass)
    result = []
    for entity_id in dict.fromkeys(entity_ids):
        entry = registry.async_get(entity_id)
        state = hass.states.get(entity_id)
        attrs = dict(entry.capabilities or {}) if entry else {}
        if entry:
            attrs.setdefault("supported_features", entry.supported_features or 0)
            attrs.setdefault("device_class", getattr(entry, "device_class", None) or getattr(entry, "original_device_class", None))
        if state is not None:
            attrs.update(state.attributes)
        result.append(EntityDescription(
            entity_id, _friendly_name(hass, entity_id),
            (entry.original_name or "") if entry else "", attrs,
            entry.device_id if entry else None, bool(entry.disabled_by) if entry else False,
            str(entry.entity_category) if entry and entry.entity_category else None,
        ))
    return result


@callback
def device_entity_ids(hass: HomeAssistant, device_id: str) -> list[str]:
    """Only this device and genuine child devices; never other devices on its hub."""
    from homeassistant.helpers import device_registry as dr

    devices = dr.async_get(hass)
    device_ids = {device_id}
    while True:
        children = {device.id for device in devices.devices.values()
                    if getattr(device, "parent_device_id", None) in device_ids}
        if children <= device_ids:
            break
        device_ids.update(children)
    registry = er.async_get(hass)
    return sorted(entry.entity_id for entry in registry.entities.values() if entry.device_id in device_ids)
