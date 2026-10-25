"""WebSocket API used by the dashboard card.

``foldingathome/dashboard`` returns, per account, the entity ids of every
machine's controls (so the card never has to guess entity ids) plus the
merged list of projects with their descriptions. Live values come from the
normal HA states the card already receives.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, GROUP_BOOL_KEYS
from .projects import project_topic

MACHINE_KEYS = {
    "sensor": [
        "status", "ppd", "current_project", "running_units", "cpus_used",
        "projects_worked", "wus_completed", "last_wu", "solar_status", "version",
    ],
    "binary_sensor": ["online", "folding"],
    "button": ["button_fold", "button_pause", "button_finish"],
    "select": ["mode", "solar_stop_action"],
    "switch": ["folding_switch", "solar_control"],
    "number": [
        "solar_priority", "solar_start_w", "solar_stop_w", "solar_start_delay",
        "solar_stop_delay", "solar_finish_above", "solar_min_soc", "solar_stop_soc",
        "solar_max_import_w",
    ],
}
ACCOUNT_KEYS = {
    "sensor": ["node", "machines_online", "total_ppd", "identity", "score", "rank", "team_score", "wus_24h"],
    "binary_sensor": ["node_connected"],
    "button": ["button_fold_all", "button_pause_all", "button_finish_all", "button_refresh"],
}
SLOT_KEYS = ["status", "progress", "ppd", "eta", "project"]


@callback
def async_register_websocket(hass: HomeAssistant) -> None:
    """Register the commands."""
    websocket_api.async_register_command(hass, ws_dashboard)
    websocket_api.async_register_command(hass, ws_research)


def _entities(reg: er.EntityRegistry, prefix: str, keys: dict[str, list[str]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for platform, names in keys.items():
        for name in names:
            eid = reg.async_get_entity_id(platform, DOMAIN, f"{prefix}_{name}")
            if eid:
                out[name] = eid
    return out


def _slot(reg: er.EntityRegistry, mid: str, slot_key: str) -> dict[str, str]:
    out = {}
    for key in SLOT_KEYS:
        eid = reg.async_get_entity_id("sensor", DOMAIN, f"{mid}_slot_{slot_key}_{key}")
        if eid:
            out[key] = eid
    return out


def build_dashboard(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Dashboard layout + projects for every loaded account."""
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    accounts = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        c = entry.runtime_data
        machines = []
        projects: dict[str, dict[str, Any]] = {}
        for mid, ctx in c.contexts.items():
            device = dev_reg.async_get_device({(DOMAIN, mid)})
            # The name shown in HA (renamed devices keep the user's name)
            name = (device.name_by_user or device.name) if device else c.machine_name(mid)
            view = (c.data or {}).get(mid)
            cpus = []
            gpus = []
            groups = list(view.groups) if view else [""]
            for slot in view.slots if view else []:
                if slot.kind == "cpu":
                    group = slot.group or ""
                    cpus.append(
                        {
                            "group": group,
                            "name": slot.name,
                            "sensors": _slot(reg, mid, slot.key),
                            "cpus": reg.async_get_entity_id("number", DOMAIN, f"{mid}_group_{group}_cpus"),
                        }
                    )
                elif slot.gpu_id:
                    gpus.append(
                        {
                            "gpu_id": slot.gpu_id,
                            "name": slot.name,
                            "sensors": _slot(reg, mid, slot.key),
                            "switch": reg.async_get_entity_id(
                                "switch", DOMAIN, f"{mid}_gpu_{slot.gpu_id}_enabled"
                            ),
                        }
                    )
            group_switches = {
                group or "default": {
                    key: eid
                    for key in GROUP_BOOL_KEYS
                    if (eid := reg.async_get_entity_id("switch", DOMAIN, f"{mid}_group_{group}_{key}"))
                }
                for group in groups
            }
            machines.append(
                {
                    "id": mid,
                    "name": name,
                    "device_id": device.id if device else None,
                    "online": ctx.machine.online,
                    "entities": _entities(reg, mid, MACHINE_KEYS),
                    "cpus": cpus,
                    "gpus": gpus,
                    "group_switches": group_switches,
                }
            )
            # projects worked on this machine
            for item in ctx.tracker.summary_list(limit=500):
                key = str(item["project"])
                proj = projects.setdefault(
                    key,
                    {
                        **{k: item.get(k) for k in ("project", "cause", "institution", "manager", "description")},
                        "wus": 0,
                        "credited": 0,
                        "last_seen": item.get("last_seen"),
                        "machines": [],
                        "current_on": [],
                    },
                )
                proj["wus"] += item.get("wus") or 0
                proj["credited"] += item.get("credited") or 0
                if (item.get("last_seen") or "") > (proj.get("last_seen") or ""):
                    proj["last_seen"] = item.get("last_seen")
                if name not in proj["machines"]:
                    proj["machines"].append(name)
            # projects running right now
            for p in view.projects if view else []:
                key = str(p)
                info = ctx.tracker.project_info.get(key) or {}
                proj = projects.setdefault(
                    key,
                    {
                        "project": p,
                        **{k: info.get(k) for k in ("cause", "institution", "manager", "description")},
                        "wus": 0,
                        "credited": 0,
                        "last_seen": None,
                        "machines": [],
                        "current_on": [],
                    },
                )
                if name not in proj["current_on"]:
                    proj["current_on"].append(name)
        ordered = list(projects.values())
        for proj in ordered:
            proj["topic"], proj["topic_label"] = project_topic(proj)
        # current first, then most recently seen
        current = [p for p in ordered if p["current_on"]]
        rest = sorted(
            (p for p in ordered if not p["current_on"]),
            key=lambda p: p.get("last_seen") or "",
            reverse=True,
        )
        accounts.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "account": _entities(reg, c.account_id, ACCOUNT_KEYS),
                "machines": sorted(machines, key=lambda m: m["name"].lower()),
                "projects": current + rest,
            }
        )
    return accounts


