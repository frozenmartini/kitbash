"""Config and options flow for Kitbash.

One entry = one HomeKit accessory. ``data`` holds the kind and the HAP port
(fixed at creation); ``options`` holds what the options flow may edit, which
reloads the entry and rebuilds the accessory.

All kinds share device/entity/group/manual entry points, suggested-parts review,
and optional per-part scalar value mapping via ``AssemblySteps``. Options editing
starts at review with the stored choices. Original per-kind steps remain for
legacy flow links.
"""

from __future__ import annotations

import socket
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .assembly import mapping_for
from .assembly_flow import AssemblySteps
from .controls import SCALAR_DOMAINS

from .const import (
    CONF_AUTO_PRESET,
    CONF_DIRECTION,
    CONF_DIRECTION_REVERSED,
    CONF_FAN,
    CONF_KIND,
    CONF_LIGHT,
    CONF_MANUAL_PRESET,
    CONF_NAMED_SPEEDS,
    CONF_MODE,
    CONF_OSCILLATE,
    CONF_PORT,
    CONF_POWER,
    CONF_SPEED,
    CONF_TEMPERATURE,
    DEFAULT_PORT_START,
    DOMAIN,
    KIND_CEILING_FAN,
    KIND_FAN,
)
from .model import (
    FAN_DOMAIN,
    LIGHT_DOMAIN,
    SENSOR_DOMAIN,
    OSCILLATE_DOMAINS,
    INPUT_BOOLEAN_DOMAIN,
    SWITCH_DOMAIN,
    ModelError,
    build_assembled_fan,
    find_free_port,
)
from .sources import (
    Candidate,
    device_candidates,
    entity_label,
    fan_info,
    light_info,
    mode_options,
    number_info,
    select_info,
)

# Dropdown sentinel: the part comes from an entity picked on the next step.
OTHER = "__other__"
# Checkbox on every step after the first: go back one step instead of saving.
# Data entry flows have no back button of their own.
BACK = "back"


def _entity_selector(config: EntitySelectorConfig) -> EntitySelector:
    """Scalar entities can supply any accessory part."""
    settings = dict(config)
    if "filter" in settings:
        settings["filter"] = [*settings["filter"], *({"domain": domain} for domain in SCALAR_DOMAINS)]
    elif "domain" in settings:
        domains = settings["domain"]
        domains = [domains] if isinstance(domains, str) else list(domains)
        settings["domain"] = list(dict.fromkeys((*domains, *SCALAR_DOMAINS)))
    return EntitySelector(EntitySelectorConfig(**settings))


POWER_SELECTOR = _entity_selector(EntitySelectorConfig(domain=[FAN_DOMAIN, SWITCH_DOMAIN]))

# What each part accepts; used by the "other entity" pickers and to point a
# validation error at its field.
PART_DOMAINS: dict[str, list[str]] = {
    CONF_SPEED: [FAN_DOMAIN, *SCALAR_DOMAINS],
    CONF_DIRECTION: [FAN_DOMAIN, SWITCH_DOMAIN, *SCALAR_DOMAINS],
    CONF_OSCILLATE: list(OSCILLATE_DOMAINS),
    CONF_MODE: [FAN_DOMAIN, SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN, *SCALAR_DOMAINS],
    # Matches model.TEMPERATURE_DOMAINS. The picker offered `sensor` alone while
    # the model already accepted `input_number`, so a dummy reading could be
    # validated but never chosen. Widened 2026-09-14.
    CONF_TEMPERATURE: [SENSOR_DOMAIN, *SCALAR_DOMAINS],
    CONF_LIGHT: [LIGHT_DOMAIN, *SCALAR_DOMAINS],
}
PART_KEYS = (
    CONF_SPEED,
    CONF_DIRECTION,
    CONF_OSCILLATE,
    CONF_MODE,
    CONF_TEMPERATURE,
    CONF_LIGHT,  # ceiling fan only
)
STORED_KEYS = (
    CONF_POWER,
    *PART_KEYS,
    CONF_DIRECTION_REVERSED,
    CONF_NAMED_SPEEDS,
    CONF_AUTO_PRESET,
    CONF_MANUAL_PRESET,
)
ERROR_FIELD = {
    "not_a_fan": CONF_POWER,
    "bad_speed_source": CONF_SPEED,
    "select_without_options": CONF_SPEED,
    "named_speeds_need_speed": CONF_NAMED_SPEEDS,
    "too_many_speeds": CONF_NAMED_SPEEDS,
    "bad_direction_source": CONF_DIRECTION,
    "direction_options": CONF_DIRECTION,
    "bad_oscillate_source": CONF_OSCILLATE,
    "bad_mode_source": CONF_MODE,
    "not_a_temperature": CONF_TEMPERATURE,
    "not_a_light": CONF_LIGHT,
}


