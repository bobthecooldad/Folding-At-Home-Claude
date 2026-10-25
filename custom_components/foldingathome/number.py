"""Numbers: team, CPUs per group, solar thresholds."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_SOLAR_FINISH_ABOVE,
    CONF_SOLAR_MAX_IMPORT_W,
    CONF_SOLAR_MIN_SOC,
    CONF_SOLAR_START_DELAY,
    CONF_SOLAR_START_W,
    CONF_SOLAR_STOP_DELAY,
    CONF_SOLAR_STOP_SOC,
    CONF_SOLAR_STOP_W,
)
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahAccountEntity, FahMachineEntity
from .model import group_config

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class FahSolarNumberDescription(NumberEntityDescription):
    """Solar threshold stored in options."""


SOLAR_NUMBERS = (
    FahSolarNumberDescription(
        key=CONF_SOLAR_START_W,
        translation_key="solar_start_w",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=-5000,
        native_max_value=20000,
        native_step=10,
        mode=NumberMode.BOX,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_STOP_W,
        translation_key="solar_stop_w",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=-5000,
        native_max_value=20000,
        native_step=10,
        mode=NumberMode.BOX,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_START_DELAY,
        translation_key="solar_start_delay",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0,
        native_max_value=120,
        native_step=1,
        mode=NumberMode.BOX,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_STOP_DELAY,
        translation_key="solar_stop_delay",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        native_min_value=0,
        native_max_value=120,
        native_step=1,
        mode=NumberMode.BOX,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_FINISH_ABOVE,
        translation_key="solar_finish_above",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_MIN_SOC,
        translation_key="solar_min_soc",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_STOP_SOC,
        translation_key="solar_stop_soc",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
    ),
    FahSolarNumberDescription(
        key=CONF_SOLAR_MAX_IMPORT_W,
        translation_key="solar_max_import_w",
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=20000,
        native_step=10,
        mode=NumberMode.BOX,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up numbers."""
    coordinator = entry.runtime_data
    async_add_entities([FahTeamNumber(coordinator)])

    @callback
    def add_machine(mid: str) -> None:
        async_add_entities(FahSolarNumber(coordinator, mid, d) for d in SOLAR_NUMBERS)

    known: set[tuple[str, str]] = set()

    @callback
    def add_groups() -> None:
        new = []
        for mid, view in (coordinator.data or {}).items():
            if mid not in coordinator.contexts:
                continue
            for group in view.groups:
                if (mid, group) not in known:
                    known.add((mid, group))
                    new.append(FahCpusNumber(coordinator, mid, group))
        if new:
            async_add_entities(new)

    coordinator.register_machine_adder(add_machine)
    add_groups()
    coordinator.register_update_adder(add_groups)


class FahTeamNumber(FahAccountEntity, NumberEntity):
    """Account team number."""

    _attr_translation_key = "team"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = 2147483647
    _attr_native_step = 1
    _attr_icon = "mdi:account-group"

    def __init__(self, coordinator: FahAccountCoordinator) -> None:
        super().__init__(coordinator, "team")

    @property
    def native_value(self) -> float:
        """Team."""
        return float(self.coordinator.account.get("team") or 0)

    async def async_set_native_value(self, value: float) -> None:
        """Set team for the account."""
        await self.coordinator.async_set_identity(team=int(value))
        self.async_write_ha_state()


class FahCpusNumber(FahMachineEntity, NumberEntity):
    """CPUs allocated to a group."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_icon = "mdi:cpu-64-bit"

    def __init__(self, coordinator: FahAccountCoordinator, machine_id: str, group: str) -> None:
        super().__init__(coordinator, machine_id, f"group_{group}_cpus")
        self._group = group
        self._attr_name = f"{group} CPUs" if group else "CPUs"

    @property
    def available(self) -> bool:
        """Only while the group exists."""
        return super().available and self._group in self.view.groups

    @property
    def native_max_value(self) -> float:
        """Max = machine CPU count."""
        v = self.view
        return float(v.cpus_available) if v and v.cpus_available else 256.0

    @property
    def native_value(self) -> float | None:
        """Configured CPUs."""
        return float(group_config(self.coordinator.machine(self.machine_id).state, self._group).get("cpus", 0))

    async def async_set_native_value(self, value: float) -> None:
        """Set CPUs."""
        await self.coordinator.async_configure_group(self.machine_id, self._group, cpus=int(value))


class FahSolarNumber(FahMachineEntity, NumberEntity):
    """Solar threshold of one machine (stored in the options)."""

    entity_description: FahSolarNumberDescription
    _attr_entity_category = EntityCategory.CONFIG
    _local = True

    def __init__(
        self, coordinator: FahAccountCoordinator, machine_id: str, description: FahSolarNumberDescription
    ) -> None:
        super().__init__(coordinator, machine_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        """Value."""
        return float(self.coordinator.machine_options(self.machine_id)[self.entity_description.key])

    async def async_set_native_value(self, value: float) -> None:
        """Save."""
        self.coordinator.async_update_machine_options(self.machine_id, {self.entity_description.key: value})
