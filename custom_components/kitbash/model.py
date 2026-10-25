"""Pure description of what Kitbash publishes to HomeKit.

No Home Assistant or HAP-python imports: everything here is data and validation,
pinned by ``tests/test_model.py`` with the stdlib alone. The HAP side
(``hap.py``) turns these models into services; the config flow builds them from
user input.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .controls import SCALAR_DOMAINS

from .const import (
    MAX_NAME_LENGTH,
    MAX_NAMED_SPEEDS,
    MAX_PORT,
)

SWITCH_DOMAIN = "switch"
INPUT_BOOLEAN_DOMAIN = "input_boolean"
# Native toggles and scalar controls can all supply an on/off part. The runtime
# adapter translates range endpoints and select options into boolean state.
TOGGLE_DOMAINS = (SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN, *SCALAR_DOMAINS)


class ModelError(ValueError):
    """A model cannot be built from the given input.

    ``code`` is the error key the config flow shows (see ``strings.json``).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# --- Names ---------------------------------------------------------------------
# HAP names: letters, digits, space and a little punctuation, starting and
# ending on a letter or digit. Anything else is replaced by a space. This mirrors
# the intent of Home Assistant's own ``cleanup_name_for_homekit``.
_NAME_ALLOWED = re.compile(r"[^0-9A-Za-z ,.'\-]")
_NAME_EDGES = "-,. '"
_SPACES = re.compile(r" {2,}")


def cleanup_name(name: str | None, fallback: str = "Kitbash") -> str:
    """Return a name HomeKit accepts, never empty."""
    if not name:
        return fallback
    cleaned = _SPACES.sub(" ", _NAME_ALLOWED.sub(" ", name)).strip()
    cleaned = cleaned.lstrip(_NAME_EDGES)[:MAX_NAME_LENGTH].rstrip(_NAME_EDGES)
    return cleaned or fallback


def unique_names(names: Iterable[str]) -> list[str]:
    """Make labels distinct in order: 'Outlet', 'Outlet' -> 'Outlet', 'Outlet 2'."""
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        key = name.casefold()
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count == 1:
            result.append(name)
            continue
        candidate = f"{name} {count}"
        while candidate.casefold() in seen:
            count += 1
            candidate = f"{name} {count}"
        seen[candidate.casefold()] = 1
        result.append(candidate[:MAX_NAME_LENGTH])
    return result


def default_label(entity_id: str) -> str:
    """Readable fallback from an entity id: switch.strip_outlet_2 -> 'Strip Outlet 2'."""
    object_id = entity_id.split(".", 1)[-1]
    return " ".join(part.capitalize() for part in object_id.split("_") if part)


# --- Ports ---------------------------------------------------------------------
def find_free_port(
    start: int,
    exclude: Iterable[int],
    is_free: Callable[[int], bool],
) -> int:
    """First port >= start that is neither excluded nor refused by ``is_free``.

    ``is_free`` is injected so the search is testable without sockets; the
    integration passes a real bind probe.
    """
    excluded = set(exclude)
    for port in range(start, MAX_PORT + 1):
        if port in excluded:
            continue
        if is_free(port):
            return port
    raise ModelError("no_free_port", "no free TCP port left")


# --- Ceiling fan -------------------------------------------------------------------
# Home Assistant's FanEntityFeature bits (homeassistant/components/fan/__init__.py).
FAN_SET_SPEED = 1
FAN_OSCILLATE = 2
FAN_DIRECTION = 4
FAN_PRESET_MODE = 8

FAN_DOMAIN = "fan"
LIGHT_DOMAIN = "light"


@dataclass(frozen=True)
class FanInfo:
    """What the source fan entity can do, as read from its state or registry row."""

    supported_features: int = 0
    preset_modes: tuple[str, ...] = ()
    percentage_step: float = 1.0