def _parts_for(kind: str) -> tuple[str, ...]:
    return tuple(k for k in PART_KEYS if k != CONF_LIGHT or kind == KIND_CEILING_FAN)


def _candidate_selector(
    candidates: list[Candidate], part: str, current: str | None
) -> SelectSelector:
    """Dropdown of the device's entities for one part, "Other entity…" last."""
    options = [
        {"value": c.entity_id, "label": c.label}
        for c in candidates
    ]
    if current and current != OTHER and current not in {o["value"] for o in options}:
        options.append({"value": current, "label": f"{current} (current)"})
    options.append({"value": OTHER, "label": "Other entity…"})
    return SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )


def _other_selector(part: str) -> EntitySelector:
    if part == CONF_TEMPERATURE:
        return _entity_selector(
            EntitySelectorConfig(domain=SENSOR_DOMAIN, device_class="temperature")
        )
    return _entity_selector(EntitySelectorConfig(domain=PART_DOMAINS[part]))


def _mode_selector(options: tuple[str, ...]) -> SelectSelector:
    """Dropdown of the mode entity's presets/options; free text if none are known yet."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(options),
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,
        )
    )


def _port_is_free(port: int) -> bool:
    """Bind probe on all interfaces; runs in the executor."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_socket:
        test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            test_socket.bind(("", port))
        except OSError:
            return False
    return True


async def _async_allocate_port(hass: HomeAssistant) -> int:
    """Next free port not already given to another Kitbash entry."""
    taken = {
        entry.data[CONF_PORT]
        for entry in hass.config_entries.async_entries(DOMAIN)
        if CONF_PORT in entry.data
    }
    return await hass.async_add_executor_job(
        find_free_port, DEFAULT_PORT_START, taken, _port_is_free
    )


def _stored(options: dict[str, Any]) -> dict[str, Any]:
    """The options as saved: every key present, blanks as None, the toggle a bool."""
    stored = {key: (options.get(key) or None) for key in STORED_KEYS}
    stored[CONF_DIRECTION_REVERSED] = bool(options.get(CONF_DIRECTION_REVERSED))
    stored[CONF_NAMED_SPEEDS] = bool(options.get(CONF_NAMED_SPEEDS))
    return stored


def _effective_mode(hass: HomeAssistant, options: dict[str, Any]) -> str | None:
    """The mode entity the model will use: the chosen one, else a power fan with presets."""
    if mode := options.get(CONF_MODE):
        return mode
    power = options.get(CONF_POWER)
    if power and power.startswith(f"{FAN_DOMAIN}.") and fan_info(hass, power).preset_modes:
        return power
    return None


def _asks_options(mode: str | None) -> bool:
    """Whether the mode step has anything to ask: a switch is fixed at on = Auto."""
    return bool(mode) and mode.split(".", 1)[0] not in (SWITCH_DOMAIN, INPUT_BOOLEAN_DOMAIN)


