"""The Kitbash integration.

Each config entry is one HomeKit accessory published on Kitbash's own HAP-python
driver (see ``hap.py``). There are no Home Assistant entities yet: the product is
the accessory in the Home app, built from raw entities that already exist.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import socket

from homeassistant.components import network, zeroconf
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
import voluptuous as vol
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, instance_id
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.storage import STORAGE_DIR

from .const import (
    CONF_AUTO_PRESET,
    CONF_DEV_MODE,
    CONF_DIRECTION,
    DATA_DEV_MODE,
    CONF_DIRECTION_REVERSED,
    CONF_NAMED_SPEEDS,
    CONF_FAN,
    CONF_KIND,
    CONF_LIGHT,
    CONF_MANUAL_PRESET,
    CONF_MODE,
    CONF_OSCILLATE,
    CONF_PORT,
    CONF_POWER,
    CONF_SPEED,
    CONF_TEMPERATURE,
    DOMAIN,
    KIND_CEILING_FAN,
    KIND_FAN,
    KINDS,
    MANUFACTURER,
    MODEL_CEILING_FAN,
    MODEL_FAN,
    PERSIST_FILE_TEMPLATE,
    VERSION,
)
from .iids import KitbashIIDStorage
from .fan_direction import FanDirectionStorage
from .recovery_storage import RecoveryStorage
from .hap import (
    FanAccessory,
    KitbashAccessory,
    KitbashDriver,
    async_dismiss_setup_message,
    async_show_setup_message,
)
from .model import (
    AssembledFan,
    ModelError,
    build_assembled_fan,
)
from .assembly import mapping_for
from .sources import (
    fan_info,
    light_info,
    mode_options,
    number_info,
    select_info,
)
from .trace import Tracer, install_trace_handler, log_dir

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# Integration-wide settings come from configuration.yaml:
#   kitbash:
#     dev_mode: true    # trace log under kitbash/logs; default off
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({vol.Optional(CONF_DEV_MODE, default=False): cv.boolean})},
    extra=vol.ALLOW_EXTRA,
)

MODELS = {
    KIND_CEILING_FAN: MODEL_CEILING_FAN,
    KIND_FAN: MODEL_FAN,
}


@dataclass
class KitbashRuntime:
    """What a loaded entry holds. ``driver`` is None until published."""

    driver: KitbashDriver | None = None
    accessory: KitbashAccessory | None = None


type KitbashConfigEntry = ConfigEntry[KitbashRuntime]


def _persist_path(hass: HomeAssistant, entry_id: str) -> str:
    return hass.config.path(STORAGE_DIR, PERSIST_FILE_TEMPLATE.format(entry_id=entry_id))


def _build_model(
    hass: HomeAssistant, entry: ConfigEntry
) -> AssembledFan:
    """Turn the entry's data/options into a pure model, or raise ConfigEntryError."""
    kind = entry.data.get(CONF_KIND)
    options = entry.options
    try:
        if kind in (KIND_CEILING_FAN, KIND_FAN):
            # Entries made before the parts existed stored the fan under "fan".
            power = options.get(CONF_POWER) or options.get(CONF_FAN)
            speed = options.get(CONF_SPEED) or None
            direction = options.get(CONF_DIRECTION) or None
            mode = options.get(CONF_MODE) or None
            # The fan-only kind never carries a light, whatever the options say.
            light = (options.get(CONF_LIGHT) or None) if kind == KIND_CEILING_FAN else None
            return build_assembled_fan(
                entry.title,
                power,
                speed_entity_id=speed,
                direction_entity_id=direction,
                direction_reversed=bool(options.get(CONF_DIRECTION_REVERSED)),
                oscillate_entity_id=options.get(CONF_OSCILLATE) or None,
                light_entity_id=light,
                temperature_entity_id=options.get(CONF_TEMPERATURE) or None,
                named_speeds=bool(options.get(CONF_NAMED_SPEEDS)),
                mode_entity_id=mode,
                auto_option=options.get(CONF_AUTO_PRESET),
                manual_option=options.get(CONF_MANUAL_PRESET),
                fan_info=fan_info(hass, power),
                light_info=light_info(hass, light),
                number_info=number_info(hass, speed, mapping_for(options, CONF_SPEED)),
                select_info=select_info(hass, speed, mapping_for(options, CONF_SPEED)),
                direction_select_info=select_info(hass, direction, mapping_for(options, CONF_DIRECTION)),
                oscillate_number_info=number_info(hass, options.get(CONF_OSCILLATE)),
                oscillate_select_info=select_info(hass, options.get(CONF_OSCILLATE)),
                mode_options=mode_options(hass, mode),
            )
    except ModelError as err:
        raise ConfigEntryError(f"{entry.title}: {err}") from err
    raise ConfigEntryError(f"unknown accessory kind {kind!r}")


