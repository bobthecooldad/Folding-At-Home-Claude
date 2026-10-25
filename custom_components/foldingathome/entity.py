"""Base entities: one device per machine, plus an account device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import FahAccountCoordinator
from .model import MachineView


class FahAccountEntity(CoordinatorEntity[FahAccountCoordinator]):
    """Entity on the account device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FahAccountCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.account_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.account_id)},
            name="Folding@home account",
            manufacturer=MANUFACTURER,
            model="Account",
            configuration_url="https://app.foldingathome.org/",
        )

    @property
    def available(self) -> bool:
        """Always available (shows node status itself)."""
        return True


class FahMachineEntity(CoordinatorEntity[FahAccountCoordinator]):
    """Entity on a machine device."""

    _attr_has_entity_name = True
    _local = False  # True = stays available while the machine is offline

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str, key: str) -> None:
        super().__init__(coordinator)
        self.machine_id = machine_id
        self._attr_unique_id = f"{machine_id}_{key}"
        mach = coordinator.machine(machine_id)
        info = mach.state.get("info", {}) if mach.state else {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, machine_id)},
            name=coordinator.machine_name(machine_id),
            manufacturer=MANUFACTURER,
            model=info.get("cpu_brand") or "Folding@home client",
            sw_version=info.get("version"),
            via_device=(DOMAIN, coordinator.account_id),
            configuration_url="https://app.foldingathome.org/",
        )

    @property
    def view(self) -> MachineView | None:
        """Parsed machine state (None while offline)."""
        return (self.coordinator.data or {}).get(self.machine_id)

    @property
    def available(self) -> bool:
        """Available while the machine is online."""
        if self._local:
            return self.machine_id in self.coordinator.contexts
        return self.view is not None