def _validate_fan(hass: HomeAssistant, name: str | None, options: dict[str, Any]) -> str:
    """Build the model from options against live sources; return the clean name."""
    fan = build_assembled_fan(
        name,
        options.get(CONF_POWER),
        speed_entity_id=options.get(CONF_SPEED),
        direction_entity_id=options.get(CONF_DIRECTION),
        direction_reversed=bool(options.get(CONF_DIRECTION_REVERSED)),
        oscillate_entity_id=options.get(CONF_OSCILLATE),
        mode_entity_id=options.get(CONF_MODE),
        auto_option=options.get(CONF_AUTO_PRESET),
        manual_option=options.get(CONF_MANUAL_PRESET),
        light_entity_id=options.get(CONF_LIGHT),
        temperature_entity_id=options.get(CONF_TEMPERATURE),
        named_speeds=bool(options.get(CONF_NAMED_SPEEDS)),
        fan_info=fan_info(hass, options.get(CONF_POWER)),
        light_info=light_info(hass, options.get(CONF_LIGHT)),
        number_info=number_info(hass, options.get(CONF_SPEED), mapping_for(options, CONF_SPEED)),
        select_info=select_info(hass, options.get(CONF_SPEED), mapping_for(options, CONF_SPEED)),
        direction_select_info=select_info(hass, options.get(CONF_DIRECTION), mapping_for(options, CONF_DIRECTION)),
        oscillate_number_info=number_info(hass, options.get(CONF_OSCILLATE)),
        oscillate_select_info=select_info(hass, options.get(CONF_OSCILLATE)),
        mode_options=mode_options(hass, options.get(CONF_MODE)),
    )
    return fan.name


