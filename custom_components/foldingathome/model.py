"""Derive friendly values from the raw Folding@home client state.

Pure functions, no Home Assistant imports, so they are easy to test.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from .const import (
    STATUS_FINISHING,
    STATUS_FOLDING,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_WAITING,
    UNIT_STATUS,
)

_ETA_RE = re.compile(r"(\d+)\s*([ydhms])", re.IGNORECASE)
_ETA_MULT = {"y": 31536000, "d": 86400, "h": 3600, "m": 60, "s": 1}

ACTIVE_UNIT_STATES = {"ASSIGN", "DOWNLOAD", "CORE", "RUN", "UPLOAD"}


def parse_time(value: Any) -> dt.datetime | None:
    """Parse an ISO time from the client."""
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_eta(value: Any) -> int | None:
    """Parse ``2h 13m`` / ``2 hours 13 mins`` / int seconds into seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    parts = _ETA_RE.findall(value)
    if not parts:
        return None
    return sum(int(n) * _ETA_MULT[u.lower()] for n, u in parts)


def group_config(state: dict[str, Any], group: str | None) -> dict[str, Any]:
    """Config dict of a group."""
    if group is None:
        return {}
    return state.get("groups", {}).get(group, {}).get("config", {})


@dataclass
class UnitView:
    """A friendly view of a work unit."""

    id: str
    group: str
    slot: str
    slot_name: str
    state: str
    status: str
    project: int | None
    run: int | None
    clone: int | None
    gen: int | None
    progress: float
    ppd: int
    eta_seconds: int | None
    eta: str | None
    cpus: int
    gpus: list[str]
    core: str | None
    base_credit: int | None
    assign_time: dt.datetime | None
    deadline: dt.datetime | None
    timeout: dt.datetime | None
    pause_reason: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def prcg(self) -> str:
        """Project (run, clone, gen) string."""
        if self.project is None:
            return ""
        return f"{self.project} ({self.run}, {self.clone}, {self.gen})"

    def as_attributes(self) -> dict[str, Any]:
        """Attributes for HA."""
        return {
            "unit_id": self.id,
            "group": self.group,
            "slot": self.slot_name,
            "status": self.status,
            "project": self.project,
            "prcg": self.prcg,
            "progress": round(self.progress, 2),
            "ppd": self.ppd,
            "eta": self.eta,
            "cpus": self.cpus,
            "gpus": self.gpus,
            "core": self.core,
            "base_credit": self.base_credit,
            "assigned": self.assign_time.isoformat() if self.assign_time else None,
            "timeout": self.timeout.isoformat() if self.timeout else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "pause_reason": self.pause_reason,
        }


def _unit_waiting(unit: dict[str, Any], now: dt.datetime) -> bool:
    wait = parse_time(unit.get("wait"))
    return wait is not None and now < wait


def unit_state(state: dict[str, Any], unit: dict[str, Any], now: dt.datetime) -> str:
    """Synthetic unit state, as the web client shows it."""
    if _unit_waiting(unit, now):
        return "WAIT"
    ustate = unit.get("state", "")
    if ustate == "DONE" and unit.get("result"):
        return str(unit["result"]).upper()
    gconf = group_config(state, unit.get("group"))
    if ustate == "RUN" and gconf.get("finish"):
        return "FINISH"
    if unit.get("pause_reason") or gconf.get("paused", False):
        return "PAUSE"
    return ustate


def gpu_ids(state: dict[str, Any]) -> list[str]:
    """Detected GPU ids, in a stable order."""
    return sorted(str(g) for g in (state.get("info", {}).get("gpus") or {}))


def gpu_name(state: dict[str, Any], gpu_id: str) -> str:
    """Friendly, unique GPU name.

    Several identical cards (e.g. two "Quadro P4000") are numbered so every
    GPU gets its own clearly named entities.
    """
    gpus = state.get("info", {}).get("gpus", {}) or {}
    gpu = gpus.get(gpu_id, {})
    desc = (gpu.get("description") if isinstance(gpu, dict) else None) or "GPU"
    ids = gpu_ids(state)
    if len(ids) > 1 and gpu_id in ids:
        return f"GPU {ids.index(gpu_id) + 1} {desc}"
    return desc


