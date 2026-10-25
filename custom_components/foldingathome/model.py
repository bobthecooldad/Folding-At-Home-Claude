"""Derive friendly values from the raw Folding@home client state.

Pure functions, no Home Assistant imports, so they are easy to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import re
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


def gpu_name(state: dict[str, Any], gpu_id: str) -> str:
    """Friendly GPU name."""
    gpu = state.get("info", {}).get("gpus", {}).get(gpu_id, {})
    desc = gpu.get("description") if isinstance(gpu, dict) else None
    return desc or f"GPU {gpu_id}"


def unit_slot(state: dict[str, Any], unit: dict[str, Any]) -> tuple[str, str]:
    """Stable slot key + name for a unit (resource it runs on)."""
    group = unit.get("group", "")
    gpus = unit.get("gpus") or []
    prefix = f"{group} " if group else ""
    if gpus:
        gid = str(gpus[0])
        return f"{group}|gpu:{gid}", f"{prefix}{gpu_name(state, gid)}".strip()
    return f"{group}|cpu", f"{prefix}CPU".strip()


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
        gpus=[str(g) for g in unit.get("gpus") or []],
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
    gpus = {str(gid): gpu_name(state, str(gid)) for gid in (info.get("gpus") or {})}
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
        ppd=sum(u.ppd for u in units if u.state in ("RUN", "FINISH")),
        units=units,
        groups=state.get("groups", {}),
        config=state.get("config", {}),
        account=info.get("account") or None,
        failed=failed,
    )
