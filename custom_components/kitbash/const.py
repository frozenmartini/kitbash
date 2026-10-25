"""Constants for the Kitbash integration.

Deliberately no Home Assistant imports here: ``const.py`` and ``model.py`` stay
importable outside a Home Assistant install so the pure-logic tests run anywhere.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "kitbash"
VERSION: Final = "0.1.0"
MANUFACTURER: Final = "Kitbash"

# --- Config entry layout ----------------------------------------------------------
# ``data`` holds what never changes after creation; ``options`` holds what the
# options flow may edit. Editing options reloads the entry, which rebuilds the
# accessory; HAP-python then bumps the config number so the Home app refetches.
CONF_KIND: Final = "kind"
CONF_VALUE_MAPPINGS: Final = "value_mappings"
CONF_SOURCE_ENTITIES: Final = "source_entities"
CONF_PORT: Final = "port"
CONF_DEV_MODE: Final = "dev_mode"  # configuration.yaml `kitbash: dev_mode: true` -> trace log
DATA_DEV_MODE: Final = "dev_mode"  # hass.data[DOMAIN] key

CONF_FAN: Final = "fan"  # options (legacy, entries made before the parts): fan entity id
CONF_POWER: Final = "power"  # options: fan or switch entity that is on/off
CONF_SPEED: Final = "speed"  # options: fan, number or select entity for RotationSpeed
CONF_DIRECTION: Final = "direction"  # options: fan, select or switch entity for RotationDirection
CONF_DIRECTION_REVERSED: Final = "direction_reversed"  # options: swap clockwise / counter-clockwise
CONF_OSCILLATE: Final = "oscillate"  # options: fan or switch entity for SwingMode
CONF_MODE: Final = "mode"  # options: fan (presets) or select entity behind TargetFanState
CONF_LIGHT: Final = "light"  # options: light entity id or absent (ceiling fan)
CONF_TEMPERATURE: Final = "temperature"  # options: sensor entity shown as a TemperatureSensor service
CONF_NAMED_SPEEDS: Final = "named_speeds"  # options: one Switch service per speed, named
CONF_AUTO_PRESET: Final = "auto_preset"  # options: mode preset/option that means TargetFanState Auto
CONF_MANUAL_PRESET: Final = "manual_preset"  # options: mode preset/option that means Manual

KIND_CEILING_FAN: Final = "ceiling_fan"
KIND_FAN: Final = "fan"  # Fanv2 alone, no light: for checking what Home shows of the fan service
KINDS: Final = (
    KIND_CEILING_FAN,
    KIND_FAN,
)

# One HAP server per config entry, each its own standalone accessory. Ports are
# allocated at flow time from here upwards, skipping any other Kitbash entry's.
# Home Assistant's own HomeKit bridge starts at 21063; stay well clear of it.
DEFAULT_PORT_START: Final = 21200
MAX_PORT: Final = 65535

# HAP-python state (pairings, keys, config number) per entry, in HA's .storage.
PERSIST_FILE_TEMPLATE: Final = "kitbash.{entry_id}.state"

# HomeKit limits. Names longer than this crash some clients.
MAX_NAME_LENGTH: Final = 64

# A number with more levels than this (0-127 or 1-256 MQTT levels, say) is
# offered only as a speed source in the flow's dropdowns, never as a
# direction/oscillate/mode candidate.
MAX_DROPDOWN_LEVELS: Final = 12

# Named speeds cost one extra Switch service each (HA's bridge does the same for
# preset modes), so only a source with few enough levels to be worth naming may
# have them: a 0-127 number would bury the tile and eat the 100-service budget.
MAX_NAMED_SPEEDS: Final = 12

MODEL_CEILING_FAN: Final = "Ceiling fan"
MODEL_FAN: Final = "Fan"

# Dispatcher signal carrying each HomeKit write to the diagnostic sensor.
SIGNAL_WRITE: Final = "kitbash_write_{entry_id}"