def gpu_slot_key(gpu_id: str) -> str:
    """Slot key of a GPU (independent of the group it is assigned to)."""
    return f"|gpu:{gpu_id}"


def cpu_slot_key(group: str) -> str:
    """Slot key of a group's CPU unit."""
    return f"{group}|cpu"


def cpu_slot_name(group: str) -> str:
    """Name of a group's CPU slot."""
    return f"{group} CPU" if group else "CPU"


def gpu_group(state: dict[str, Any], gpu_id: str) -> str | None:
    """Group the GPU is enabled in (None = not enabled anywhere)."""
    for name, group in (state.get("groups") or {}).items():
        conf = (group.get("config") or {}).get("gpus") or {}
        if (conf.get(gpu_id) or {}).get("enabled"):
            return name
    return None


def unit_gpus(unit: dict[str, Any]) -> list[str]:
    """GPUs of a unit (``unit.gpus``, or the assignment's before it starts)."""
    gpus = unit.get("gpus") or (unit.get("assignment") or {}).get("gpus") or []
    return [str(g) for g in gpus]


def unit_slot(state: dict[str, Any], unit: dict[str, Any]) -> tuple[str, str]:
    """Stable slot key + name for a unit (resource it runs on)."""
    gpus = unit_gpus(unit)
    if gpus:
        return gpu_slot_key(gpus[0]), gpu_name(state, gpus[0])
    group = unit.get("group", "")
    return cpu_slot_key(group), cpu_slot_name(group)


@dataclass
class SlotView:
    """A resource slot: the CPU of a group, or one GPU."""

    key: str
    name: str
    kind: str  # "cpu" | "gpu"
    gpu_id: str | None = None
    group: str | None = None  # group the CPU/GPU belongs to
    enabled: bool = True
    supported: bool = True


def build_unit(state: dict[str, Any], unit: dict[str, Any], now: dt.datetime) -> UnitView:
    """Build a UnitView."""
    assignment = unit.get("assignment") or {}
    wu = unit.get("wu") or {}
    ustate = unit_state(state, unit, now)
    pause_reason = unit.get("pause_reason")
    status = pause_reason or UNIT_STATUS.get(ustate, ustate.title())
    progress = unit.get("wu_progress", unit.get("progress", 0)) or 0
    if ustate == "WAIT" and unit.get("wait_progress") is not None:
        progress = unit["wait_progress"]
    assign_time = parse_time(assignment.get("time"))
    deadline = timeout = None
    if assign_time is not None:
        if assignment.get("deadline"):
            deadline = assign_time + dt.timedelta(seconds=assignment["deadline"])
        if assignment.get("timeout"):
            timeout = assign_time + dt.timedelta(seconds=assignment["timeout"])
    slot, slot_name = unit_slot(state, unit)
    eta = unit.get("eta")
    core = (assignment.get("core") or {}).get("type")
    return UnitView(
        id=str(unit.get("id", "")),
        group=unit.get("group", ""),
        slot=slot,
        slot_name=slot_name,
        state=ustate,
        status=status,
        project=assignment.get("project"),
        run=wu.get("run"),
        clone=wu.get("clone"),
        gen=wu.get("gen"),
        progress=float(progress) * 100.0,
        ppd=int(unit.get("ppd", 0) or 0),
        eta_seconds=parse_eta(eta),
        eta=eta if isinstance(eta, str) else None,
        cpus=int(unit.get("cpus", 0) or 0),
        gpus=unit_gpus(unit),
        core=core,
        base_credit=assignment.get("credit"),
        assign_time=assign_time,
        deadline=deadline,
        timeout=timeout,
        pause_reason=pause_reason,
        raw=unit,
    )


