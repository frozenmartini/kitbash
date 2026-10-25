"""Associate controls and derived readings with their underlying HA sources."""

from __future__ import annotations

from typing import Any

from .model import oscillate_to_homekit, speed_to_homekit
from .recovery import finite


def configure_recovery(accessory: Any) -> None:
    bind, depend = accessory._recovery.bind, accessory._recovery.depend

    def light(entity: str | None) -> None:
        bind(entity, "light", (accessory.char_light_on,))
        bind(entity, "light", (accessory.char_brightness,), numeric=True, standby=entity, standby_role="light",
             attribute="brightness" if entity and entity.startswith("light.") else None)

    def speed(source: Any, power: str | None, char: Any, power_role: str = "power") -> None:
        if source is not None:
            bind(source.entity_id, "speed", (char,), standby=power, setting=True, standby_role=power_role,
                 validator=lambda state: (
                     (source.domain != "fan" or finite(state.attributes.get("percentage")))
                     and speed_to_homekit(source, state.attributes.get("percentage")
                         if source.domain == "fan" else state.state) is not None))

    def swing(source: Any, power: str | None, char: Any, role: str, power_role: str = "power") -> None:
        if source is not None:
            bind(source.entity_id, role, (char,), standby=power, setting=True, standby_role=power_role,
                 validator=lambda state: (source.domain != "fan" or type(state.attributes.get("oscillating")) is bool)
                 and (
                     accessory._control(state, role).is_on(state.state) is not None
                     if accessory._mapping(source.entity_id, role)
                     else oscillate_to_homekit(source, state.attributes.get("oscillating")
                         if source.domain == "fan" else state.state) is not None))

    if hasattr(accessory, "fan"):
        fan = accessory.fan
        power = fan.power_entity_id
        bind(power, "power", (accessory.char_active,))
        speed(fan.speed, power, accessory.char_speed)
        swing(fan.oscillate, power, accessory.char_swing, "oscillate")
        if fan.mode:
            bind(fan.mode.entity_id, "mode", (accessory.char_target,), setting=True,
                 standby=power, validator=(lambda state: (
                     state.attributes.get("preset_mode") not in ("unknown", "unavailable")
                     and (state.attributes.get("preset_mode") is not None or state.state == "on")
                 )) if fan.mode.domain == "fan" else None)
        # Direction retains its separate durable, confirmed-write controller.
        bind(fan.temperature_entity_id, "temperature", (accessory.char_temperature,), numeric=True)
        light(fan.light_entity_id)
        for char in accessory.speed_switches:
            depend(char, (accessory.char_active, accessory.char_speed))
            accessory._recovery.inactive(char, (accessory.char_active,))
