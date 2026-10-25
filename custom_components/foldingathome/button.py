"""Buttons: Fold / Pause / Finish (per machine and all machines)."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .account import FahConnectionError
from .const import STATE_FINISH, STATE_FOLD, STATE_PAUSE
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahAccountEntity, FahMachineEntity

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class FahButtonDescription(ButtonEntityDescription):
    """Button description."""

    command: str | None = None


MACHINE_BUTTONS = (
    FahButtonDescription(key="fold", translation_key="fold", icon="mdi:play", command=STATE_FOLD),
    FahButtonDescription(key="pause", translation_key="pause", icon="mdi:pause", command=STATE_PAUSE),
    FahButtonDescription(key="finish", translation_key="finish", icon="mdi:flag-checkered", command=STATE_FINISH),
)
ACCOUNT_BUTTONS = (
    FahButtonDescription(key="fold_all", translation_key="fold_all", icon="mdi:play", command=STATE_FOLD),
    FahButtonDescription(key="pause_all", translation_key="pause_all", icon="mdi:pause", command=STATE_PAUSE),
    FahButtonDescription(
        key="finish_all", translation_key="finish_all", icon="mdi:flag-checkered", command=STATE_FINISH
    ),
    FahButtonDescription(
        key="refresh", translation_key="refresh", icon="mdi:refresh", entity_category=EntityCategory.DIAGNOSTIC
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    coordinator = entry.runtime_data
    async_add_entities(FahAccountButton(coordinator, d) for d in ACCOUNT_BUTTONS)

    @callback
    def add_machine(mid: str) -> None:
        async_add_entities(FahMachineButton(coordinator, mid, d) for d in MACHINE_BUTTONS)

    coordinator.register_machine_adder(add_machine)


class FahMachineButton(FahMachineEntity, ButtonEntity):
    """Command for one machine."""

    entity_description: FahButtonDescription

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str, description: FahButtonDescription) -> None:
        super().__init__(coordinator, machine_id, f"button_{description.key}")
        self.entity_description = description

    async def async_press(self) -> None:
        """Send the command."""
        assert self.entity_description.command
        await self.coordinator.async_set_state(self.machine_id, self.entity_description.command)


class FahAccountButton(FahAccountEntity, ButtonEntity):
    """Command for all machines."""

    entity_description: FahButtonDescription

    def __init__(self, coordinator: FahAccountCoordinator, description: FahButtonDescription) -> None:
        super().__init__(coordinator, f"button_{description.key}")
        self.entity_description = description

    async def async_press(self) -> None:
        """Send the command."""
        if self.entity_description.command:
            await self.coordinator.async_set_state_all(self.entity_description.command)
            return
        error = None
        try:
            await self.coordinator.async_refresh_account()
        except FahConnectionError as err:
            error = err
        await self.coordinator.async_refresh_stats()
        self.coordinator.async_update_listeners()
        if error is not None:
            raise HomeAssistantError(f"Could not refresh the Folding@home account: {error}") from error
