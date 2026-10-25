"""Binary sensors for Folding@home."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import STATUS_FINISHING, STATUS_FOLDING
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahAccountEntity, FahMachineEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities([FahNodeConnected(coordinator)])

    @callback
    def add_machine(mid: str) -> None:
        async_add_entities([FahMachineOnline(coordinator, mid), FahComputing(coordinator, mid)])

    coordinator.register_machine_adder(add_machine)


class FahNodeConnected(FahAccountEntity, BinarySensorEntity):
    """Connected to the Folding@home node."""

    _attr_translation_key = "node_connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: FahAccountCoordinator) -> None:
        super().__init__(coordinator, "node_connected")

    @property
    def is_on(self) -> bool:
        """Connected?"""
        return self.coordinator.node.connected


class FahMachineOnline(FahMachineEntity, BinarySensorEntity):
    """Machine is connected to the node (reachable)."""

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _local = True

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str) -> None:
        super().__init__(coordinator, machine_id, "online")

    @property
    def is_on(self) -> bool:
        """Online?"""
        return self.coordinator.machine(self.machine_id).connected


class FahComputing(FahMachineEntity, BinarySensorEntity):
    """A work unit is computing."""

    _attr_translation_key = "folding"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str) -> None:
        super().__init__(coordinator, machine_id, "folding")

    @property
    def is_on(self) -> bool | None:
        """Computing?"""
        v = self.view
        if v is None:
            return None
        return v.status in (STATUS_FOLDING, STATUS_FINISHING) and bool(v.running_units)
