"""Sensors for Folding@home."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime as dt
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import MACHINE_STATUSES, SOLAR_STATUSES, STATUS_OFFLINE
from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahAccountEntity, FahMachineEntity
from .model import MachineView, UnitView, parse_time

PARALLEL_UPDATES = 0

type C = FahAccountCoordinator
type V = MachineView | None


# ======================================================== machine sensors
@dataclass(frozen=True, kw_only=True)
class FahSensorDescription(SensorEntityDescription):
    """Machine sensor."""

    value_fn: Callable[[C, str, V], Any]
    attr_fn: Callable[[C, str, V], dict[str, Any] | None] | None = None
    local: bool = False  # keeps its value while the machine is offline


def _units_attr(c: C, mid: str, v: V) -> dict[str, Any] | None:
    return {"units": [u.as_attributes() for u in v.units]} if v else None


def _current_project(c: C, mid: str, v: V) -> str | None:
    if not v or not v.projects:
        return None
    return ", ".join(str(p) for p in v.projects)


def _current_project_attr(c: C, mid: str, v: V) -> dict[str, Any] | None:
    if not v:
        return None
    info = c.tracker(mid).project_info
    projects = [
        {"project": p, **{k: (info.get(str(p)) or {}).get(k) for k in ("cause", "manager", "institution", "description")}}
        for p in v.projects
    ]
    return {"projects": projects, "units": [u.as_attributes() for u in v.units]}


def _projects_attr(c: C, mid: str, v: V) -> dict[str, Any]:
    t = c.tracker(mid)
    return {"projects": t.summary_list(), "wus_by_cause": t.by_cause(), "credited_wus": t.credited_count}


def _last_wu(c: C, mid: str, v: V) -> dt.datetime | None:
    wu = c.tracker(mid).last_wu
    return parse_time(wu.get("end_time")) if wu else None


def _status(c: C, mid: str, v: V) -> str:
    return v.status if v else STATUS_OFFLINE


def _status_attr(c: C, mid: str, v: V) -> dict[str, Any]:
    mach = c.machine(mid)
    attrs: dict[str, Any] = {"machine_id": mid, "online": mach.online}
    if v:
        attrs["groups"] = {
            (name or "default"): {
                "paused": g.get("config", {}).get("paused"),
                "finish": g.get("config", {}).get("finish"),
                "cpus": g.get("config", {}).get("cpus"),
            }
            for name, g in v.groups.items()
        }
        attrs["user"] = v.config.get("user")
        attrs["team"] = v.config.get("team")
        attrs["passkey_set"] = bool(v.config.get("passkey"))
        if v.failed:
            attrs["failed"] = v.failed
    return attrs


MACHINE_SENSORS: tuple[FahSensorDescription, ...] = (
    FahSensorDescription(
        key="status",
        translation_key="status",
        device_class=SensorDeviceClass.ENUM,
        options=MACHINE_STATUSES,
        value_fn=_status,
        attr_fn=_status_attr,
        local=True,
    ),
    FahSensorDescription(
        key="ppd",
        translation_key="ppd",
        native_unit_of_measurement="PPD",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c, m, v: v.ppd if v else None,
    ),
    FahSensorDescription(
        key="running_units",
        translation_key="running_units",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c, m, v: len(v.running_units) if v else None,
        attr_fn=_units_attr,
    ),
    FahSensorDescription(
        key="cpus_used",
        translation_key="cpus_used",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c, m, v: v.cpus_used if v else None,
        attr_fn=lambda c, m, v: {"cpus_available": v.cpus_available} if v else None,
    ),
    FahSensorDescription(
        key="current_project",
        translation_key="current_project",
        value_fn=_current_project,
        attr_fn=_current_project_attr,
    ),
    FahSensorDescription(
        key="projects_worked",
        translation_key="projects_worked",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c, m, v: c.tracker(m).project_count,
        attr_fn=_projects_attr,
        local=True,
    ),
    FahSensorDescription(
        key="wus_completed",
        translation_key="wus_completed",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c, m, v: c.tracker(m).wu_count,
        local=True,
    ),
    FahSensorDescription(
        key="base_credit",
        translation_key="base_credit",
        native_unit_of_measurement="points",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda c, m, v: c.tracker(m).base_credit_total,
        local=True,
    ),
    FahSensorDescription(
        key="last_wu",
        translation_key="last_wu",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_last_wu,
        attr_fn=lambda c, m, v: dict(c.tracker(m).last_wu or {}),
        local=True,
    ),
    FahSensorDescription(
        key="solar_status",
        translation_key="solar_status",
        device_class=SensorDeviceClass.ENUM,
        options=SOLAR_STATUSES,
        value_fn=lambda c, m, v: c.solar_status(m),
        attr_fn=lambda c, m, v: c.solar_attributes(m),
        local=True,
    ),
    FahSensorDescription(
        key="version",
        translation_key="version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c, m, v: v.version if v else None,
        attr_fn=lambda c, m, v: {"os": v.os, "cpu": v.cpu_brand, "gpus": v.gpus} if v else None,
    ),
)


@dataclass(frozen=True, kw_only=True)
class FahSlotDescription(SensorEntityDescription):
    """Sensor for the unit running in a resource slot (CPU / each GPU)."""

    value_fn: Callable[[UnitView], Any]


SLOT_SENSORS: tuple[FahSlotDescription, ...] = (
    FahSlotDescription(
        key="progress",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda u: round(u.progress, 2),
    ),
    FahSlotDescription(
        key="ppd",
        native_unit_of_measurement="PPD",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda u: u.ppd,
    ),
    FahSlotDescription(
        key="eta",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        suggested_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=2,
        value_fn=lambda u: u.eta_seconds,
    ),
    FahSlotDescription(key="project", value_fn=lambda u: u.project),
    FahSlotDescription(key="status", value_fn=lambda u: u.status),
)
SLOT_LABEL = {"progress": "progress", "ppd": "PPD", "eta": "ETA", "project": "project", "status": "status"}


# ======================================================== account sensors
@dataclass(frozen=True, kw_only=True)
class FahAccountSensorDescription(SensorEntityDescription):
    """Account sensor."""

    value_fn: Callable[[C], Any]
    attr_fn: Callable[[C], dict[str, Any] | None] | None = None


def _stat(key: str, team: bool = False) -> Callable[[C], Any]:
    def fn(c: C) -> Any:
        if not c.stats:
            return None
        src = c.stats.get("_team") if team else c.stats
        return (src or {}).get(key)

    return fn


def _node_status(c: C) -> str:
    if c.auth_failed:
        return "login_expired"
    return "connected" if c.node.connected else "disconnected"


def _machines_attr(c: C) -> dict[str, Any]:
    return {
        "machines": [
            {
                "name": c.machine_name(mid),
                "online": ctx.machine.online,
                "status": (c.data.get(mid).status if c.data and mid in c.data else STATUS_OFFLINE),
                "ppd": (c.data.get(mid).ppd if c.data and mid in c.data else 0),
            }
            for mid, ctx in c.contexts.items()
        ]
    }


ACCOUNT_SENSORS: tuple[FahAccountSensorDescription, ...] = (
    FahAccountSensorDescription(
        key="node",
        translation_key="node",
        device_class=SensorDeviceClass.ENUM,
        options=["connected", "disconnected", "login_expired"],
        value_fn=_node_status,
        attr_fn=lambda c: {
            "node": c.node.node_host,
            "last_error": c.node.last_error,
        },
    ),
    FahAccountSensorDescription(
        key="machines_online",
        translation_key="machines_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: sum(1 for ctx in c.contexts.values() if ctx.machine.online),
        attr_fn=_machines_attr,
    ),
    FahAccountSensorDescription(
        key="total_ppd",
        translation_key="total_ppd",
        native_unit_of_measurement="PPD",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: sum(v.ppd for v in (c.data or {}).values()),
    ),
    FahAccountSensorDescription(
        key="identity",
        translation_key="identity",
        value_fn=lambda c: c.account.get("user"),
        attr_fn=lambda c: {
            "team": c.account.get("team"),
            "cause": c.account.get("cause"),
            "passkey_set": bool(c.account.get("passkey")),
        },
    ),
    FahAccountSensorDescription(
        key="score",
        translation_key="score",
        native_unit_of_measurement="points",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_stat("score"),
        attr_fn=lambda c: (
            {k: c.stats.get(k) for k in ("name", "last", "active_7", "active_50", "users")}
            if c.stats
            else {"error": c.stats_error}
        ),
    ),
    FahAccountSensorDescription(
        key="stats_wus",
        translation_key="stats_wus",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_stat("wus"),
    ),
    FahAccountSensorDescription(
        key="rank",
        translation_key="rank",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_stat("rank"),
    ),
    FahAccountSensorDescription(
        key="team_score",
        translation_key="team_score",
        native_unit_of_measurement="points",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_stat("tscore", team=True),
        attr_fn=lambda c: {k: ((c.stats or {}).get("_team") or {}).get(k) for k in ("name", "trank", "twus")},
    ),
)


# ================================================================== setup
async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    coordinator = entry.runtime_data
    async_add_entities(FahAccountSensor(coordinator, d) for d in ACCOUNT_SENSORS)

    @callback
    def add_machine(mid: str) -> None:
        async_add_entities(FahMachineSensor(coordinator, mid, d) for d in MACHINE_SENSORS)

    known_slots: set[tuple[str, str]] = set()

    @callback
    def add_slots() -> None:
        new = []
        for mid, view in (coordinator.data or {}).items():
            if mid not in coordinator.contexts:
                continue
            for unit in view.units:
                if (mid, unit.slot) in known_slots:
                    continue
                known_slots.add((mid, unit.slot))
                new.extend(FahSlotSensor(coordinator, mid, d, unit.slot, unit.slot_name) for d in SLOT_SENSORS)
        if new:
            async_add_entities(new)

    coordinator.register_machine_adder(add_machine)
    add_slots()
    coordinator.register_update_adder(add_slots)


class FahAccountSensor(FahAccountEntity, SensorEntity):
    """Account level sensor."""

    entity_description: FahAccountSensorDescription

    def __init__(self, coordinator: C, description: FahAccountSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        """State."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        fn = self.entity_description.attr_fn
        return fn(self.coordinator) if fn else None