@dataclass(frozen=True)
class LightInfo:
    """What the source light entity can do."""

    brightness: bool = False


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _trim(value: float | str) -> str:
    """A level as a label: 3.0 -> "3", 2.5 -> "2.5"."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number == int(number) else str(number)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


# --- Assembled fan: power / speed / direction / oscillate from any entities -------------
# The fan kind is built from parts. Each part names an entity and how to read and
# write it; ``fan`` entities carry their own percentage/direction/oscillation,
# ``switch``/``number``/``select`` entities are raw features (as works_with_alexa
# exposes them). Everything here is pure so the mapping is pinned by tests.

NUMBER_DOMAIN = "number"
INPUT_NUMBER_DOMAIN = "input_number"
# Both carry min/max/step as attributes and take ``set_value`` with a ``value``
# in their own domain, so anything that only needs a settable number takes
# either. An input_number is how a setpoint gets tested without a device.
NUMBER_DOMAINS = (NUMBER_DOMAIN, INPUT_NUMBER_DOMAIN)
SELECT_DOMAIN = "select"
OSCILLATE_DOMAINS = (FAN_DOMAIN, *TOGGLE_DOMAINS)
SENSOR_DOMAIN = "sensor"
# Anything that can stand in for a thermometer: a real sensor, or an
# input_number helper standing in for one while a shape is being tested.
TEMPERATURE_DOMAINS = (SENSOR_DOMAIN, *SCALAR_DOMAINS)


@dataclass(frozen=True)
class NumberInfo:
    """Range of a number entity, and the unit it reports in (if any)."""

    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 1.0
    unit: str | None = None
    levels: tuple[float, ...] = ()


@dataclass(frozen=True)
class SelectInfo:
    """Ordered options of a select entity."""

    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class SpeedSource:
    """Where RotationSpeed comes from.

    RotationSpeed is always published as a percentage: HAP has no other unit
    for it, and the Home app labels the slider "%" whatever range is given.
    A number or select is treated as an ordered list of ``levels`` above off
    and mapped onto 0-100 automatically: number 0..6 step 1 -> levels
    1,2,3,4,5,6 (0 is off) -> 17/33/50/67/83/100; select Low/Medium/High/Turbo
    -> 25/50/75/100; a 0..127 number -> 127 levels on a one-percent grid.
    A fan entity keeps its own percentage and step.
    """

    entity_id: str
    domain: str
    levels: tuple[float | str, ...]  # number values or select options, low -> high
    percentage_step: float = 1.0  # fan entities only

    @property
    def count(self) -> int:
        """How many speeds above off."""
        if self.domain == FAN_DOMAIN:
            return max(1, round(100 / self.percentage_step)) if self.percentage_step else 100
        return len(self.levels)

    @property
    def homekit_props(self) -> dict[str, float]:
        """minValue / maxValue / minStep to publish for RotationSpeed.

        One detent per level, never finer than one percent: a 127-level number
        gets a whole-percent slider rather than a 0.79 grid.
        """
        step = self.percentage_step if self.domain == FAN_DOMAIN else 100 / self.count
        return {"minValue": 0, "maxValue": 100, "minStep": max(1.0, step)}

    @property
    def names(self) -> tuple[str, ...]:
        """A label per level, low -> high, for the optional named-speed switches.

        A select already has the names the user chose (Low, Medium, High); a
        number is labelled by the value it writes; a fan has only percentages,
        so its levels are numbered.
        """
        if self.domain == SELECT_DOMAIN:
            return tuple(str(level) for level in self.levels)
        if self.domain in NUMBER_DOMAINS:
            return tuple(f"Speed {_trim(level)}" for level in self.levels)
        return tuple(f"Speed {index + 1}" for index in range(self.count))


@dataclass(frozen=True)
class DirectionSource:
    """Where RotationDirection comes from. HAP: 0 = clockwise, 1 = counter-clockwise.

    fan: its ``direction`` attribute, forward = clockwise.
    select: its first option = clockwise, its second = counter-clockwise.
    switch: off = clockwise, on = counter-clockwise.
    ``reversed`` swaps the two, for when the Home app's arrow shows the
    opposite of what the blades do.
    """

    entity_id: str
    domain: str
    reversed: bool = False
    options: tuple[str, str] | None = None  # select only: (clockwise, counter) before reversal


@dataclass(frozen=True)
class OscillateSource:
    """SwingMode binding: toggles, numeric min/max, or select off/on options.

    Other numeric values above the minimum and other select options mean on.
    """

    entity_id: str
    domain: str
    off: float | str = 0.0
    on: float | str = 1.0
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModeSource:
    """Where TargetFanState comes from: a fan's ``preset_mode``, a select or a switch.

    ``auto`` is the preset/option that means Auto; ``manual`` the one that
    means Manual. A fan may leave ``manual`` out (Manual then re-asserts the
    current speed, which drops the preset); a select must name both; a switch
    is fixed at on = Auto, off = Manual (the Vornado's "Auto" switch).
    """

    entity_id: str
    domain: str
    auto: str
    manual: str | None = None


@dataclass(frozen=True)
class AssembledFan:
    """One accessory: a Fanv2 from parts, plus a Lightbulb when a light is given
    and a TemperatureSensor when a temperature sensor is given."""

    name: str
    power_entity_id: str  # fan or switch
    speed: SpeedSource | None
    direction: DirectionSource | None
    oscillate: OscillateSource | None
    mode: ModeSource | None
    light_entity_id: str | None
    brightness: bool
    temperature_entity_id: str | None = None  # sensor entity, any temperature unit
    named_speeds: bool = False  # one Switch service per speed, linked to the Fanv2

    @property
    def power_is_fan(self) -> bool:
        return _domain_of(self.power_entity_id) == FAN_DOMAIN

    @property
    def entity_ids(self) -> tuple[str, ...]:
        """Every source entity, each once, power first."""
        ids = [self.power_entity_id]
        for part in (self.speed, self.direction, self.oscillate, self.mode):
            if part and part.entity_id not in ids:
                ids.append(part.entity_id)
        for entity_id in (self.light_entity_id, self.temperature_entity_id):
            if entity_id and entity_id not in ids:
                ids.append(entity_id)
        return tuple(ids)

    @property
    def speed_names(self) -> tuple[str, ...]:
        """The named-speed labels to publish, low -> high; empty when not asked for."""
        if not self.named_speeds or self.speed is None:
            return ()
        return tuple(
            cleanup_name(name, fallback=f"Speed {index + 1}")[:MAX_NAME_LENGTH]
            for index, name in enumerate(self.speed.names)
        )

    @property
    def light_name(self) -> str:
        return cleanup_name(f"{self.name} Light")[:MAX_NAME_LENGTH]

    @property
    def temperature_name(self) -> str:
        return cleanup_name(f"{self.name} Temperature")[:MAX_NAME_LENGTH]


# --- level <-> percentage, Home Assistant's fan convention ---------------------------------
def level_to_percentage(count: int, index: int) -> int:
    """Level ``index`` (0-based) of ``count`` levels as a percentage, HA style.

    Never 0: with more than 100 levels the lowest would floor to 0, which
    HomeKit reads as off.
    """
    return max(1, ((index + 1) * 100) // count)


# Half a percent of tolerance on the band edge. The Home app does not need it --
# measured 2026-09-13 across four accessories, it sends the exact detent
# (8.333333333333334, 16.666666666666668, 66.66666666666667) and never rounds --
# but a controller that sent 67 for the middle of three would otherwise land a
# level too high, and no exact detent can be mis-assigned by half a percent.
# Sources with more levels than percentages are already on a one-percent slider,
# where the same tolerance would just bias every value upward, so they get none.
def _slop(count: int) -> float:
    return 0.5 if count <= 100 else 0.0


def percentage_to_level(count: int, percentage: float) -> int:
    """Percentage -> level index (0-based), HA style: 1-25 low, 26-50 medium…

    The band edges are exact (100/3 = 33.33…), not the floored percentage
    ``level_to_percentage`` publishes, so rounding at either end stays inside
    the level it came from. They are clamped to 1 the same way, so on a source
    with more levels than percentages 1% still means the lowest speed.
    """
    slop = _slop(count)
    for index in range(count):
        if percentage <= max(1.0, (index + 1) * 100 / count) + slop:
            return index
    return count - 1


def number_levels(info: NumberInfo) -> tuple[float, ...]:
    """The speeds a number entity offers above off.

    A range starting at 0 treats 0 as off; the first level is one step up.
    A range starting above 0 (1..256) has no off value: the lowest is level 1.
    """
    step = info.step if info.step > 0 else 1.0
    low = info.minimum if info.minimum > 0 else step
    if info.maximum < low:
        return (low,)
    count = int(round((info.maximum - low) / step)) + 1
    return tuple(round(low + i * step, 10) for i in range(count))


def speed_to_homekit(source: SpeedSource, raw: Any) -> float | None:
    """HA value of the speed entity -> RotationSpeed percentage, or None to leave it.

    ``raw`` is the fan's ``percentage`` attribute, the number's state, or the
    select's state. Off/unknown/sentinel values return None so the stored
    HomeKit speed survives an off period (HomeKit restores it on turn-on).
    """
    if raw is None:
        return None
    if source.domain == FAN_DOMAIN:
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            return None
        return pct if pct > 0 else None
    if source.domain in NUMBER_DOMAINS:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0 or not source.levels:
            return None
        index = min(
            range(len(source.levels)),
            key=lambda i: abs(float(source.levels[i]) - value),
        )
    else:
        if str(raw) not in source.levels:
            return None
        index = source.levels.index(str(raw))
    return level_to_percentage(source.count, index)


def homekit_to_speed(source: SpeedSource, value: float) -> float | str | None:
    """RotationSpeed percentage written by HomeKit -> what to write to the speed entity.

    None means "off" (0 was written); the caller turns the power off instead.
    """
    if value is None or value <= 0:
        return None
    if source.domain == FAN_DOMAIN:
        # Passed through untouched: the controller sends the exact detent
        # (66.66666666666667 for the middle of three, measured 2026-09-13) and
        # the fan entity owns its own level maths -- a three-speed fan takes
        # that value and reports 66 back. Snapping it here would second-guess a fan
        # that already knows its own speeds.
        return float(value)
    if not source.levels:
        return None
    return source.levels[percentage_to_level(source.count, value)]


def direction_to_homekit(source: DirectionSource, raw: Any) -> int | None:
    """HA state/attribute of the direction part -> 0 clockwise / 1 counter, or None."""
    if raw is None:
        return None
    if source.domain == FAN_DOMAIN:
        value = {"forward": 0, "reverse": 1}.get(str(raw))
    elif source.domain == SELECT_DOMAIN:
        options = source.options or ()
        value = options.index(raw) if raw in options else None
    else:
        value = {"off": 0, "on": 1}.get(str(raw))
    if value is None:
        return None
    return value ^ 1 if source.reversed else value


def direction_from_homekit(source: DirectionSource, value: int) -> str | bool:
    """RotationDirection written by HomeKit -> what to write to the direction part.

    fan: ``forward``/``reverse``; select: the option; switch: True for on.
    """
    counter = (int(value) == 1) != source.reversed
    if source.domain == FAN_DOMAIN:
        return "reverse" if counter else "forward"
    if source.domain == SELECT_DOMAIN:
        options = source.options or ("", "")
        return options[1] if counter else options[0]
    return counter


# --- Temperature -----------------------------------------------------------------------
def temperature_to_celsius(raw: Any, unit: str | None) -> float | None:
    """A sensor's state and unit -> degrees Celsius for CurrentTemperature, or None.

    Accepts °C, °F and K (Home Assistant's UnitOfTemperature); anything else is
    taken as Celsius already. Non-numeric states (unknown, unavailable) give None.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    scale = (unit or "").strip().upper().replace("°", "")
    if scale == "F":
        value = (value - 32) * 5 / 9
    elif scale == "K":
        value = value - 273.15
    return round(value, 1)


# --- Building ------------------------------------------------------------------
def _build_speed(
    entity_id: str | None, fan_info: FanInfo,
    number_info: NumberInfo | None, select_info: SelectInfo | None,
) -> SpeedSource | None:
    entity_id = _clean_optional(entity_id)
    if not entity_id:
        return None
    domain = _domain_of(entity_id)
    if domain == FAN_DOMAIN:
        step = float(fan_info.percentage_step or 1.0)
        if not 0 < step <= 100:
            step = 1.0
        return SpeedSource(entity_id, domain, (), percentage_step=step)
    if domain in NUMBER_DOMAINS:
        info = number_info or NumberInfo()
        return SpeedSource(entity_id, domain, info.levels or number_levels(info))
    if domain == SELECT_DOMAIN:
        options = tuple((select_info or SelectInfo()).options)
        if not options:
            raise ModelError("select_without_options", f"{entity_id} lists no options yet")
        return SpeedSource(entity_id, domain, options)
    raise ModelError("bad_speed_source", f"{entity_id} cannot provide a speed")


def _build_direction(
    entity_id: str | None, reversed_: bool, select_info: SelectInfo | None,
) -> DirectionSource | None:
    entity_id = _clean_optional(entity_id)
    if not entity_id:
        return None
    domain = _domain_of(entity_id)
    if domain in (FAN_DOMAIN, *TOGGLE_DOMAINS) and domain != SELECT_DOMAIN:
        return DirectionSource(entity_id, domain, bool(reversed_))
    if domain != SELECT_DOMAIN:
        raise ModelError("bad_direction_source", f"{entity_id} cannot provide a direction")
    options = tuple((select_info or SelectInfo()).options)
    if len(options) < 2:
        raise ModelError("direction_options", f"{entity_id} needs two options for the directions")
    return DirectionSource(entity_id, domain, bool(reversed_), (options[0], options[1]))


def oscillate_to_homekit(source: OscillateSource, raw: Any) -> int | None:
    """Read a source without treating missing or invalid values as off."""
    if source.domain == FAN_DOMAIN:
        return int(raw) if isinstance(raw, bool) else None
    if source.domain in NUMBER_DOMAINS:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not float(source.off) <= value <= float(source.on):
            return None
        return int(value > float(source.off))
    if source.domain == SELECT_DOMAIN:
        return int(raw != source.off) if raw in source.options else None
    return {"off": 0, "on": 1}.get(raw) if isinstance(raw, str) else None


def oscillate_from_homekit(source: OscillateSource, value: int) -> float | str | bool:
    """Return the source value that enables or disables oscillation."""
    on = value == 1
    if source.domain in (*NUMBER_DOMAINS, SELECT_DOMAIN):
        return source.on if on else source.off
    return on


def _build_oscillate(
    entity_id: str | None,
    number_info: NumberInfo | None = None,
    select_info: SelectInfo | None = None,
) -> OscillateSource | None:
    entity_id = _clean_optional(entity_id)
    if not entity_id:
        return None
    domain = _domain_of(entity_id)
    if domain not in OSCILLATE_DOMAINS:
        raise ModelError("bad_oscillate_source", f"{entity_id} cannot provide oscillation")
    if domain in NUMBER_DOMAINS:
        info = number_info or NumberInfo()
        if not float("-inf") < info.minimum < info.maximum < float("inf"):
            raise ModelError("bad_oscillate_source", f"{entity_id} needs a finite range")
        return OscillateSource(entity_id, domain, info.minimum, info.maximum)
    if domain == SELECT_DOMAIN:
        options = tuple(dict.fromkeys((select_info or SelectInfo()).options))
        if len(options) < 2:
            raise ModelError("bad_oscillate_source", f"{entity_id} needs off and on options")
        off = next(
            (option for option in options if option.strip().casefold() in
             ("off", "disabled", "disable", "false", "0", "none", "stop", "stopped")),
            options[0],
        )
        on = next(option for option in options if option != off)
        return OscillateSource(entity_id, domain, off, on, options)
    return OscillateSource(entity_id, domain)


def _build_mode(
    entity_id: str | None, auto: str | None, manual: str | None,
    options: tuple[str, ...] | None,
) -> ModeSource | None:
    """The Auto/Manual binding, or None when no Auto option is named."""
    entity_id = _clean_optional(entity_id)
    auto = _clean_optional(auto)
    manual = _clean_optional(manual)
    if entity_id is None:
        if auto or manual:
            raise ModelError("mode_needs_source", "an Auto option needs a mode entity")
        return None
    domain = _domain_of(entity_id)
    if domain in (SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN):
        return ModeSource(entity_id, domain, "on", "off")
    if domain not in (FAN_DOMAIN, *SCALAR_DOMAINS):
        raise ModelError("bad_mode_source", f"{entity_id} cannot provide a mode")
    if manual and not auto:
        raise ModelError("manual_needs_auto", "a manual option needs an auto option")
    if not auto:
        return None
    if auto == manual:
        raise ModelError("same_preset", "auto and manual options must differ")
    if domain in SCALAR_DOMAINS and not manual:
        raise ModelError("select_needs_manual", "a select mode needs a manual option too")
    for option in (auto, manual):
        if option and options and domain not in NUMBER_DOMAINS and option not in options:
            raise ModelError("unknown_preset", f"{option!r} is not an option of {entity_id}")
    return ModeSource(entity_id, domain, auto, manual)


def build_assembled_fan(
    name: str | None,
    power_entity_id: str | None,
    *,
    speed_entity_id: str | None = None,
    direction_entity_id: str | None = None,
    direction_reversed: bool = False,
    oscillate_entity_id: str | None = None,
    mode_entity_id: str | None = None,
    auto_option: str | None = None,
    manual_option: str | None = None,
    light_entity_id: str | None = None,
    temperature_entity_id: str | None = None,
    named_speeds: bool = False,
    fan_info: FanInfo | None = None,
    light_info: LightInfo | None = None,
    number_info: NumberInfo | None = None,
    select_info: SelectInfo | None = None,
    direction_select_info: SelectInfo | None = None,
    oscillate_number_info: NumberInfo | None = None,
    oscillate_select_info: SelectInfo | None = None,
    mode_options: tuple[str, ...] | None = None,
) -> AssembledFan:
    """Validate the parts and return the fan to publish.

    With a ``fan`` entity as power and no explicit speed/direction/oscillate/
    mode part, the fan's own features fill those in, so a plain fan entity
    needs nothing else. ``mode_options`` are the presets or select options of
    the mode entity when it is not the power fan (whose ``fan_info`` is used).
    ``named_speeds`` adds one Switch service per speed level; it needs a speed
    part with few enough levels to be worth naming.
    """
    power_entity_id = _clean_optional(power_entity_id)
    if not power_entity_id or _domain_of(power_entity_id) not in (FAN_DOMAIN, *TOGGLE_DOMAINS):
        raise ModelError("not_a_fan", f"{power_entity_id} is not a fan or switch entity")
    light_entity_id = _clean_optional(light_entity_id)
    if light_entity_id is not None and _domain_of(light_entity_id) not in (LIGHT_DOMAIN, *SCALAR_DOMAINS):
        raise ModelError("not_a_light", f"{light_entity_id} is not a light entity")
    temperature_entity_id = _clean_optional(temperature_entity_id)
    if (
        temperature_entity_id is not None
        and _domain_of(temperature_entity_id) not in TEMPERATURE_DOMAINS
    ):
        raise ModelError(
            "not_a_temperature",
            f"{temperature_entity_id} is not a sensor or input_number entity",
        )

    fan_info = fan_info or FanInfo()
    light_info = light_info or LightInfo()
    power_is_fan = _domain_of(power_entity_id) == FAN_DOMAIN
    features = int(fan_info.supported_features or 0) if power_is_fan else 0

    speed_entity_id = _clean_optional(speed_entity_id)
    if speed_entity_id is None and power_is_fan and features & FAN_SET_SPEED:
        speed_entity_id = power_entity_id
    direction_entity_id = _clean_optional(direction_entity_id)
    if direction_entity_id is None and power_is_fan and features & FAN_DIRECTION:
        direction_entity_id = power_entity_id
    oscillate_entity_id = _clean_optional(oscillate_entity_id)
    if oscillate_entity_id is None and power_is_fan and features & FAN_OSCILLATE:
        oscillate_entity_id = power_entity_id
    mode_entity_id = _clean_optional(mode_entity_id)
    if mode_entity_id is None and power_is_fan and features & FAN_PRESET_MODE:
        mode_entity_id = power_entity_id
    if mode_entity_id == power_entity_id and power_is_fan:
        mode_options = tuple(fan_info.preset_modes)

    speed = _build_speed(speed_entity_id, fan_info, number_info, select_info)
    named_speeds = bool(named_speeds)
    if named_speeds:
        if speed is None:
            raise ModelError("named_speeds_need_speed", "naming speeds needs a speed part")
        if speed.count > MAX_NAMED_SPEEDS:
            raise ModelError(
                "too_many_speeds",
                f"{speed.count} speeds is too many to name (at most {MAX_NAMED_SPEEDS})",
            )

    return AssembledFan(
        name=cleanup_name(name),
        power_entity_id=power_entity_id,
        speed=speed,
        direction=_build_direction(direction_entity_id, direction_reversed, direction_select_info),
        oscillate=_build_oscillate(
            oscillate_entity_id, oscillate_number_info, oscillate_select_info
        ),
        mode=_build_mode(mode_entity_id, auto_option, manual_option, mode_options),
        light_entity_id=light_entity_id,
        brightness=bool(light_entity_id) and light_info.brightness,
        temperature_entity_id=temperature_entity_id,
        named_speeds=named_speeds,
    )
