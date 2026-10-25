"""Selects: folding mode (per machine), cause (account), solar stop action."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CLIENT_STATES,
    CONF_SOLAR_STOP_ACTION,
    KNOWN_CAUSES,
    STATE_FINISH,
    STATE_FOLD,
    STATE_PAUSE,
    STATUS_FINISHING,
    STATUS_PAUSED,
    STOP_ACTIONS,
)
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahAccountEntity, FahMachineEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up selects."""
    coordinator = entry.runtime_data
    async_add_entities([FahCauseSelect(coordinator)])

    @callback
    def add_machine(mid: str) -> None:
        async_add_entities([FahModeSelect(coordinator, mid), FahSolarStopActionSelect(coordinator, mid)])

    coordinator.register_machine_adder(add_machine)


class FahModeSelect(FahMachineEntity, SelectEntity):
    """Fold / Pause / Finish."""

    _attr_translation_key = "mode"
    _attr_options = CLIENT_STATES

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str) -> None:
        super().__init__(coordinator, machine_id, "mode")

    @property
    def current_option(self) -> str | None:
        """Current mode."""
        v = self.view
        if v is None:
            return None
        if v.status == STATUS_PAUSED:
            return STATE_PAUSE
        if v.status == STATUS_FINISHING:
            return STATE_FINISH
        return STATE_FOLD

    async def async_select_option(self, option: str) -> None:
        """Send the state command."""
        await self.coordinator.async_set_state(self.machine_id, option)


class FahCauseSelect(FahAccountEntity, SelectEntity):
    """Preferred cause for the account."""

    _attr_translation_key = "cause"
    _attr_options = KNOWN_CAUSES
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FahAccountCoordinator) -> None:
        super().__init__(coordinator, "cause")

    @property
    def current_option(self) -> str | None:
        """Current cause."""
        cause = self.coordinator.account.get("cause") or "any"
        return "any" if cause == "unspecified" else (cause if cause in KNOWN_CAUSES else None)

    async def async_select_option(self, option: str) -> None:
        """Set cause."""
        await self.coordinator.async_set_identity(cause=option)
        self.async_write_ha_state()


class FahSolarStopActionSelect(FahMachineEntity, SelectEntity):
    """What to do when solar surplus ends."""

    _attr_translation_key = "solar_stop_action"
    _attr_options = STOP_ACTIONS
    _attr_entity_category = EntityCategory.CONFIG
    _local = True

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str) -> None:
        super().__init__(coordinator, machine_id, "solar_stop_action")

    @property
    def current_option(self) -> str:
        """Current option."""
        return self.coordinator.machine_options(self.machine_id)[CONF_SOLAR_STOP_ACTION]

    async def async_select_option(self, option: str) -> None:
        """Save option (no reload)."""
        self.coordinator.async_update_machine_options(self.machine_id, {CONF_SOLAR_STOP_ACTION: option})