class _FanSteps:
    """The fan steps, shared by the config flow and the options flow.

    power -> parts -> (other) -> (mode) -> ``_async_finish_fan``. The config
    flow asks for a name on the power step and allocates a port at the end;
    the options flow starts from the stored options and saves them.
    """

    hass: HomeAssistant
    _kind: str
    _name: str | None
    _fan: dict[str, Any]  # the options gathered so far
    _other: list[str]  # parts set to "Other entity…"
    _presets_mode: str | None  # the mode entity the stored Auto/Manual names belong to
    _ask_name: bool

    def _init_fan(self, kind: str, options: dict[str, Any], name: str | None) -> None:
        self._kind = kind
        self._name = name
        self._fan = {k: v for k, v in options.items() if v}
        # Entries made before the parts existed stored the fan under "fan".
        if CONF_FAN in options and not self._fan.get(CONF_POWER):
            self._fan[CONF_POWER] = options[CONF_FAN]
        self._other = []
        self._presets_mode = _effective_mode(self.hass, self._fan) if self._fan else None

    async def _async_finish_fan(self) -> ConfigFlowResult:
        raise NotImplementedError

    # -- step ids (one set per kind; the forms and texts differ only by kind) --
    async def async_step_ceiling_fan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._kind = KIND_CEILING_FAN
        return await self._async_step_power(user_input)

    async def async_step_fan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._kind = KIND_FAN
        return await self._async_step_power(user_input)

    async def async_step_ceiling_fan_parts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_parts(user_input)

    async def async_step_fan_parts(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_parts(user_input)

    async def async_step_ceiling_fan_other(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_other(user_input)

    async def async_step_fan_other(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_other(user_input)

    async def async_step_ceiling_fan_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_mode(user_input)

    async def async_step_fan_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_step_mode(user_input)

    # -- 1. power --
    async def _async_step_power(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            power = user_input.get(CONF_POWER) or None
            if not power:
                errors[CONF_POWER] = "not_a_fan"
            else:
                if self._ask_name:
                    self._name = user_input.get(CONF_NAME)
                if power != self._fan.get(CONF_POWER):
                    # Another device: the parts chosen for the old one no longer apply.
                    self._fan = {CONF_POWER: power}
                return await self._async_show_parts({})

        fields: dict[Any, Any] = {}
        if self._ask_name:
            fields[vol.Required(CONF_NAME)] = TextSelector()
        fields[vol.Required(CONF_POWER)] = POWER_SELECTOR
        suggested = user_input or {
            CONF_NAME: self._name,
            CONF_POWER: self._fan.get(CONF_POWER),
        }
        return self.async_show_form(
            step_id=self._kind,
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), suggested),
            errors=errors,
        )

    # -- 2. parts --
    async def _async_step_parts(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is None:
            return await self._async_show_parts({})
        if user_input.get(BACK):
            return await self._async_step_power(None)
        parts = _parts_for(self._kind)
        self._other = [part for part in parts if user_input.get(part) == OTHER]
        for part in parts:
            value = user_input.get(part) or None
            self._fan[part] = None if value == OTHER else value
        self._fan[CONF_DIRECTION_REVERSED] = bool(user_input.get(CONF_DIRECTION_REVERSED))
        self._fan[CONF_NAMED_SPEEDS] = bool(user_input.get(CONF_NAMED_SPEEDS))
        if self._other:
            return await self._async_show_other({})
        return await self._async_after_parts(from_other=False)

    async def _async_show_parts(self, errors: dict[str, str]) -> ConfigFlowResult:
        power = self._fan[CONF_POWER]
        candidates = device_candidates(self.hass, power)
        fields: dict[Any, Any] = {}
        suggested: dict[str, Any] = {}
        for part in _parts_for(self._kind):
            current = self._fan.get(part)
            fields[vol.Optional(part)] = _candidate_selector(candidates, part, current)
            if current:
                suggested[part] = current
            elif part in self._other:
                suggested[part] = OTHER
            if part == CONF_SPEED:
                fields[vol.Optional(CONF_NAMED_SPEEDS, default=False)] = BooleanSelector()
                if self._fan.get(CONF_NAMED_SPEEDS):
                    suggested[CONF_NAMED_SPEEDS] = True
            if part == CONF_DIRECTION:
                fields[vol.Optional(CONF_DIRECTION_REVERSED, default=False)] = BooleanSelector()
                if self._fan.get(CONF_DIRECTION_REVERSED):
                    suggested[CONF_DIRECTION_REVERSED] = True
        fields[vol.Optional(BACK, default=False)] = BooleanSelector()
        return self.async_show_form(
            step_id=f"{self._kind}_parts",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), suggested),
            errors=errors,
            description_placeholders={
                "power": entity_label(self.hass, power),
                "count": str(len(candidates)),
            },
        )

    # -- 3. other entities --
    async def _async_step_other(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is None:
            return await self._async_show_other({})
        if user_input.get(BACK):
            return await self._async_show_parts({})
        for part in self._other:
            self._fan[part] = user_input.get(part) or None
        return await self._async_after_parts(from_other=True)

    async def _async_show_other(self, errors: dict[str, str]) -> ConfigFlowResult:
        fields: dict[Any, Any] = {vol.Required(part): _other_selector(part) for part in self._other}
        fields[vol.Optional(BACK, default=False)] = BooleanSelector()
        suggested = {part: self._fan[part] for part in self._other if self._fan.get(part)}
        return self.async_show_form(
            step_id=f"{self._kind}_other",
            data_schema=self.add_suggested_values_to_schema(vol.Schema(fields), suggested),
            errors=errors,
        )

    async def _async_after_parts(self, from_other: bool) -> ConfigFlowResult:
        """Validate the parts; on to the mode step or the end."""
        try:
            _validate_fan(self.hass, self._name, {**self._fan, CONF_AUTO_PRESET: None, CONF_MANUAL_PRESET: None})
        except ModelError as err:
            errors = {ERROR_FIELD.get(err.code, "base"): err.code}
            if from_other and ERROR_FIELD.get(err.code) in self._other:
                return await self._async_show_other(errors)
            return await self._async_show_parts(errors)
        mode = _effective_mode(self.hass, self._fan)
        if mode != self._presets_mode:
            # Another mode entity: the Auto/Manual names chosen for the old one
            # would be offered again, and accepted if the new entity's options
            # are not loaded yet.
            self._fan[CONF_AUTO_PRESET] = None
            self._fan[CONF_MANUAL_PRESET] = None
            self._presets_mode = mode
        if _asks_options(mode):
            return await self._async_show_mode({}, None)
        return await self._async_finish_fan()

    # -- 4. mode: which option is Auto, which Manual --
    async def _async_step_mode(
        self, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        if user_input is None:
            return await self._async_show_mode({}, None)
        if user_input.get(BACK):
            if self._other:
                return await self._async_show_other({})
            return await self._async_show_parts({})
        self._fan[CONF_AUTO_PRESET] = user_input.get(CONF_AUTO_PRESET) or None
        self._fan[CONF_MANUAL_PRESET] = user_input.get(CONF_MANUAL_PRESET) or None
        try:
            _validate_fan(self.hass, self._name, self._fan)
        except ModelError as err:
            return await self._async_show_mode({"base": err.code}, user_input)
        return await self._async_finish_fan()

    async def _async_show_mode(
        self, errors: dict[str, str], user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        mode = _effective_mode(self.hass, self._fan) or ""
        options = mode_options(self.hass, mode)
        selector = _mode_selector(options)
        schema = vol.Schema(
            {
                vol.Optional(CONF_AUTO_PRESET): selector,
                vol.Optional(CONF_MANUAL_PRESET): selector,
                vol.Optional(BACK, default=False): BooleanSelector(),
            }
        )
        suggested = user_input or {
            key: self._fan[key]
            for key in (CONF_AUTO_PRESET, CONF_MANUAL_PRESET)
            if self._fan.get(key)
        }
        return self.async_show_form(
            step_id=f"{self._kind}_mode",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={
                "mode": entity_label(self.hass, mode) if mode else "",
                "options": ", ".join(options) or "none reported yet",
            },
        )


def _validate_assembly(hass, kind, name, options):
    from .assembly import clean_mappings
    from .controls import ScalarControl, validate_mapping
    from .sources import describe_entities

    for key, mapping in clean_mappings(kind, options).items():
        entity = describe_entities(hass, [mapping["entity_id"]])[0]
        try:
            validate_mapping(ScalarControl.from_attributes(entity.domain, entity.attributes), mapping)
        except (TypeError, ValueError) as err:
            raise ModelError("invalid_mapping", f"{key}: {err}") from err
    if kind in (KIND_FAN, KIND_CEILING_FAN):
        return _validate_fan(hass, name, options)


class KitbashConfigFlow(
    AssemblySteps, _FanSteps,
    ConfigFlow, domain=DOMAIN,
):
    """Create one accessory."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize both the shared assembly flow and legacy fan steps."""
        self._ask_name = True
        self._assembly_init()
        self._init_fan(KIND_CEILING_FAN, {}, None)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Start from a device."""
        return await self.async_step_device()

    def _assembly_validate(self, kind, name, options):
        return _validate_assembly(self.hass, kind, name, options)

    async def _assembly_create(self, title, kind, options):
        try:
            port = await _async_allocate_port(self.hass)
        except ModelError as err:
            return self.async_abort(reason=err.code)
        return self.async_create_entry(title=title, data={CONF_KIND: kind, CONF_PORT: port}, options=options)

    # -- fan kinds: steps come from _FanSteps --
    async def _async_finish_fan(self) -> ConfigFlowResult:
        name = _validate_fan(self.hass, self._name, self._fan)
        try:
            port = await _async_allocate_port(self.hass)
        except ModelError as err:
            return self.async_abort(reason=err.code)
        return self.async_create_entry(
            title=name,
            data={CONF_KIND: self._kind, CONF_PORT: port},
            options=_stored(self._fan),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> KitbashOptionsFlow:
        """Return the options flow."""
        return KitbashOptionsFlow()


class KitbashOptionsFlow(
    AssemblySteps, _FanSteps,
    OptionsFlow
):
    """Change what the accessory is built from."""

    _ask_name = False

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Review stored choices without replacing them with detection results."""
        self._assembly_init(self.config_entry.data[CONF_KIND], dict(self.config_entry.options), self.config_entry.title)
        return await self.async_step_assembly_review(user_input)

    def _assembly_validate(self, kind, name, options):
        return _validate_assembly(self.hass, kind, self.config_entry.title, options)

    async def _assembly_create(self, title, kind, options):
        return self.async_create_entry(data=options)

    async def async_step_legacy_parts(self, user_input=None):
        """Keep the original per-kind steps available for older flow links."""
        kind = self.config_entry.data.get(CONF_KIND)
        if kind in (KIND_CEILING_FAN, KIND_FAN):
            self._init_fan(kind, dict(self.config_entry.options), self.config_entry.title)
            if kind == KIND_FAN:
                return await self.async_step_fan()
            return await self.async_step_ceiling_fan()

    async def _async_finish_fan(self) -> ConfigFlowResult:
        _validate_fan(self.hass, self._name, self._fan)
        return self.async_create_entry(data=_stored(self._fan))
