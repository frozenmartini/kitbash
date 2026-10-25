"""Pure scalar adapters."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

SCALAR_DOMAINS = ("number", "input_number", "select")
OFF_LABELS = (
    "off",
    "disabled",
    "disable",
    "false",
    "0",
    "none",
    "stop",
    "stopped",
    "closed",
    "unlocked",
    "manual",
    "fixed",
)
ON_LABELS = ("on", "enabled", "enable", "true", "1", "open", "locked", "auto", "swing")


def numeric_option(option: Any) -> float | None:
    """Accept plain numbers and numbers carrying a unit, such as '72 °F'."""
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*[^\d]*", str(option))
    if match is None:
        return None
    value = float(match[1])
    return value if math.isfinite(value) else None


@dataclass(frozen=True)
class ScalarControl:
    domain: str
    minimum: float = 0.0
    maximum: float = 100.0
    step: float = 1.0
    options: tuple[str, ...] = ()
    off_value: float | str | None = None
    on_value: float | str | None = None
    levels: tuple[float | str, ...] = ()

    def with_mapping(self, mapping: Mapping[str, Any]) -> ScalarControl:
        return replace(
            self,
            minimum=float(mapping.get("minimum", self.minimum)),
            maximum=float(mapping.get("maximum", self.maximum)),
            off_value=mapping.get("off"),
            on_value=mapping.get("on"),
            levels=tuple(mapping.get("levels") or ()),
        )

    @classmethod
    def from_attributes(cls, domain: str, attrs: Mapping[str, Any]) -> ScalarControl:
        options = tuple(str(option) for option in (attrs.get("options") or ()))
        if domain == "select":
            values = tuple(numeric_option(option) for option in options)
            numeric = values and all(value is not None for value in values)
            if not numeric:
                off = next(
                    (o for o in options if o.strip().casefold() in OFF_LABELS), None
                )
                if off is not None:
                    options = (off, *(o for o in options if o != off))
            return cls(
                domain,
                min(values) if numeric else 0.0,
                max(values) if numeric else float(max(1, len(options) - 1)),
                1.0,
                options,
            )
        try:
            low, high, step = (
                float(attrs.get(key, default))
                for key, default in (("min", 0), ("max", 100), ("step", 1))
            )
        except (TypeError, ValueError):
            low, high, step = 0.0, 100.0, 1.0
        if not all(math.isfinite(value) for value in (low, high, step)) or high <= low:
            low, high, step = 0.0, 100.0, 1.0
        return cls(domain, low, high, max(0.0, step))

    def scalar(self, raw: Any) -> float | None:
        if self.domain == "select":
            options = self.levels or self.options
            if raw not in options:
                return None
            values = tuple(numeric_option(option) for option in options)
            if all(value is not None for value in values):
                return numeric_option(raw)
            return float(options.index(raw))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def boolean_values(self) -> tuple[float | str, float | str]:
        if self.domain != "select":
            return (
                self.off_value if self.off_value is not None else self.minimum,
                self.on_value if self.on_value is not None else self.maximum,
            )
        if not self.options:
            raise ValueError("The select entity has no options yet")
        off = next(
            (o for o in self.options if o.strip().casefold() in OFF_LABELS),
            self.options[0],
        )
        on = next(
            (o for o in self.options if o != off and o.strip().casefold() in ON_LABELS),
            next((o for o in self.options if o != off), off),
        )
        return (
            self.off_value if self.off_value is not None else off,
            self.on_value if self.on_value is not None else on,
        )

    def is_on(self, raw: Any) -> bool | None:
        off, _ = self.boolean_values()
        if self.domain == "select":
            return raw != off if raw in self.options else None
        value = self.scalar(raw)
        if self.off_value is not None:
            return value != float(off) if value is not None else None
        return value > self.minimum if value is not None else None

    def percentage(self, raw: Any) -> float | None:
        if self.levels:
            try:
                index = (
                    self.levels.index(raw)
                    if self.domain == "select"
                    else min(
                        range(len(self.levels)),
                        key=lambda i: abs(float(self.levels[i]) - float(raw)),
                    )
                )
            except (TypeError, ValueError):
                return None
            return index * 100 / max(1, len(self.levels) - 1)
        value = self.scalar(raw)
        if value is None or self.maximum <= self.minimum:
            return None
        return max(
            0.0,
            min(100.0, (value - self.minimum) * 100 / (self.maximum - self.minimum)),
        )

    def target(self, value: float, *, percentage: bool = False) -> float | str:
        if percentage and self.levels:
            index = round(max(0.0, min(100.0, value)) * (len(self.levels) - 1) / 100)
            return self.levels[index]
        if percentage:
            value = (
                self.minimum
                + max(0.0, min(100.0, value)) * (self.maximum - self.minimum) / 100
            )
        value = max(self.minimum, min(self.maximum, value))
        if self.domain == "select":
            if not self.options:
                raise ValueError("The select entity has no options yet")
            return min(
                self.levels or self.options,
                key=lambda option: abs(self.scalar(option) - value),
            )
        if self.levels:
            return min(self.levels, key=lambda level: abs(float(level) - value))
        if self.step:
            value = self.minimum + round((value - self.minimum) / self.step) * self.step
        return max(self.minimum, min(self.maximum, value))

    def service(
        self, service: str, data: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Translate a native accessory command to the scalar domain."""
        off, on = self.boolean_values()
        if service in ("turn_off", "unlock", "close_cover"):
            target = off
        elif service in ("turn_on", "lock", "open_cover", "oscillate"):
            if service == "oscillate":
                target = on if data.get("oscillating") else off
            elif "brightness_pct" in data or "percentage" in data:
                target = self.target(
                    float(data.get("brightness_pct", data.get("percentage"))),
                    percentage=True,
                )
            else:
                target = on
        elif service in (
            "set_cover_position",
            "set_cover_tilt_position",
            "set_percentage",
        ):
            key = {
                "set_cover_position": "position",
                "set_cover_tilt_position": "tilt_position",
                "set_percentage": "percentage",
            }[service]
            target = self.target(float(data[key]), percentage=True)
        elif service == "set_direction":
            target = on if data["direction"] == "reverse" else off
        elif service in ("set_hvac_mode", "set_operation_mode"):
            mode = data.get("hvac_mode", data.get("operation_mode"))
            target = off if mode == "off" else on
        elif service == "select_option":
            option = str(data["option"])
            if self.domain == "select":
                target = option
            else:
                target = self.target(float(option))
        elif service == "set_value" and self.domain != "select":
            # Existing numeric targets already apply their own range and unit
            # conversion. Preserve the precision of temperature writes.
            target = (
                self.target(float(data["value"]))
                if self.levels
                else float(data["value"])
            )
        elif service in ("set_value", "set_temperature"):
            target = self.target(float(data.get("value", data.get("temperature"))))
        else:
            raise ValueError(f"{service} has no scalar mapping")
        if self.domain == "select":
            if target not in self.options:
                raise ValueError(f"{target!r} is not a select option")
            return "select_option", {"entity_id": data["entity_id"], "option": target}
        return "set_value", {"entity_id": data["entity_id"], "value": target}