@dataclass
class MachineView:
    """Aggregated machine data used by entities."""

    status: str
    version: str | None
    machine_name: str | None
    os: str | None
    cpu_brand: str | None
    cpus_available: int
    cpus_used: int
    gpus: dict[str, str]
    slots: list[SlotView]
    ppd: int
    units: list[UnitView]
    groups: dict[str, dict[str, Any]]
    config: dict[str, Any]
    account: str | None
    failed: str | None

    @property
    def running_units(self) -> list[UnitView]:
        """Units actively computing."""
        return [u for u in self.units if u.state in ("RUN", "FINISH")]

    @property
    def projects(self) -> list[int]:
        """Distinct projects currently assigned."""
        seen: list[int] = []
        for u in self.units:
            if u.project is not None and u.project not in seen:
                seen.append(u.project)
        return seen

    def slot(self, key: str) -> SlotView | None:
        """Slot by key."""
        return next((s for s in self.slots if s.key == key), None)

    def unit_for_slot(self, slot: str) -> UnitView | None:
        """Best unit occupying a slot (the running one if several)."""
        candidates = [u for u in self.units if u.slot == slot]
        if not candidates:
            return None
        candidates.sort(key=lambda u: (u.state not in ("RUN", "FINISH"), -u.progress))
        return candidates[0]

    @property
    def max_progress(self) -> float:
        """Highest progress of a running unit (for smart solar stop)."""
        return max((u.progress for u in self.running_units), default=0.0)


def machine_status(state: dict[str, Any], units: list[UnitView]) -> str:
    """Overall status of the machine."""
    groups = state.get("groups", {})
    if not groups:
        return STATUS_IDLE
    active = [g for g, d in groups.items() if not d.get("config", {}).get("paused", True)]
    if not active:
        return STATUS_PAUSED
    active_units = [u for u in units if u.group in active]
    if any(group_config(state, g).get("finish") for g in active):
        return STATUS_FINISHING
    if any(u.state == "RUN" for u in active_units):
        return STATUS_FOLDING
    if any(u.state in ACTIVE_UNIT_STATES for u in active_units):
        return STATUS_FOLDING
    if any(u.state in ("PAUSE", "WAIT") for u in active_units):
        return STATUS_WAITING
    if active_units:
        return STATUS_WAITING
    return STATUS_IDLE


def build_machine(state: dict[str, Any], now: dt.datetime | None = None) -> MachineView:
    """Build a MachineView from the client state."""
    now = now or dt.datetime.now(dt.UTC)
    info = state.get("info", {})
    units = [build_unit(state, u, now) for u in state.get("units", []) if isinstance(u, dict)]
    gpus = {gid: gpu_name(state, gid) for gid in gpu_ids(state)}
    slots: list[SlotView] = []
    for gname, gdata in (state.get("groups") or {}).items():
        cpus = (gdata.get("config") or {}).get("cpus", 0)
        slots.append(
            SlotView(cpu_slot_key(gname), cpu_slot_name(gname), "cpu", group=gname, enabled=bool(cpus))
        )
    for gid in gpu_ids(state):
        ginfo = (info.get("gpus") or {}).get(gid) or {}
        owner = gpu_group(state, gid)
        slots.append(
            SlotView(
                gpu_slot_key(gid),
                gpus[gid],
                "gpu",
                gpu_id=gid,
                group=owner,
                enabled=owner is not None,
                supported=bool(ginfo.get("supported", True)) if isinstance(ginfo, dict) else True,
            )
        )
    known = {sl.key for sl in slots}
    for u in units:  # e.g. a unit on a GPU that is no longer detected
        if u.slot not in known:
            known.add(u.slot)
            slots.append(SlotView(u.slot, u.slot_name, "gpu" if u.gpus else "cpu", group=u.group))
    failed = None
    for g in state.get("groups", {}).values():
        if g.get("failed"):
            failed = g["failed"]
    return MachineView(
        status=machine_status(state, units),
        version=info.get("version"),
        machine_name=info.get("mach_name") or info.get("hostname"),
        os=info.get("os"),
        cpu_brand=info.get("cpu_brand"),
        cpus_available=int(info.get("cpus", 0) or 0),
        cpus_used=sum(u.cpus for u in units if u.state in ("RUN", "FINISH")),
        gpus=gpus,
        slots=slots,
        ppd=sum(u.ppd for u in units if u.state in ("RUN", "FINISH")),
        units=units,
        groups=state.get("groups", {}),
        config=state.get("config", {}),
        account=info.get("account") or None,
        failed=failed,
    )
