"""Solar surplus controller.

Decides when a machine should fold based on a surplus power sensor
(optionally guarded by battery state of charge and grid import), with
start/stop thresholds and delays for hysteresis.

The controller is *edge triggered*: it only sends a command when its own
decision changes, so a manual Fold/Pause from HA or the web UI is respected
until the next solar transition.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any

from .const import (
    CONF_SOLAR_FINISH_ABOVE,
    CONF_SOLAR_MAX_IMPORT_W,
    CONF_SOLAR_MIN_SOC,
    CONF_SOLAR_SOC_ENTITY,
    CONF_SOLAR_START_DELAY,
    CONF_SOLAR_START_W,
    CONF_SOLAR_STOP_ACTION,
    CONF_SOLAR_STOP_DELAY,
    CONF_SOLAR_STOP_SOC,
    CONF_SOLAR_STOP_W,
    SOLAR_DEFAULTS,
    SOLAR_DISABLED,
    SOLAR_NO_SENSOR,
    SOLAR_RUNNING,
    SOLAR_STARTING,
    SOLAR_STOPPING,
    SOLAR_WAITING,
    STATE_FINISH,
    STATE_PAUSE,
    STOP_ACTION_FINISH,
    STOP_ACTION_PAUSE,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class SolarReadings:
    """Current sensor values (None = unavailable / not configured)."""

    surplus: float | None
    soc: float | None = None
    grid_import: float | None = None


@dataclass
class SolarSettings:
    """Thresholds."""

    start_w: float
    stop_w: float
    start_delay_s: float
    stop_delay_s: float
    use_soc: bool
    min_soc: float
    stop_soc: float
    max_import_w: float
    stop_action: str
    finish_above: float

    @classmethod
    def from_options(cls, options: dict[str, Any]) -> SolarSettings:
        """Build from config entry options."""
        o = {**SOLAR_DEFAULTS, **options}
        return cls(
            start_w=float(o[CONF_SOLAR_START_W]),
            stop_w=float(o[CONF_SOLAR_STOP_W]),
            start_delay_s=float(o[CONF_SOLAR_START_DELAY]) * 60,
            stop_delay_s=float(o[CONF_SOLAR_STOP_DELAY]) * 60,
            use_soc=bool(o.get(CONF_SOLAR_SOC_ENTITY)),
            min_soc=float(o[CONF_SOLAR_MIN_SOC]),
            stop_soc=float(o[CONF_SOLAR_STOP_SOC]),
            max_import_w=float(o[CONF_SOLAR_MAX_IMPORT_W]),
            stop_action=str(o[CONF_SOLAR_STOP_ACTION]),
            finish_above=float(o[CONF_SOLAR_FINISH_ABOVE]),
        )


class SolarLogic:
    """Pure decision logic (testable without HA)."""

    def __init__(self, settings: SolarSettings, running: bool) -> None:
        self.settings = settings
        self.on = running  # our current decision
        self._pending_since: float | None = None
        self.reason: str = ""

    def start_ok(self, r: SolarReadings) -> bool:
        """Conditions to start folding."""
        s = self.settings
        if r.surplus is None or r.surplus < s.start_w:
            return False
        if s.use_soc and (r.soc is None or r.soc < s.min_soc):
            return False
        if s.max_import_w > 0 and r.grid_import is not None and r.grid_import > s.max_import_w:
            return False
        return True

    def stop_needed(self, r: SolarReadings) -> str | None:
        """Reason to stop, or None."""
        s = self.settings
        if r.surplus is None:
            return "surplus sensor unavailable"
        if r.surplus < s.stop_w:
            return f"surplus {r.surplus:.0f} W < {s.stop_w:.0f} W"
        if s.use_soc and r.soc is not None and r.soc < s.stop_soc:
            return f"battery {r.soc:.0f}% < {s.stop_soc:.0f}%"
        if s.max_import_w > 0 and r.grid_import is not None and r.grid_import > s.max_import_w:
            return f"grid import {r.grid_import:.0f} W > {s.max_import_w:.0f} W"
        return None

    @property
    def pending(self) -> bool:
        """A transition is being timed."""
        return self._pending_since is not None

    def pending_remaining(self, now: float) -> float | None:
        """Seconds until the pending transition fires."""
        if self._pending_since is None:
            return None
        delay = self.settings.stop_delay_s if self.on else self.settings.start_delay_s
        return max(0.0, delay - (now - self._pending_since))

    def evaluate(self, r: SolarReadings, now: float) -> str | None:
        """Return "start" / "stop" when the decision flips, else None."""
        s = self.settings
        if not self.on:
            if self.start_ok(r):
                if self._pending_since is None:
                    self._pending_since = now
                if now - self._pending_since >= s.start_delay_s:
                    self.on = True
                    self._pending_since = None
                    self.reason = f"surplus {r.surplus:.0f} W ≥ {s.start_w:.0f} W"
                    return "start"
            else:
                self._pending_since = None
            return None

        reason = self.stop_needed(r)
        if reason:
            if self._pending_since is None:
                self._pending_since = now
            if now - self._pending_since >= s.stop_delay_s:
                self.on = False
                self._pending_since = None
                self.reason = reason
                return "stop"
        else:
            self._pending_since = None
        return None

    def status(self) -> str:
        """Controller status for the sensor."""
        if self.on:
            return SOLAR_STOPPING if self.pending else SOLAR_RUNNING
        return SOLAR_STARTING if self.pending else SOLAR_WAITING

    def stop_command(self, max_progress: float) -> str:
        """Which client command to send when stopping."""
        if self.settings.stop_action == STOP_ACTION_PAUSE:
            return STATE_PAUSE
        if self.settings.stop_action == STOP_ACTION_FINISH:
            return STATE_FINISH
        return STATE_FINISH if max_progress >= self.settings.finish_above else STATE_PAUSE


def state_to_float(state: Any) -> float | None:
    """Convert an HA state object to a float."""
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def watt_value(state: Any) -> float | None:
    """Float value in watts (handles kW sensors)."""
    value = state_to_float(state)
    if value is None:
        return None
    unit = str(state.attributes.get("unit_of_measurement") or "").strip()
    if unit.lower() == "kw":
        return value * 1000
    if unit.lower() == "mw":
        return value * 1_000_000
    return value


def monotonic() -> float:
    """Wrapper so tests can patch time."""
    return time.monotonic()


def solar_status_disabled(enabled: bool, surplus_entity: str | None) -> str | None:
    """Status when the controller is not active."""
    if not enabled:
        return SOLAR_DISABLED
    if not surplus_entity:
        return SOLAR_NO_SENSOR
    return None

