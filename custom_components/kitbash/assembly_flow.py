"""Shared device-first review and value mapping steps for every accessory."""

from __future__ import annotations

from copy import deepcopy

import voluptuous as vol
from homeassistant.const import CONF_NAME
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .assembly import (
    ENTITY_FIELDS,
    clean_mappings,
    mapping_for,
    part_bindings,
    role_for,
    suggest_kind,
    suggest_parts,
)
from .const import (
    CONF_AUTO_PRESET,
    CONF_DIRECTION,
    CONF_DIRECTION_REVERSED,
    CONF_FAN,
    CONF_KIND,
    CONF_LIGHT,
    CONF_MANUAL_PRESET,
    CONF_MODE,
    CONF_NAMED_SPEEDS,
    CONF_OSCILLATE,
    CONF_POWER,
    CONF_SOURCE_ENTITIES,
    CONF_SPEED,
    CONF_TEMPERATURE,
    CONF_VALUE_MAPPINGS,
    KINDS,
    KIND_CEILING_FAN,
    KIND_FAN,
)
from .controls import (
    SCALAR_DOMAINS,
    ScalarControl,
    validate_mapping,
)
from .model import ModelError
from .sources import describe_entities, device_entity_ids, entity_label, mode_options


class AssemblySteps:
    def _assembly_init(self, kind=None, options=None, name=None):
        self._assembly_kind = kind
        self._assembly_options = deepcopy(options or {})
        if self._assembly_options.get(CONF_FAN) and not self._assembly_options.get(
            CONF_POWER
        ):
            self._assembly_options[CONF_POWER] = self._assembly_options[CONF_FAN]
        self._assembly_name = name
        self._assembly_sources = list(
            self._assembly_options.get(CONF_SOURCE_ENTITIES) or []
        )
        if kind:
            self._assembly_sources = list(
                dict.fromkeys(
                    (
                        *self._assembly_sources,
                        *part_bindings(kind, self._assembly_options).values(),
                    )
                )
            )
        self._assembly_reasons = {}

    async def async_step_device(self, user_input=None):
        if user_input is not None:
            device = dr.async_get(self.hass).async_get(user_input["device"])
            if device is not None:
                self._assembly_sources = device_entity_ids(self.hass, device.id)
                self._assembly_name = device.name_by_user or device.name
                return await self.async_step_assembly_kind()
        return self.async_show_form(
            step_id="device",
            data_schema=vol.Schema({vol.Required("device"): DeviceSelector()}),
        )

    async def async_step_assembly_kind(self, user_input=None):
        entities = describe_entities(self.hass, self._assembly_sources)
        if user_input is not None:
            self._assembly_kind = user_input[CONF_KIND]
            self._assembly_options, self._assembly_reasons = suggest_parts(
                self._assembly_kind, entities
            )
            self._assembly_options[CONF_SOURCE_ENTITIES] = list(self._assembly_sources)
            self._suggest_mode()
            return await self.async_step_assembly_review()
        schema = vol.Schema(
            {
                vol.Required(CONF_KIND): SelectSelector(
                    SelectSelectorConfig(
                        options=list(KINDS),
                        translation_key=CONF_KIND,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        suggested = suggest_kind(entities)
        return self.async_show_form(
            step_id="assembly_kind",
            data_schema=self.add_suggested_values_to_schema(
                schema, {CONF_KIND: suggested} if suggested else {}
            ),
        )

    def _suggest_mode(self):
        options = self._assembly_options
        entity_id = options.get(CONF_MODE)
        if not entity_id:
            return
        if self._assembly_kind in (KIND_FAN, KIND_CEILING_FAN) and entity_id.split(".", 1)[0] in ("number", "input_number"):
            entity = describe_entities(self.hass, [entity_id])[0]
            control = ScalarControl.from_attributes(entity.domain, entity.attributes).with_mapping(mapping_for(options, CONF_MODE))
            manual, auto = control.boolean_values()
            options.setdefault(CONF_AUTO_PRESET, str(auto))
            options.setdefault(CONF_MANUAL_PRESET, str(manual))
            return
        offered = mode_options(self.hass, entity_id)
        auto = next((o for o in offered if o.strip().casefold() in ("auto", "automatic")), None)
        manual = next(
            (o for o in offered if o.strip().casefold() in ("manual", "normal")), None
        )
        if self._assembly_kind in (KIND_FAN, KIND_CEILING_FAN) and entity_id.startswith("select."):
            auto = auto or next((o for o in offered if o.strip().casefold() in ("on", "enabled", "true", "1")), None)
            manual = manual or next((o for o in offered if o.strip().casefold() in ("off", "disabled", "false", "0")), None)
        if auto and manual:
            options.setdefault(CONF_AUTO_PRESET, auto)
            options.setdefault(CONF_MANUAL_PRESET, manual)

    def _review_fields(self):
        from . import config_flow as flow

        fields = {}
        kind = self._assembly_kind
        if self._ask_name:
            fields[vol.Required(CONF_NAME)] = TextSelector()
        native = {
            CONF_POWER: ["fan", "switch", "input_boolean"],
            CONF_SPEED: ["fan"],
            CONF_DIRECTION: ["fan", "switch", "input_boolean"],
            CONF_OSCILLATE: ["fan", "switch", "input_boolean"],
            CONF_MODE: ["fan", "switch", "input_boolean"],
            CONF_LIGHT: ["light"],
            CONF_TEMPERATURE: ["sensor"],
        }
        for key in ENTITY_FIELDS[kind]:
            domains = native.get(key, [])
            config = flow.EntitySelectorConfig(
                domain=list(dict.fromkeys((*domains, *SCALAR_DOMAINS)))
            )
            # All fields search the whole HA registry; suggestions are never a filter.
            fields[vol.Optional(key)] = flow._entity_selector(config)
        extras = {}
        if kind in (KIND_FAN, KIND_CEILING_FAN):
            extras = {
                CONF_DIRECTION_REVERSED: BooleanSelector(),
                CONF_NAMED_SPEEDS: BooleanSelector(),
                CONF_AUTO_PRESET: TextSelector(),
                CONF_MANUAL_PRESET: TextSelector(),
            }
        for key, selector in extras.items():
            fields[vol.Optional(key)] = selector
        fields[vol.Optional("edit_values", default=False)] = BooleanSelector()
        return fields

    async def async_step_assembly_review(self, user_input=None):
        from .config_flow import ERROR_FIELD

        errors = {}
        if user_input is not None:
            if self._ask_name:
                self._assembly_name = user_input.get(CONF_NAME)
            previous = self._assembly_options
            options = deepcopy(previous)
            for key in (str(field.schema) for field in self._review_fields()):
                if key in (
                    CONF_NAME,
                    "edit_values",
                ):
                    continue
                options[key] = (
                    user_input.get(key)
                    if key in ENTITY_FIELDS[self._assembly_kind]
                    else user_input.get(key, previous.get(key))
                )
            # Clearing a part is intentional. Ancillary settings and mappings
            # survive, but mappings for changed entity bindings are discarded.
            options[CONF_VALUE_MAPPINGS] = previous.get(CONF_VALUE_MAPPINGS, {})
            options[CONF_SOURCE_ENTITIES] = list(
                dict.fromkeys(
                    (
                        *self._assembly_sources,
                        *part_bindings(self._assembly_kind, options).values(),
                    )
                )
            )
            options[CONF_VALUE_MAPPINGS] = clean_mappings(self._assembly_kind, options)
            self._assembly_options = options
            if user_input.get("edit_values"):
                return await self.async_step_value_mappings()
            try:
                title = self._assembly_validate(
                    self._assembly_kind, self._assembly_name, options
                )
            except ModelError as err:
                errors[ERROR_FIELD.get(err.code, "base")] = err.code
            else:
                try:
                    return await self._save_assembly(title)
                except ModelError as err:
                    errors["base"] = err.code
        suggested = {
            **self._assembly_options,
            CONF_NAME: self._assembly_name,
            "edit_values": False,
        }
        reasons = []
        for key, value in part_bindings(
            self._assembly_kind, self._assembly_options
        ).items():
            reason = self._assembly_reasons.get(key, "Saved or selected by you")
            reasons.append(
                f"• {key.split(':', 1)[0].replace('_', ' ').title()}: {entity_label(self.hass, value)} — {reason}"
            )
        return self.async_show_form(
            step_id="assembly_review",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(self._review_fields()),
                {k: v for k, v in suggested.items() if v is not None},
            ),
            errors=errors,
            description_placeholders={
                "matches": "\n".join(reasons) or "Choose the parts for this accessory."
            },
        )

    async def _save_assembly(self, title):
        return await self._assembly_create(
            title, self._assembly_kind, deepcopy(self._assembly_options)
        )

    async def async_step_value_mappings(self, user_input=None):
        from .assembly import mapping_for

        bindings = part_bindings(self._assembly_kind, self._assembly_options)
        bindings = {
            key: eid
            for key, eid in bindings.items()
            if eid.split(".", 1)[0] in SCALAR_DOMAINS
        }
        if user_input is not None:
            if user_input.get("done"):
                return await self.async_step_assembly_review()
            if user_input.get("control") in bindings:
                self._mapping_key = user_input["control"]
                self._mapping_entity = bindings[self._mapping_key]
                self._mapping_values = deepcopy(
                    mapping_for(self._assembly_options, self._mapping_key)
                )
                return await self.async_step_mapping_values()
        fields = {vol.Optional("done", default=False): BooleanSelector()}
        if bindings:
            fields[vol.Optional("control")] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {
                            "value": key,
                            "label": f"{key.split(':', 1)[0].replace('_', ' ').title()}: {entity_label(self.hass, eid)}",
                        }
                        for key, eid in bindings.items()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        return self.async_show_form(
            step_id="value_mappings",
            data_schema=vol.Schema(fields),
            description_placeholders={"count": str(len(bindings))},
        )

    async def async_step_mapping_values(self, user_input=None):
        entity = describe_entities(self.hass, [self._mapping_entity])[0]
        control = ScalarControl.from_attributes(entity.domain, entity.attributes)
        role = role_for(self._mapping_key.split(":", 1)[0], self._assembly_kind)
        binary = role in (
            "power",
            "oscillate",
            "direction",
            "mode",
        )
        errors, detail = {}, ""
        if user_input is not None:
            if user_input.get("back"):
                return await self.async_step_value_mappings()
            if user_input.get("automatic"):
                self._assembly_options.setdefault(CONF_VALUE_MAPPINGS, {}).pop(
                    self._mapping_key, None
                )
                if role == "mode" and (control.options or entity.domain != "select"):
                    off, on = control.boolean_values()
                    self._assembly_options[CONF_AUTO_PRESET] = str(on)
                    self._assembly_options[CONF_MANUAL_PRESET] = str(off)
                return await self.async_step_value_mappings()
            mapping = {
                key: user_input.get(key)
                for key in ("off", "on", "minimum", "maximum", "unit")
                if key in user_input
            }
            if user_input.get("levels"):
                mapping["levels"] = [
                    line.strip()
                    for line in user_input["levels"].splitlines()
                    if line.strip()
                ]
            try:
                mapping = validate_mapping(control, mapping)
            except (TypeError, ValueError) as err:
                errors["base"], detail = "invalid_mapping", str(err)
                self._mapping_values = dict(user_input)
            else:
                mapping["entity_id"] = self._mapping_entity
                self._assembly_options.setdefault(CONF_VALUE_MAPPINGS, {})[
                    self._mapping_key
                ] = mapping
                if role == "mode":
                    self._assembly_options[CONF_AUTO_PRESET] = str(
                        mapping.get("on", control.boolean_values()[1])
                    )
                    self._assembly_options[CONF_MANUAL_PRESET] = str(
                        mapping.get("off", control.boolean_values()[0])
                    )
                return await self.async_step_value_mappings()
        fields = {}
        numeric = NumberSelector(
            NumberSelectorConfig(
                mode=NumberSelectorMode.BOX,
                min=control.minimum,
                max=control.maximum,
                step="any",
            )
        )
        if binary:
            selector = (
                SelectSelector(
                    SelectSelectorConfig(
                        options=list(control.options), mode=SelectSelectorMode.DROPDOWN
                    )
                )
                if entity.domain == "select"
                else numeric
            )
            fields[vol.Optional("off")], fields[vol.Optional("on")] = selector, selector
        else:
            fields[vol.Optional("minimum")], fields[vol.Optional("maximum")] = (
                numeric,
                numeric,
            )
            fields[vol.Optional("levels")] = TextSelector(
                TextSelectorConfig(multiline=True)
            )
            if role in ("temperature", "setpoint", "heating", "cooling", "thermostat"):
                fields[vol.Optional("unit")] = SelectSelector(
                    SelectSelectorConfig(options=["°C", "°F", "K"])
                )
        fields[vol.Optional("automatic", default=False)] = BooleanSelector()
        fields[vol.Optional("back", default=False)] = BooleanSelector()
        suggested = dict(self._mapping_values)
        if isinstance(suggested.get("levels"), list):
            suggested["levels"] = "\n".join(str(value) for value in suggested["levels"])
        if binary and (control.options or entity.domain != "select"):
            off, on = control.boolean_values()
            suggested.setdefault("off", off)
            suggested.setdefault("on", on)
        return self.async_show_form(
            step_id="mapping_values",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), suggested
            ),
            errors=errors,
            description_placeholders={
                "entity": entity_label(self.hass, entity.entity_id),
                "role": role,
                "error": detail,
            },
        )