def _make_accessory(
    hass: HomeAssistant,
    driver: KitbashDriver,
    model: AssembledFan,
    entry_id: str,
    model_name: str,
    trace: Tracer,
) -> KitbashAccessory:
    return FanAccessory(hass, driver, model, entry_id, model_name=model_name, trace=trace)


def _make_driver(
    hass: HomeAssistant,
    entry: ConfigEntry,
    advertised: list[str],
    async_zc: zeroconf.HaAsyncZeroconf,
    uuid: str,
) -> tuple[KitbashDriver, bool]:
    """Build the driver and load its persisted state. Runs in the executor.

    Returns the driver and whether state was loaded from disk (i.e. this is not
    the entry's first ever start).
    """
    persist_file = _persist_path(hass, entry.entry_id)
    # Listen on every interface, as HA's own bridge does; advertise the
    # addresses the network integration says are reachable.
    bind = ["0.0.0.0", "::"] if socket.has_ipv6 else ["0.0.0.0"]
    driver = KitbashDriver(
        hass,
        entry.entry_id,
        entry.title,
        loop=hass.loop,
        address=bind,
        port=entry.data[CONF_PORT],
        persist_file=persist_file,
        advertised_address=advertised,
        async_zeroconf_instance=async_zc,
        zeroconf_server=f"{uuid}-kitbash.local.",
    )
    if os.path.exists(persist_file):
        driver.load()
        return driver, True
    return driver, False


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Read the integration-wide YAML settings (none are required)."""
    yaml_conf = config.get(DOMAIN) or {}
    hass.data.setdefault(DOMAIN, {})[DATA_DEV_MODE] = bool(yaml_conf.get(CONF_DEV_MODE, False))
    if hass.data[DOMAIN][DATA_DEV_MODE]:
        _LOGGER.info("Kitbash dev mode is on; tracing to %s", log_dir(hass))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: KitbashConfigEntry) -> bool:
    """Register the entry and publish its accessory once Home Assistant is up.

    Publishing waits for EVENT_HOMEASSISTANT_STARTED (as HA's bridge does) so
    the source entities are loaded and the accessory shape -- a fan's speed
    step, a light's brightness -- is read from live state, not guessed.
    """
    if entry.data.get(CONF_KIND) not in KINDS:
        raise ConfigEntryError(f"Kitbash {VERSION} supports Fan and Ceiling fan; this entry is a {entry.data.get(CONF_KIND)}")

    entry.runtime_data = KitbashRuntime()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer=MANUFACTURER,
        model=MODELS[entry.data[CONF_KIND]],
        name=entry.title,
        sw_version=VERSION,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if hass.is_running:
        await _async_publish(hass, entry)
        return True

    async def _at_started(_: HomeAssistant) -> None:
        try:
            await _async_publish(hass, entry)
        except Exception:  # noqa: BLE001 - nothing upstream to raise to
            _LOGGER.exception("%s: could not publish the HomeKit accessory", entry.title)

    entry.async_on_unload(async_at_started(hass, _at_started))
    return True


async def _async_publish(hass: HomeAssistant, entry: KitbashConfigEntry) -> None:
    """Build the driver and accessory and start serving."""
    model = _build_model(hass, entry)

    advertised = await network.async_get_announce_addresses(hass)
    async_zc = await zeroconf.async_get_async_instance(hass)
    uuid = await instance_id.async_get(hass)
    driver, loaded = await hass.async_add_executor_job(
        _make_driver, hass, entry, advertised, async_zc, uuid
    )

    # Load the entry's instance-id allocations before anything is built: the
    # accessory hands every service and characteristic to the manager as it is
    # added, and the manager answers out of this.
    iid_storage = KitbashIIDStorage(hass, entry.entry_id)
    await iid_storage.async_initialize()
    driver.iid_storage = iid_storage

    trace_logger = None
    if hass.data.get(DOMAIN, {}).get(DATA_DEV_MODE):
        trace_logger = await hass.async_add_executor_job(install_trace_handler, log_dir(hass))
    trace = Tracer(entry.title, trace_logger)
    trace("publishing %s from %s", MODELS[entry.data[CONF_KIND]].lower(), model)
    driver.kitbash_options = dict(entry.options)
    recovery_storage = RecoveryStorage(hass, entry.entry_id)
    command_binding = json.loads(json.dumps({
        "model": asdict(model), "temperature_unit": str(hass.config.units.temperature_unit),
    }, default=lambda value: sorted(value)))
    await recovery_storage.async_initialize(
        {"kind": entry.data[CONF_KIND], "options": dict(entry.options)}, command_binding
    )
    driver.kitbash_recovery_storage = recovery_storage
    if isinstance(model, AssembledFan) and model.direction is not None:
        direction_storage = FanDirectionStorage(hass, entry.entry_id)
        await direction_storage.async_initialize({
            "power": model.power_entity_id,
            "direction": model.direction.entity_id,
            "reversed": model.direction.reversed,
            "options": list(model.direction.options or ()),
            "power_mapping": mapping_for(entry.options, CONF_POWER),
            "direction_mapping": mapping_for(entry.options, CONF_DIRECTION),
        })
        driver.kitbash_direction_storage = direction_storage
    accessory = _make_accessory(
        hass, driver, model, entry.entry_id, MODELS[entry.data[CONF_KIND]], trace
    )
    # Bypass driver.add_accessory: it does file I/O in the caller's thread, and
    # the load/persist is handled in _make_driver and below instead.
    driver.accessory = accessory
    # Every id this shape needed is now allocated; keep them for the next one.
    iid_storage.async_save()

    try:
        await driver.async_start()
    except OSError as err:
        await accessory.stop()
        raise ConfigEntryNotReady(
            f"cannot start HAP server on port {entry.data[CONF_PORT]}: {err}"
        ) from err
    if not loaded:
        await hass.async_add_executor_job(driver.persist)

    entry.runtime_data.driver = driver
    entry.runtime_data.accessory = accessory

    async def _async_stop(_: Event) -> None:
        await driver.async_stop()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop)
    )

    if driver.state.paired:
        async_dismiss_setup_message(hass, entry.entry_id)
    else:
        async_show_setup_message(
            hass, entry.entry_id, entry.title, driver.state.pincode, accessory.xhm_uri()
        )
    _LOGGER.info(
        "%s published as a HomeKit %s on port %s from %s",
        entry.title,
        MODELS[entry.data[CONF_KIND]].lower(),
        entry.data[CONF_PORT],
        ", ".join(model.entity_ids) or "no entity at all (its state lives in memory)",
    )


async def _async_update_listener(hass: HomeAssistant, entry: KitbashConfigEntry) -> None:
    """Options changed: rebuild the accessory."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: KitbashConfigEntry) -> bool:
    """Stop the driver. Pairings stay on disk for the next load."""
    # HA's shared zeroconf ignores async_close, so stopping the driver does not
    # take the rest of Home Assistant's mDNS down with it.
    if (driver := entry.runtime_data.driver) is not None:
        await driver.async_stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry deleted: forget the pairing so a re-add starts clean."""
    async_dismiss_setup_message(hass, entry.entry_id)
    await KitbashIIDStorage(hass, entry.entry_id).async_remove()
    await FanDirectionStorage(hass, entry.entry_id).async_remove()
    await RecoveryStorage(hass, entry.entry_id).async_remove()
    path = _persist_path(hass, entry.entry_id)

    def _remove() -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    await hass.async_add_executor_job(_remove)
