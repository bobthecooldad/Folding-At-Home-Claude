"""Services (actions) for Folding@home."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID, ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, entity_registry as er

from .const import (
    ATTR_GROUP,
    CONF_CAUSE,
    CONF_TEAM,
    CONF_USER,
    DOMAIN,
    GROUP_BOOL_KEYS,
    KNOWN_CAUSES,
    SERVICE_FINISH,
    SERVICE_FOLD,
    SERVICE_PAUSE,
    SERVICE_REFRESH_ACCOUNT,
    SERVICE_SET_GROUP_CONFIG,
    SERVICE_SET_IDENTITY,
    STATE_FINISH,
    STATE_FOLD,
    STATE_PAUSE,
)
from .validation import valid_user

if TYPE_CHECKING:
    from .coordinator import FahAccountCoordinator

TARGET = {
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
}
STATE_SCHEMA = vol.Schema({**TARGET, vol.Optional(ATTR_GROUP): cv.string})
IDENTITY_SCHEMA = vol.Schema(
    {
        **TARGET,
        vol.Optional(CONF_USER): vol.All(cv.string, valid_user),
        vol.Optional(CONF_TEAM): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional(CONF_CAUSE): vol.In(KNOWN_CAUSES),
    }
)
GROUP_SCHEMA = vol.Schema(
    {
        **TARGET,
        vol.Optional(ATTR_GROUP, default=""): cv.string,
        vol.Optional("cpus"): vol.All(vol.Coerce(int), vol.Range(min=0, max=1024)),
        **{vol.Optional(k): cv.boolean for k in GROUP_BOOL_KEYS},
    }
)
REFRESH_SCHEMA = vol.Schema(TARGET)


def _targets(
    hass: HomeAssistant, call: ServiceCall
) -> list[tuple[FahAccountCoordinator, list[str]]]:
    """(coordinator, machine ids) pairs targeted by a call.

    No target = every machine of every account. Targeting the account device
    selects all of its machines.
    """
    coords: dict[str, FahAccountCoordinator] = {
        e.entry_id: e.runtime_data
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    }
    device_ids: set[str] = set(call.data.get(ATTR_DEVICE_ID, []))
    ent_reg = er.async_get(hass)
    for entity_id in call.data.get(ATTR_ENTITY_ID, []):
        if (ent := ent_reg.async_get(entity_id)) and ent.device_id:
            device_ids.add(ent.device_id)
    if not device_ids:
        return [(c, list(c.contexts)) for c in coords.values()]

    dev_reg = dr.async_get(hass)
    result: dict[str, tuple[FahAccountCoordinator, list[str]]] = {}
    for device_id in device_ids:
        device = dev_reg.async_get(device_id)
        if device is None:
            raise ServiceValidationError(f"Unknown device {device_id}")
        ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        for entry_id in device.config_entries:
            coord = coords.get(entry_id)
            if coord is None:
                continue
            item = result.setdefault(entry_id, (coord, []))
            for mid in ids:
                if mid == coord.account_id:
                    item[1].extend(m for m in coord.contexts if m not in item[1])
                elif mid in coord.contexts and mid not in item[1]:
                    item[1].append(mid)
    if not result:
        raise ServiceValidationError("No Folding@home machine matches the target")
    return list(result.values())


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration services."""

    def _state_handler(state: str):
        async def handler(call: ServiceCall) -> None:
            group = call.data.get(ATTR_GROUP)
            explicit = bool(call.data.get(ATTR_DEVICE_ID) or call.data.get(ATTR_ENTITY_ID))
            for coord, machines in _targets(hass, call):
                for mid in machines:
                    if not explicit and not coord.machine(mid).connected:
                        continue  # "all machines": skip the offline ones
                    await coord.async_set_state(mid, state, group)

        return handler

    hass.services.async_register(DOMAIN, SERVICE_FOLD, _state_handler(STATE_FOLD), STATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_PAUSE, _state_handler(STATE_PAUSE), STATE_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_FINISH, _state_handler(STATE_FINISH), STATE_SCHEMA)

    async def set_identity(call: ServiceCall) -> None:
        values: dict[str, Any] = {
            k: call.data[k] for k in (CONF_USER, CONF_TEAM, CONF_CAUSE) if k in call.data
        }
        if not values:
            raise ServiceValidationError("Nothing to set")
        for coord, _machines in _targets(hass, call):
            await coord.async_set_identity(**values)

    hass.services.async_register(DOMAIN, SERVICE_SET_IDENTITY, set_identity, IDENTITY_SCHEMA)

    async def set_group_config(call: ServiceCall) -> None:
        group = call.data[ATTR_GROUP]
        values = {k: call.data[k] for k in ["cpus", *GROUP_BOOL_KEYS] if k in call.data}
        if not values:
            raise ServiceValidationError("Nothing to set")
        for coord, machines in _targets(hass, call):
            for mid in machines:
                await coord.async_configure_group(mid, group, **values)

    hass.services.async_register(DOMAIN, SERVICE_SET_GROUP_CONFIG, set_group_config, GROUP_SCHEMA)

    async def refresh(call: ServiceCall) -> None:
        for coord, _machines in _targets(hass, call):
            try:
                await coord.async_refresh_account()
            except Exception:  # noqa: BLE001
                pass
            await coord.async_refresh_stats()

    hass.services.async_register(DOMAIN, SERVICE_REFRESH_ACCOUNT, refresh, REFRESH_SCHEMA)
