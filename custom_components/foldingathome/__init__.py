"""The Folding@home integration (account / node based)."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .account import FahAuthError, check_stored_key, valid_node_host
from .const import CONF_ACCOUNT, CONF_PRIVATE_KEY, DOMAIN
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .frontend import async_register_card
from .services import async_setup_services
from .websocket import async_register_websocket

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register services, the dashboard card and its websocket command."""
    async_setup_services(hass)
    async_register_websocket(hass)
    integration = await async_get_integration(hass, DOMAIN)
    await async_register_card(hass, str(integration.version or "0"))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FahConfigEntry) -> bool:
    """Set up a Folding@home account."""
    try:
        check_stored_key(entry.data[CONF_PRIVATE_KEY], entry.data[CONF_ACCOUNT]["pubkey"])
        if not valid_node_host(entry.data[CONF_ACCOUNT].get("node")):
            raise FahAuthError("Invalid node host")
    except (FahAuthError, KeyError, TypeError) as err:
        raise ConfigEntryAuthFailed(f"Stored Folding@home account key is invalid: {err}") from err
    coordinator = FahAccountCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_start()
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: FahConfigEntry) -> None:
    """Options changed (solar settings): apply live, no reload."""
    coordinator = entry.runtime_data
    coordinator.async_setup_solar()


async def async_unload_entry(hass: HomeAssistant, entry: FahConfigEntry) -> bool:
    """Unload."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: FahConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a machine that is no longer linked / online."""
    coordinator = entry.runtime_data
    ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
    if coordinator.account_id in ids:
        return False
    for mid in ids:
        ctx = coordinator.contexts.get(mid)
        if ctx and ctx.machine.online:
            return False
        linked = {m.get("id") for m in coordinator.account.get("machines") or []}
        if mid in linked:
            return False
        coordinator.contexts.pop(mid, None)
    return True