class FahMachineSensor(FahMachineEntity, SensorEntity):
    """Machine level sensor."""

    entity_description: FahSensorDescription

    def __init__(self, coordinator: C, machine_id: str, description: FahSensorDescription) -> None:
        super().__init__(coordinator, machine_id, description.key)
        self.entity_description = description
        self._local = description.local

    @property
    def native_value(self) -> Any:
        """State."""
        return self.entity_description.value_fn(self.coordinator, self.machine_id, self.view)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attributes."""
        fn = self.entity_description.attr_fn
        return fn(self.coordinator, self.machine_id, self.view) if fn else None


class FahSlotSensor(FahMachineEntity, SensorEntity):
    """Per CPU / GPU slot sensor."""

    entity_description: FahSlotDescription

    def __init__(
        self, coordinator: C, machine_id: str, description: FahSlotDescription, slot: str, slot_name: str
    ) -> None:
        super().__init__(coordinator, machine_id, f"slot_{slot}_{description.key}")
        self.entity_description = description
        self._slot = slot
        self._attr_name = f"{slot_name} {SLOT_LABEL[description.key]}"

    @property
    def _unit(self) -> UnitView | None:
        return self.view.unit_for_slot(self._slot) if self.view else None

    @property
    def native_value(self) -> Any:
        """State."""
        unit = self._unit
        if unit is None:
            if self.entity_description.key == "status":
                return "Idle"
            if self.entity_description.key in ("progress", "ppd"):
                return 0
            return None
        return self.entity_description.value_fn(unit)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Full unit details on the progress sensor."""
        if self.entity_description.key != "progress":
            return None
        unit = self._unit
        return unit.as_attributes() if unit else None