@websocket_api.websocket_command({vol.Required("type"): "foldingathome/dashboard"})
@callback
def ws_dashboard(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the dashboard layout."""
    connection.send_result(msg["id"], {"accounts": build_dashboard(hass)})


def _display_name(dev_reg: dr.DeviceRegistry, c: Any, mid: str) -> str:
    device = dev_reg.async_get_device({(DOMAIN, mid)})
    return (device.name_by_user or device.name) if device else c.machine_name(mid)


def build_research(hass: HomeAssistant, hours: float) -> list[dict[str, Any]]:
    """Work units per research topic for every loaded account."""
    reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)
    accounts = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED:
            continue
        c = entry.runtime_data
        names = {c.machine_name(mid): _display_name(dev_reg, c, mid) for mid in c.contexts}
        topics = [
            {
                **item,
                "machines": sorted({names.get(n, n) for n in item["machines"]}),
                "entity": reg.async_get_entity_id(
                    "sensor", DOMAIN, f"{c.account_id}_topic_{item['topic'].replace('-', '_')}"
                ),
            }
            for item in c.topic_stats(hours or None)
        ]
        accounts.append(
            {
                "entry_id": entry.entry_id,
                "title": entry.title,
                "donor": c.account.get("user"),
                "hours": hours,
                "total": sum(t["wus"] for t in topics),
                "running": sum(t["running"] for t in topics),
                "topics": topics,
            }
        )
    return accounts


@websocket_api.websocket_command(
    {
        vol.Required("type"): "foldingathome/research",
        # 0 = all time; the log of finish times covers the last 31 days
        vol.Optional("hours", default=24): vol.All(vol.Coerce(float), vol.Range(min=0, max=744)),
    }
)
@callback
def ws_research(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return work units per research topic."""
    connection.send_result(msg["id"], {"accounts": build_research(hass, msg["hours"])})
