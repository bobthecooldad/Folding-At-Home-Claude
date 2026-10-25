"""Switches: folding, solar control, per-group options."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_SOLAR_ENABLED, STATE_FOLD, STATE_PAUSE, STATUS_PAUSED
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahMachineEntity
from .model import group_config

PARALLEL_UPDATES = 1

GROUP_SWITCHES = {
    "on_idle": ("Only when idle", "mdi:sleep"),
    "on_battery": ("While on battery", "mdi:battery"),
    "keep_awake": ("Keep awake", "mdi:coffee"),
    "beta": ("Beta projects", "mdi:beta"),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up switches."""
    coordinator = entry.runtime_data

    @callback
    def add_machine(mid: str) -> None:
        async_add_entities([FahFoldingSwitch(coordinator, mid), FahSolarSwitch(coordinator, mid)])

    known: set[tuple[str, str]] = set()

    @callback
    def add_groups() -> None:
        new: list[SwitchEntity] = []
        for mid, view in (coordinator.data or {}).items():
            if mid not in coordinator.contexts:
                continue
            for group in view.groups:
                if (mid, group) in known:
                    continue
                known.add((mid, group))
                new.extend(FahGroupSwitch(coordinator, mid, group, key) for key in GROUP_SWITCHES)
            for slot in view.slots:
                if slot.kind != "gpu" or not slot.gpu_id or (mid, slot.key) in known:
                    continue
                known.add((mid, slot.key))
                new.append(FahGpuSwitch(coordinator, mid, slot.gpu_id, slot.key, slot.name))
        if new:
            async_add_entities(new)

    coordinator.register_machine_adder(add_machine)
    add_groups()
    coordinator.register_update_adder(add_groups)


class FahFoldingSwitch(FahMachineEntity, SwitchEntity):
    """On = fold, off = pause."""

    _attr_translation_key = "folding"
    _attr_icon = "mdi:molecule"

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str) -> None:
        super().__init__(coordinator, machine_id, "folding_switch")

    @property
    def is_on(self) -> bool | None:
        """Folding (or finishing)?"""
        v = self.view
        return None if v is None else v.status != STATUS_PAUSED

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Fold."""
        await self.coordinator.async_set_state(self.machine_id, STATE_FOLD)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause."""
        await self.coordinator.async_set_state(self.machine_id, STATE_PAUSE)


class FahSolarSwitch(FahMachineEntity, SwitchEntity):
    """Enable/disable solar control for this machine."""

    _attr_translation_key = "solar_control"
    _attr_icon = "mdi:solar-power-variant"
    _local = True

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str) -> None:
        super().__init__(coordinator, machine_id, "solar_control")

    @property
    def is_on(self) -> bool:
        """Enabled?"""
        return bool(self.coordinator.machine_options(self.machine_id)[CONF_SOLAR_ENABLED])

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        self.coordinator.async_update_machine_options(self.machine_id, {CONF_SOLAR_ENABLED: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        self.coordinator.async_update_machine_options(self.machine_id, {CONF_SOLAR_ENABLED: False})


class FahGroupSwitch(FahMachineEntity, SwitchEntity):
    """Boolean option of a resource group."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str, group: str, key: str) -> None:
        super().__init__(coordinator, machine_id, f"group_{group}_{key}")
        self._group = group
        self._key = key
        label, icon = GROUP_SWITCHES[key]
        self._attr_name = f"{group} {label}" if group else label
        self._attr_icon = icon

    @property
    def available(self) -> bool:
        """Only while the group exists."""
        return super().available and self._group in self.view.groups

    @property
    def is_on(self) -> bool | None:
        """Current value."""
        value = group_config(self.coordinator.machine(self.machine_id).state, self._group).get(self._key)
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable."""
        await self.coordinator.async_configure_group(self.machine_id, self._group, **{self._key: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable."""
        await self.coordinator.async_configure_group(self.machine_id, self._group, **{self._key: False})


class FahGpuSwitch(FahMachineEntity, SwitchEntity):
    """Fold on this GPU (the web app's GPU "Enabled" checkbox)."""

    _attr_icon = "mdi:expansion-card"

    def __init__(
        self, coordinator: FahAccountCoordinator, machine_id: str, gpu_id: str, slot_key: str, name: str
    ) -> None:
        super().__init__(coordinator, machine_id, f"gpu_{gpu_id}_enabled")
        self._gpu_id = gpu_id
        self._slot_key = slot_key
        self._attr_name = name

    @property
    def available(self) -> bool:
        """Only while the GPU is detected."""
        return super().available and self.view.slot(self._slot_key) is not None

    @property
    def is_on(self) -> bool | None:
        """Enabled in a resource group?"""
        slot = self.view.slot(self._slot_key) if self.view else None
        return None if slot is None else slot.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """GPU details."""
        slot = self.view.slot(self._slot_key) if self.view else None
        if slot is None:
            return None
        return {"gpu_id": self._gpu_id, "resource_group": slot.group, "supported": slot.supported}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the GPU."""
        await self.coordinator.async_set_gpu_enabled(self.machine_id, self._gpu_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the GPU."""
        await self.coordinator.async_set_gpu_enabled(self.machine_id, self._gpu_id, False)