def validate_mapping(
    control: ScalarControl, mapping: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate against the real source, without executing a service."""
    result = {
        key: value
        for key, value in mapping.items()
        if value is not None and value != ""
    }
    for key in ("off", "on"):
        if key not in result:
            continue
        value = result[key]
        if control.domain == "select":
            if value not in control.options:
                raise ValueError(f"{value!r} is not an option of the source")
        else:
            value = float(value)
            if (
                not math.isfinite(value)
                or not control.minimum <= value <= control.maximum
            ):
                raise ValueError(f"{key} must be within the source range")
            result[key] = value
    if "off" in result and "on" in result and result["off"] == result["on"]:
        raise ValueError("Off and On must be different")
    for key in ("minimum", "maximum"):
        if key in result:
            result[key] = float(result[key])
            if (
                not math.isfinite(result[key])
                or not control.minimum <= result[key] <= control.maximum
            ):
                raise ValueError("Slider bounds must be within the source range")
    low, high = (
        result.get("minimum", control.minimum),
        result.get("maximum", control.maximum),
    )
    if high <= low:
        raise ValueError("Maximum must be above minimum")
    if "levels" in result:
        levels = list(result["levels"])
        if not levels:
            result.pop("levels")
        else:
            if control.domain == "select":
                if any(value not in control.options for value in levels):
                    raise ValueError("Every level must be a source option")
            else:
                levels = [float(value) for value in levels]
                if any(
                    not math.isfinite(value)
                    or not control.minimum <= value <= control.maximum
                    for value in levels
                ):
                    raise ValueError("Every level must be within the source range")
            if len(set(levels)) != len(levels):
                raise ValueError("Levels must be distinct")
            result["levels"] = levels
    if result.get("unit") not in (None, "°C", "°F", "K"):
        raise ValueError("Choose Celsius, Fahrenheit or Kelvin")
    return result
