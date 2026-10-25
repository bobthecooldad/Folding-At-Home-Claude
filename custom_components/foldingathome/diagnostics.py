"""Diagnostics (secrets redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .account import redact
from .const import CONF_EMAIL, CONF_PASSKEY, CONF_PRIVATE_KEY, CONF_SID
from .coordinator import FahConfigEntry

TO_REDACT = {
    CONF_PASSKEY, CONF_PRIVATE_KEY, CONF_SID, CONF_EMAIL, "token", "key", "user", "account",
    "id", "hostname", "mach_name", "pubkey", "avatar", "email", "name",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: FahConfigEntry
) -> dict[str, Any]:
    """Return diagnostics."""
    c = entry.runtime_data
    machines = {}
    for n, (mid, ctx) in enumerate(c.contexts.items()):
        state = dict(ctx.machine.state)
        state.pop("wus", None)
        state.pop("log", None)
        machines[f"machine_{n}"] = {
            "online": ctx.machine.online,
            "ready": ctx.machine.ready,
            "state": async_redact_data(redact(state), TO_REDACT),
            "projects": ctx.tracker.project_count,
            "wus": ctx.tracker.wu_count,
            "solar_status": c.solar_status(mid),
        }
    return {
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "node_connected": c.node.connected,
        "node_last_error": c.node.last_error,
        "auth_failed": c.auth_failed,
        "machine_count": len(c.contexts),
        "machines": machines,
    }
