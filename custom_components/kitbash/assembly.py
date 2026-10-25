"""Read-only matching and reusable assembly settings, independent of HA."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .const import (
    CONF_DIRECTION,
    CONF_LIGHT,
    CONF_MODE,
    CONF_OSCILLATE,
    CONF_POWER,
    CONF_SPEED,
    CONF_TEMPERATURE,
    CONF_VALUE_MAPPINGS,
    KIND_CEILING_FAN,
    KIND_FAN,
)
from .controls import SCALAR_DOMAINS
from .model import (
    FAN_DIRECTION,
    FAN_OSCILLATE,
    FAN_PRESET_MODE,
    FAN_SET_SPEED,
)

ENTITY_FIELDS = {
    KIND_FAN: (
        CONF_POWER,
        CONF_SPEED,
        CONF_DIRECTION,
        CONF_OSCILLATE,
        CONF_MODE,
        CONF_TEMPERATURE,
    ),
    KIND_CEILING_FAN: (
        CONF_POWER,
        CONF_SPEED,
        CONF_DIRECTION,
        CONF_OSCILLATE,
        CONF_MODE,
        CONF_TEMPERATURE,
        CONF_LIGHT,
    ),
}
ROLE_WORDS = {
    "power": ("power", "on off"),
    "speed": ("speed",),
    "direction": ("direction", "reverse"),
    "oscillate": ("oscillate", "oscillation", "swing"),
    "mode": ("mode", "preset"),
    "temperature": ("temperature", "air temp", "room temp"),
    "setpoint": (
        "set temperature",
        "target temperature",
        "setpoint",
        "target setpoint",
    ),
    "heating": ("heating", "heat setpoint", "lower setpoint"),
    "cooling": ("cooling", "cool setpoint", "upper setpoint"),
    "outlet": ("outlet", "socket", "zone", "valve"),
    "lock": ("lock",),
    "contact": ("door", "contact", "window"),
    "light": ("light", "brightness", "lamp"),
    "cover": ("cover", "blind", "shade", "curtain", "position"),
    "thermostat": ("thermostat",),
    "hvac_mode": ("thermostat mode", "hvac mode"),
    "humidity": ("humidity",),
    "humidity_target": ("target humidity", "humidity target", "humidity setpoint"),
    "mute": ("mute",), "volume": ("volume",),
    "input": ("input", "source"), "remote": ("remote",),
}


def role_for(key: str, kind: str | None = None) -> str:
    return key


@dataclass(frozen=True)
class EntityDescription:
    entity_id: str
    name: str
    original_name: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)
    device_id: str | None = None
    disabled: bool = False
    category: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


@dataclass(frozen=True)
class Match:
    entity_id: str
    score: int
    reason: str


def match_entity(entity: EntityDescription, role: str) -> Match:
    attrs = entity.attributes
    hint = attrs.get("wwa_feature") or {}
    if not isinstance(hint, dict):
        hint = {}
    feature_type = str(hint.get("type") or "").rsplit(".", 1)[-1].casefold()
    writable = role not in ("temperature", "contact", "humidity")
    if (
        entity.disabled
        or entity.category == "diagnostic"
        or (writable and hint.get("writable") is False)
    ):
        return Match(entity.entity_id, 0, "Not suggested for this control")
    domain = entity.domain
    labels = tuple(re.sub(r"\s+", " ", re.sub(r"[_\-.]+", " ", str(label))).casefold().strip()
                   for label in (hint.get("label") or "", entity.original_name, entity.name))
    names = " ".join(labels)
    if role == "oscillate" and (
        hint.get("role") == "oscillation_angle"
        or "angle" in names
        or (
            feature_type in ("range", "rangecontroller")
            and hint.get("role") != "oscillate"
        )
    ):
        return Match(entity.entity_id, 0, "Oscillation angle has no implied off value")
    if role == "speed" and (
        hint.get("ordered") is False
        or (
            feature_type in ("mode", "modecontroller")
            and hint.get("ordered") is not True
        )
    ):
        return Match(entity.entity_id, 0, "Mode options have no confirmed speed order")
    if (
        role == "temperature"
        and feature_type in ("thermostat", "thermostatcontroller")
        and hint.get("role") != "temperature"
    ):
        return Match(
            entity.entity_id,
            0,
            "Thermostat feature does not identify a temperature reading",
        )
    allowed = {
        "power": ("fan", "switch", "input_boolean", *SCALAR_DOMAINS),
        "speed": ("fan", *SCALAR_DOMAINS),
        "direction": ("fan", "switch", "input_boolean", *SCALAR_DOMAINS),
        "oscillate": ("fan", "switch", "input_boolean", *SCALAR_DOMAINS),
        "mode": ("fan", "switch", "input_boolean", *SCALAR_DOMAINS),
        "temperature": ("sensor", *SCALAR_DOMAINS),
        "contact": ("binary_sensor", "sensor", "input_boolean", *SCALAR_DOMAINS),
        "lock": ("lock", "switch", "input_boolean", *SCALAR_DOMAINS),
        "light": ("light", *SCALAR_DOMAINS),
        "cover": ("cover", *SCALAR_DOMAINS),
        "thermostat": ("climate", "water_heater", *SCALAR_DOMAINS),
        "outlet": ("switch", "input_boolean", *SCALAR_DOMAINS),
        "child_lock": ("switch", "input_boolean", *SCALAR_DOMAINS),
        "hvac_mode": ("climate", *SCALAR_DOMAINS),
        "humidity": ("sensor", "climate", "humidifier", *SCALAR_DOMAINS),
        "humidity_target": ("humidifier", *SCALAR_DOMAINS),
        "mute": ("media_player", "switch", "input_boolean", *SCALAR_DOMAINS),
        "volume": ("media_player", *SCALAR_DOMAINS),
        "input": ("media_player", *SCALAR_DOMAINS),
        "remote": ("remote", *SCALAR_DOMAINS),
    }.get(role, SCALAR_DOMAINS)
    if domain not in allowed:
        return Match(entity.entity_id, 0, "Choose manually if appropriate")
    feature_role = hint.get("role")
    if feature_type in ("thermostat", "thermostatcontroller") and feature_role in (
        None,
        "setpoint",
    ):
        # Alexa keeps the parent feature key/role for each split setpoint.
        # The property identifies the actual heating or cooling threshold.
        feature_role = {
            "targetSetpoint": "setpoint",
            "lowerSetpoint": "heating",
            "upperSetpoint": "cooling",
            "thermostatMode": "hvac_mode",
        }.get(hint.get("property"), feature_role)
    if feature_type in ("thermostat", "thermostatcontroller") and hint.get("property") == "thermostatMode":
        feature_role = "hvac_mode"
    if not feature_role:
        feature_role = {
            "power": "power",
            "powercontroller": "power",
            "lockcontroller": "lock",
            "temperaturesensor": "temperature",
            "contactsensor": "contact",
            "contact": "contact",
        }.get(feature_type)
    if feature_role == role:
        return Match(entity.entity_id, 120, "Integration feature metadata")
    if feature_role and feature_role != role:
        return Match(entity.entity_id, 10, "Integration describes another feature")
    native = {"power": "fan", "lock": "lock", "cover": "cover", "light": "light"}
    if domain == native.get(role):
        return Match(entity.entity_id, 110, f"Native {domain} control")
    if role == "thermostat" and domain in ("climate", "water_heater"):
        return Match(entity.entity_id, 110, "Native thermostat capabilities")
    features = int(attrs.get("supported_features") or 0)
    bit = {
        "speed": FAN_SET_SPEED,
        "direction": FAN_DIRECTION,
        "oscillate": FAN_OSCILLATE,
        "mode": FAN_PRESET_MODE,
    }.get(role, 0)
    if domain == "fan" and bit and features & bit:
        return Match(entity.entity_id, 110, "Fan supports this feature")
    if (
        role == "temperature"
        and attrs.get("device_class") == "temperature"
        and domain == "sensor"
    ):
        return Match(entity.entity_id, 105, "Temperature device class")
    if role == "contact" and attrs.get("device_class") in (
        "door",
        "window",
        "opening",
        "garage_door",
    ):
        return Match(entity.entity_id, 105, "Door/window contact device class")
    if role == "temperature" and any(
        word in names for word in ("setpoint", "set temperature", "target", "threshold")
    ):
        return Match(entity.entity_id, 0, "Target rather than a temperature reading")
    if role == "mode":
        # Some integrations expose Auto/Manual as a toggle named just Auto.
        # Match the feature label, not arbitrary uses of 'auto' (auto lock,
        # auto shutoff, etc.). Explicit integration roles already won above.
        if domain in ("switch", "input_boolean", *SCALAR_DOMAINS) and any(re.fullmatch(r"(?:fan )?(?:auto|automatic)(?: mode)?", label)
               or re.search(r"\bfan (?:auto|automatic)(?: mode)?$", label)
               or re.fullmatch(r"(?:auto|automatic)(?:\s*/\s*| )manual", label) for label in labels):
            return Match(entity.entity_id, 85, "Auto/Manual control name")
        if domain == "select":
            options = {str(option).strip().casefold() for option in attrs.get("options") or ()}
            if len(options) == 2 and options & {"auto", "automatic"} and options & {"manual", "normal"}:
                return Match(entity.entity_id, 85, "Select offers Auto and Manual")
    if role == "child_lock":
        words = ("child lock", "control lock", "keypad lock")
    else:
        words = ROLE_WORDS.get(role, ())
    if any(re.search(r"\b" + re.escape(word) + r"\b", names) for word in words):
        return Match(entity.entity_id, 85, "Feature name matches this part")
    return Match(entity.entity_id, 10, "Available for manual selection")


def ranked_matches(entities: list[EntityDescription], role: str) -> list[Match]:
    return sorted(
        (match_entity(e, role) for e in entities), key=lambda m: (-m.score, m.entity_id)
    )


def suggest_parts(
    kind: str, entities: list[EntityDescription]
) -> tuple[dict[str, Any], dict[str, str]]:
    suggested, reasons = {}, {}
    for key in ENTITY_FIELDS[kind]:
        candidates = entities
        ranked = ranked_matches(candidates, role_for(key, kind))
        if (
            ranked
            and ranked[0].score >= 80
            and (len(ranked) == 1 or ranked[0].score - ranked[1].score >= 10)
        ):
            suggested[key] = ranked[0].entity_id
            reasons[key] = ranked[0].reason
    return suggested, reasons


def suggest_kind(entities: list[EntityDescription]) -> str | None:
    domains = {e.domain for e in entities if not e.disabled}
    fan = "fan" in domains or any(
        match_entity(e, "speed").score >= 80 for e in entities
    )
    if fan:
        return (
            KIND_CEILING_FAN
            if any(match_entity(e, "light").score >= 80 for e in entities)
            else KIND_FAN
        )
    return None


def part_bindings(kind: str, options: dict[str, Any]) -> dict[str, str]:
    bindings = {}
    for key in ENTITY_FIELDS[kind]:
        value = options.get(key)
        if value:
            bindings[key] = value
    return bindings


def clean_mappings(kind: str, options: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bindings = part_bindings(kind, options)
    return {
        key: dict(value)
        for key, value in (options.get(CONF_VALUE_MAPPINGS) or {}).items()
        if isinstance(value, dict) and bindings.get(key) == value.get("entity_id")
    }


def mapping_for(options: dict[str, Any], key: str) -> dict[str, Any]:
    mapping = (options.get(CONF_VALUE_MAPPINGS) or {}).get(key, {})
    if not isinstance(mapping, dict):
        return {}
    entity_id = mapping.get("entity_id")
    field_name = key.split(":", 1)[0]
    bound = options.get(field_name)
    if bound == entity_id or (isinstance(bound, list) and entity_id in bound):
        return mapping
    return {}
