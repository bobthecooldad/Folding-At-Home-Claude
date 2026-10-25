"""Coordinator: one per Folding@home account, managing all linked machines."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .account import (
    FahAccountApi,
    FahApiError,
    FahAuthError,
    FahConnectionError,
    FahMachine,
    FahNode,
    valid_node_host,
)
from .const import (
    ACCOUNT_REFRESH,
    CONF_ACCOUNT,
    CONF_EMAIL,
    CONF_MACHINES,
    CONF_PRIVATE_KEY,
    CONF_SID,
    CONF_SOLAR_ENABLED,
    CONF_SOLAR_IMPORT_ENTITY,
    CONF_SOLAR_SOC_ENTITY,
    CONF_SOLAR_SURPLUS_ENTITY,
    DOMAIN,
    EVENT_SOLAR_ACTION,
    EVENT_WU_COMPLETED,
    SOLAR_DEFAULTS,
    STATE_FINISH,
    STATE_FOLD,
    STATS_INTERVAL_SECONDS,
    STATUS_FINISHING,
    STATUS_FOLDING,
    STATUS_WAITING,
    STORAGE_VERSION,
)
from .model import MachineView, build_machine
from .projects import TOPIC_OTHER, ProjectTracker, info_is_current, project_topic
from .solar import (
    SolarLogic,
    SolarReadings,
    SolarSettings,
    monotonic,
    solar_status_disabled,
    state_to_float,
    watt_value,
)

_LOGGER = logging.getLogger(__name__)

PUSH_DEBOUNCE = 2.0
SOLAR_TICK = dt.timedelta(seconds=30)

type FahConfigEntry = ConfigEntry[FahAccountCoordinator]


class MachineContext:
    """Per machine HA-side data: project tracker + solar controller."""

    def __init__(self, machine: FahMachine, tracker: ProjectTracker) -> None:
        self.machine = machine
        self.tracker = tracker
        self.solar_logic: SolarLogic | None = None
        self.solar_unsubs: list[CALLBACK_TYPE] = []
        self.solar_last_action: str | None = None
        self.solar_last_action_time: dt.datetime | None = None
        self.solar_readings = SolarReadings(surplus=None)
        self.history_imported = tracker.wu_count > 0


class FahAccountCoordinator(DataUpdateCoordinator[dict[str, MachineView]]):
    """Live connection to the account's node + all machines."""

    config_entry: FahConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FahConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {entry.data[CONF_EMAIL]}",
            update_interval=dt.timedelta(seconds=60),
            always_update=True,
        )
        # Own session with a cookie jar (the API may also set a cookie)
        self.session = async_create_clientsession(hass)
        self.api = FahAccountApi(self.session)
        self.account: dict[str, Any] = dict(entry.data[CONF_ACCOUNT])
        self.node = FahNode(
            self.session,
            self.account["node"],
            self.account["pubkey"],
            entry.data[CONF_PRIVATE_KEY],
            on_machine_online=self._on_machine_online,
            on_machine_offline=self._on_machine_offline,
            on_machine_content=self._on_machine_content,
            on_broadcast=self._on_broadcast,
            on_connection_change=self._on_node_connection,
        )
        self.account_id = self.node.account_id
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.contexts: dict[str, MachineContext] = {}
        self._stored: dict[str, Any] = {}
        self.stats: dict[str, Any] | None = None
        self.stats_error: str | None = None
        self.auth_failed = False
        self._push_unsub: CALLBACK_TYPE | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._info_task: asyncio.Task | None = None
        self._machine_adders: list[Callable[[str], None]] = []
        self._update_adders: list[Callable[[], None]] = []
        self._solar_lock = asyncio.Lock()
        self._topic_cache: dict[float, tuple[float, list[dict[str, Any]]]] = {}
        self.data = {}

    # ---------------------------------------------------------------- setup
    @property
    def sid(self) -> str:
        """REST session id."""
        return self.config_entry.data[CONF_SID]

    async def async_start(self) -> None:
        """Load history, refresh the account, connect to the node."""
        self._stored = await self.store.async_load() or {}
        try:
            await self.async_refresh_account()
        except FahConnectionError as err:
            _LOGGER.warning("Folding@home API not reachable (%s); using cached account", err)
        for mach in self.account.get("machines") or []:
            if mach.get("id"):
                self._context(mach["id"], mach.get("name"))
        self.node.start()
        if not await self.node.wait_connected(15):
            _LOGGER.warning(
                "Folding@home node %s not reachable yet (%s); will keep retrying",
                self.node.node_host,
                self.node.last_error,
            )
        else:
            # Give linked machines a moment to send their state
            await self._wait_for_machines(8)
        self._unsubs.append(
            async_track_time_interval(self.hass, self._refresh_account_cb, ACCOUNT_REFRESH)
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._refresh_stats_cb, dt.timedelta(seconds=STATS_INTERVAL_SECONDS)
            )
        )
        self.config_entry.async_create_background_task(
            self.hass, self.async_refresh_stats(), "fah stats"
        )
        self.async_setup_solar()

    async def _wait_for_machines(self, timeout: float) -> None:
        expected = {m["id"] for m in self.account.get("machines") or [] if m.get("id")}
        for _ in range(int(timeout / 0.25)):
            if all(self.node.machine(mid).ready for mid in expected):
                return
            await asyncio.sleep(0.25)

    async def async_shutdown(self) -> None:
        """Close everything."""
        await super().async_shutdown()
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for ctx in self.contexts.values():
            for unsub in ctx.solar_unsubs:
                unsub()
            ctx.solar_unsubs.clear()
        if self._push_unsub:
            self._push_unsub()
            self._push_unsub = None
        if self._info_task:
            self._info_task.cancel()
        await self.node.stop()
        await self.store.async_save(self._storage_data())

    def _storage_data(self) -> dict[str, Any]:
        data = dict(self._stored)
        for mid, ctx in self.contexts.items():
            data[mid] = ctx.tracker.as_dict()
        return data

    def _save_later(self, delay: float = 30) -> None:
        self.store.async_delay_save(self._storage_data, delay)

    async def _async_update_data(self) -> dict[str, MachineView]:
        return self._build_all()

    def _build_all(self) -> dict[str, MachineView]:
        self._topic_cache.clear()
        out: dict[str, MachineView] = {}
        for mid, ctx in self.contexts.items():
            if ctx.machine.connected and ctx.machine.state:
                try:
                    out[mid] = build_machine(ctx.machine.state)
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Failed to parse state of %s", ctx.machine.name)
        return out

    # ------------------------------------------------------------ machines
    def _context(self, machine_id: str, name: str | None = None) -> MachineContext:
        ctx = self.contexts.get(machine_id)
        if ctx is None:
            mach = self.node.machine(machine_id, name)
            tracker = ProjectTracker(self._stored.get(machine_id))
            ctx = self.contexts[machine_id] = MachineContext(mach, tracker)
            for adder in self._machine_adders:
                adder(machine_id)
            self.async_setup_solar(machine_id)
        elif name:
            ctx.machine.name = name
        return ctx

    def machine_name(self, machine_id: str) -> str:
        """Display name of a machine."""
        ctx = self.contexts.get(machine_id)
        if ctx and ctx.machine.name:
            return ctx.machine.name
        if ctx and ctx.machine.state:
            info = ctx.machine.state.get("info", {})
            return info.get("mach_name") or info.get("hostname") or machine_id[:8]
        return machine_id[:8]

    # ------------------------------------------------------------- topics
    def project_info_all(self) -> dict[str, dict[str, Any]]:
        """Project descriptions known on any machine."""
        merged: dict[str, dict[str, Any]] = {}
        for ctx in self.contexts.values():
            merged.update(ctx.tracker.project_info)
        return merged

    def topic_stats(self, hours: float | None = None) -> list[dict[str, Any]]:
        """Work units per research topic, all time or for the last ``hours``.

        Cached for a few seconds, because several entities and the card ask
        for the same numbers on each update.
        """
        cache_key = float(hours or 0)
        now = time.monotonic()
        cached = self._topic_cache.get(cache_key)
        if cached and now - cached[0] < 5:
            return cached[1]
        info = self.project_info_all()
        topics: dict[str, dict[str, Any]] = {}

        def bucket(project: Any) -> dict[str, Any]:
            key, label = project_topic(info.get(str(project)))
            return topics.setdefault(
                key,
                {"topic": key, "label": label, "wus": 0, "running": 0, "projects": set(), "machines": set()},
            )

        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours) if hours else None
        for mid, ctx in self.contexts.items():
            name = self.machine_name(mid)
            if since is not None:
                for _when, project in ctx.tracker.recent_since(since):
                    b = bucket(project)
                    b["wus"] += 1
                    b["projects"].add(int(project))
                    b["machines"].add(name)
            else:
                for key, proj in ctx.tracker.projects.items():
                    b = bucket(key)
                    b["wus"] += proj.get("wus") or 0
                    b["projects"].add(int(key))
                    b["machines"].add(name)
            view = (self.data or {}).get(mid)
            for unit in view.running_units if view else []:
                if unit.project is None:
                    continue
                b = bucket(unit.project)
                b["running"] += 1
                b["projects"].add(int(unit.project))
                b["machines"].add(name)
        result = sorted(
            (
                {**b, "projects": sorted(b["projects"]), "machines": sorted(b["machines"])}
                for b in topics.values()
            ),
            key=lambda b: (b["topic"] == TOPIC_OTHER[0], -b["wus"], -b["running"], b["label"]),
        )
        self._topic_cache[cache_key] = (now, result)
        return result

    def register_machine_adder(self, adder: Callable[[str], None]) -> None:
        """Platforms add entities for each (new) machine."""
        self._machine_adders.append(adder)
        for mid in list(self.contexts):
            adder(mid)

    def register_update_adder(self, adder: Callable[[], None]) -> None:
        """Platforms add entities for new slots / groups after updates."""
        self._update_adders.append(adder)

    @callback
    def async_forget_machine(self, machine_id: str) -> None:
        """Drop a machine (unlinked / device deleted), stopping its solar timers."""
        ctx = self.contexts.pop(machine_id, None)
        for unsub in ctx.solar_unsubs if ctx else []:
            unsub()

    def machine(self, machine_id: str) -> FahMachine:
        """FahMachine for an id."""
        return self.contexts[machine_id].machine

    def tracker(self, machine_id: str) -> ProjectTracker:
        """Project tracker of a machine."""
        return self.contexts[machine_id].tracker

    # ----------------------------------------------------------- callbacks
    @callback
    def _on_node_connection(self, connected: bool) -> None:
        self._publish_now()

    @callback
    def _on_machine_online(self, mach: FahMachine) -> None:
        self._context(mach.id)
        _LOGGER.info("Folding@home machine %s online", self.machine_name(mach.id))
        self._publish_now()
        self.config_entry.async_create_background_task(
            self.hass, self._async_machine_online(mach.id), "fah machine online"
        )

    async def _async_machine_online(self, machine_id: str) -> None:
        # The client may have missed an identity change while offline
        ident = {k: self.account.get(k) for k in ("user", "team", "passkey", "cause")}
        cfg = self.machine(machine_id).state.get("config", {})
        diff = {
            k: v
            for k, v in ident.items()
            if v not in (None, "") and cfg.get(k) != v and not (k == "passkey" and not v)
        }
        if diff and self.machine(machine_id).state.get("info", {}).get("account"):
            # Linked machines get the account config from the server on restart,
            # pushing it here just avoids waiting for that.
            try:
                await self.machine(machine_id).configure_global(**diff)
            except FahConnectionError:
                pass
        self._schedule_project_info(machine_id)

    @callback
    def _on_machine_offline(self, mach: FahMachine) -> None:
        if mach.id in self.contexts:
            _LOGGER.info("Folding@home machine %s offline", self.machine_name(mach.id))
        self._schedule_publish()

    @callback
    def _on_machine_content(self, mach: FahMachine, content: Any) -> None:
        if mach.id not in self.contexts:
            self._context(mach.id)
        if isinstance(content, dict):
            if isinstance(content.get("wus"), list):
                self._ingest_wus(mach.id, content["wus"], incremental=False)
            self._schedule_publish()
            return
        if not isinstance(content, list) or not content:
            return
        if content[0] == "wus":
            if len(content) == 2 and isinstance(content[1], list):
                self._ingest_wus(mach.id, content[1], incremental=False)
            elif len(content) == 3 and content[1] in (-1, -2):
                items = content[2] if content[1] == -2 else [content[2]]
                self._ingest_wus(mach.id, items, incremental=True)
            return
        if content[0] == "log":
            return
        self._schedule_publish()

    @callback
    def _on_broadcast(self, payload: dict[str, Any]) -> None:
        # Another web session changed account settings: refresh soon
        if payload.get("cmd") in ("config", "restart"):
            self.config_entry.async_create_background_task(
                self.hass, self.async_refresh_account(), "fah account refresh"
            )

    @callback
    def _schedule_publish(self) -> None:
        if self._push_unsub is not None:
            return

        @callback
        def _fire(_now: Any) -> None:
            self._push_unsub = None
            self._publish_now()

        self._push_unsub = async_call_later(self.hass, PUSH_DEBOUNCE, _fire)

    @callback
    def _publish_now(self) -> None:
        self.async_set_updated_data(self._build_all())
        for adder in self._update_adders:
            adder()

    # --------------------------------------------------------------- account
    async def _refresh_account_cb(self, _now: Any) -> None:
        try:
            await self.async_refresh_account()
        except FahConnectionError as err:
            _LOGGER.debug("Account refresh failed: %s", err)

    async def async_refresh_account(self) -> None:
        """Re-read the account: machines, names, node, identity."""
        try:
            account = await self.api.get_account(self.sid)
        except FahAuthError as err:
            self.auth_failed = True
            # Machines keep working through the node with the stored key;
            # ask the user to log in again for the REST features.
            self.config_entry.async_start_reauth(self.hass)
            raise FahConnectionError("Session expired") from err
        except FahApiError as err:
            raise FahConnectionError(str(err)) from err
        self.auth_failed = False
        old_node = self.account.get("node")
        if account.get("pubkey") != self.account.get("pubkey"):
            # Account key was reset (new passphrase): stored key is useless
            self.config_entry.async_start_reauth(self.hass)
            raise FahConnectionError("Account key changed")
        self.account = account
        # Persist (minus volatile bits) so we can start without the API
        cached = {k: account.get(k) for k in ("pubkey", "node", "machines", "user", "team", "passkey", "cause", "avatar", "config", "id")}
        if cached != self.config_entry.data.get(CONF_ACCOUNT):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data={**self.config_entry.data, CONF_ACCOUNT: cached}
            )
        current = set()
        for mach in account.get("machines") or []:
            if mach.get("id"):
                current.add(mach["id"])
                self._context(mach["id"], mach.get("name"))
        self._remove_stale_devices(current)
        if account.get("node") and account["node"] != old_node and not valid_node_host(account["node"]):
            _LOGGER.warning("Ignoring invalid node host from the account data")
            account["node"] = old_node
        if account.get("node") and account["node"] != old_node:
            _LOGGER.info("Account node changed to %s, reconnecting", account["node"])
            await self.node.restart(account["node"])
        self._schedule_publish()

    @callback
    def _remove_stale_devices(self, current: set[str]) -> None:
        """Machines unlinked from the account disappear from HA too."""
        dev_reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(dev_reg, self.config_entry.entry_id):
            ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
            if not ids or self.account_id in ids:
                continue
            mid = next(iter(ids))
            if mid not in current and not (
                mid in self.contexts and self.contexts[mid].machine.online
            ):
                _LOGGER.info("Machine %s no longer linked, removing device", device.name)
                dev_reg.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )
                self.async_forget_machine(mid)

    async def async_set_identity(self, **values: Any) -> None:
        """Change user/team/passkey/cause for the whole account."""
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            return
        if self.auth_failed:
            raise HomeAssistantError("Folding@home login expired, please re-authenticate")
        acc = self.account
        cause = acc.get("cause") or "any"
        data = {
            "user": acc.get("user"),
            "team": acc.get("team") or 0,
            "passkey": acc.get("passkey") or None,
            "cause": "any" if cause == "unspecified" else cause,
            "avatar": acc.get("avatar") or "",
            "node": acc.get("node") or None,
            "config": acc.get("config") or {},
        }
        data.update(values)
        data = {k: v for k, v in data.items() if v is not None}
        try:
            await self.api.put_account(self.sid, data)
        except FahAuthError as err:
            self.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError("Folding@home login expired, please re-authenticate") from err
        except (FahConnectionError, FahApiError) as err:
            raise HomeAssistantError(f"Could not save account settings: {err}") from err
        self.account.update(values)
        # Push to every online machine over its encrypted session
        for ctx in self.contexts.values():
            if ctx.machine.connected:
                try:
                    await ctx.machine.configure_global(**values)
                except FahConnectionError:
                    pass
        self.config_entry.async_create_background_task(
            self.hass, self._delayed_refresh(), "fah delayed refresh"
        )

    async def _delayed_refresh(self) -> None:
        await asyncio.sleep(5)
        try:
            await self.async_refresh_account()
        except FahConnectionError:
            pass
        await self.async_refresh_stats()

    # -------------------------------------------------------------- commands
    def _require(self, machine_id: str) -> FahMachine:
        if machine_id not in self.contexts:
            raise HomeAssistantError(f"Unknown machine {machine_id}")
        mach = self.contexts[machine_id].machine
        if not mach.connected:
            raise HomeAssistantError(f"{self.machine_name(machine_id)} is offline")
        return mach

    async def async_set_state(self, machine_id: str, state: str, group: str | None = None) -> None:
        """fold / pause / finish one machine."""
        try:
            await self._require(machine_id).set_state(state, group)
        except (FahConnectionError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_state_all(self, state: str) -> None:
        """fold / pause / finish every online machine."""
        errors = []
        for mid, ctx in self.contexts.items():
            if ctx.machine.connected:
                try:
                    await ctx.machine.set_state(state)
                except FahConnectionError as err:
                    errors.append(f"{self.machine_name(mid)}: {err}")
        if errors:
            raise HomeAssistantError("; ".join(errors))

    async def async_configure_group(self, machine_id: str, group: str, **values: Any) -> None:
        """Group options (cpus, on_idle ...)."""
        try:
            await self._require(machine_id).configure_group(group, **values)
        except (FahConnectionError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    async def async_set_gpu_enabled(self, machine_id: str, gpu_id: str, enabled: bool) -> None:
        """Turn a GPU on/off for folding."""
        try:
            await self._require(machine_id).configure_gpu(gpu_id, enabled)
        except (FahConnectionError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    # -------------------------------------------------------------- projects
    @callback
    def _ingest_wus(self, machine_id: str, wus: list[Any], incremental: bool) -> None:
        ctx = self._context(machine_id)
        recent_before = len(ctx.tracker.recent)
        new = [s for wu in wus if (s := ctx.tracker.ingest(wu))]
        ctx.tracker.prune_recent()
        if not new:
            ctx.history_imported = True
            if len(ctx.tracker.recent) != recent_before:
                self._save_later()  # recent log filled from the client's history
            return
        if incremental or ctx.history_imported:
            for summary in sorted(new, key=lambda s: s.get("end_time") or ""):
                self.hass.bus.async_fire(
                    EVENT_WU_COMPLETED,
                    {
                        "device": self.machine_name(machine_id),
                        "machine_id": machine_id,
                        "config_entry_id": self.config_entry.entry_id,
                        **summary,
                    },
                )
        else:
            _LOGGER.info(
                "Imported %d work units (%d projects) of history from %s",
                len(new),
                ctx.tracker.project_count,
                self.machine_name(machine_id),
            )
        ctx.history_imported = True
        self._save_later()
        self._schedule_project_info(machine_id)
        self._schedule_publish()

    @callback
    def _schedule_project_info(self, machine_id: str | None = None) -> None:
        if self._info_task and not self._info_task.done():
            return
        if not self._missing_project_info():
            return
        self._info_task = self.config_entry.async_create_background_task(
            self.hass, self._async_fetch_project_info(), "fah project info"
        )

    def _missing_project_info(self) -> list[str]:
        missing: set[str] = set()
        known: set[str] = set()
        for mid, ctx in self.contexts.items():
            known.update(k for k, v in ctx.tracker.project_info.items() if info_is_current(v))
            view = self.data.get(mid) if self.data else None
            missing.update(ctx.tracker.missing_info(view.projects if view else []))
        return sorted(p for p in missing if p not in known)

    async def _async_fetch_project_info(self) -> None:
        shared: dict[str, Any] = {}
        for ctx in self.contexts.values():
            shared.update(ctx.tracker.project_info)
        # Loop: projects that turn up while we fetch are picked up too
        attempted: set[str] = set()
        while todo := [p for p in self._missing_project_info() if p not in attempted][:50]:
            if len(attempted) >= 200:
                break
            offline = False
            for project in todo:
                attempted.add(project)
                if not str(project).isdigit():
                    continue  # project ids come from the machines: only digits go in a URL
                try:
                    info = await self.api.get_json(f"/project/{project}")
                except FahApiError as err:
                    if "HTTP 404" in str(err):
                        # Not published (yet): remember, ask again tomorrow
                        for ctx in self.contexts.values():
                            ctx.tracker.set_project_unpublished(project)
                    await asyncio.sleep(0.5)
                    continue
                except FahConnectionError as err:
                    _LOGGER.debug("Project %s info failed: %s", project, err)
                    offline = True
                    break
                if isinstance(info, dict) and info and not info.get("error"):
                    for ctx in self.contexts.values():
                        ctx.tracker.set_project_info(project, info)
                    shared[project] = info
                await asyncio.sleep(0.5)
            if offline:
                break
        # Share info between machines (same project on several machines)
        for ctx in self.contexts.values():
            for key, info in shared.items():
                if (
                    isinstance(info, dict)
                    and "fetched" in info
                    and not info_is_current(ctx.tracker.project_info.get(key))
                ):
                    ctx.tracker.project_info[key] = info
        self._save_later(10)
        self._schedule_publish()

    # ---------------------------------------------------------------- stats
    async def _refresh_stats_cb(self, _now: Any) -> None:
        await self.async_refresh_stats()

    async def async_refresh_stats(self) -> None:
        """Donor + team stats for the account's user/team/passkey."""
        user = self.account.get("user") or "Anonymous"
        team = self.account.get("team")
        passkey = self.account.get("passkey")
        if user == "Anonymous" and not team:
            self.stats = None
            return
        params: dict[str, str] = {}
        if team is not None:
            params["team"] = str(team)
        if passkey:
            params["passkey"] = passkey
        try:
            data = await self.api.get_json(f"/user/{quote(str(user), safe='')}", params)
        except (FahConnectionError, FahApiError, FahAuthError) as err:
            self.stats_error = str(err)
            return
        if isinstance(data, dict) and data.get("name"):
            self.stats = data
            self.stats_error = None
            self.stats["_team"] = next(
                (t for t in data.get("teams") or [] if str(t.get("team")) == str(team)), None
            )
        else:
            self.stats = None
            self.stats_error = "no stats"
        self.async_update_listeners()

    # ---------------------------------------------------------------- solar
    def machine_options(self, machine_id: str) -> dict[str, Any]:
        """Solar options of one machine merged with defaults."""
        per = (self.config_entry.options.get(CONF_MACHINES) or {}).get(machine_id) or {}
        return {**SOLAR_DEFAULTS, **per}

    @callback
    def async_update_machine_options(self, machine_id: str, values: dict[str, Any]) -> None:
        """Save options of one machine (no reload)."""
        options = dict(self.config_entry.options)
        machines = dict(options.get(CONF_MACHINES) or {})
        per = dict(machines.get(machine_id) or {})
        per.update(values)
        for key in (CONF_SOLAR_SURPLUS_ENTITY, CONF_SOLAR_SOC_ENTITY, CONF_SOLAR_IMPORT_ENTITY):
            if key in per and not per[key]:
                per.pop(key)
        machines[machine_id] = per
        options[CONF_MACHINES] = machines
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)

    @callback
    def async_setup_solar(self, machine_id: str | None = None) -> None:
        """(Re)configure solar control for one or all machines."""
        ids = [machine_id] if machine_id else list(self.contexts)
        for mid in ids:
            ctx = self.contexts.get(mid)
            if ctx is None:
                continue
            for unsub in ctx.solar_unsubs:
                unsub()
            ctx.solar_unsubs.clear()
            opts = self.machine_options(mid)
            if solar_status_disabled(opts[CONF_SOLAR_ENABLED], opts.get(CONF_SOLAR_SURPLUS_ENTITY)):
                ctx.solar_logic = None
                continue
            settings = SolarSettings.from_options(opts)
            if ctx.solar_logic is None:
                view = self.data.get(mid) if self.data else None
                running = bool(view and view.status in (STATUS_FOLDING, STATUS_WAITING))
                ctx.solar_logic = SolarLogic(settings, running)
            else:
                ctx.solar_logic.settings = settings
            entities = [
                e
                for e in (
                    opts.get(CONF_SOLAR_SURPLUS_ENTITY),
                    opts.get(CONF_SOLAR_SOC_ENTITY),
                    opts.get(CONF_SOLAR_IMPORT_ENTITY),
                )
                if e
            ]

            @callback
            def _state_event(_event: Event, mid: str = mid) -> None:
                self.hass.async_create_task(self._async_solar_evaluate(mid))

            async def _tick(_now: Any, mid: str = mid) -> None:
                await self._async_solar_evaluate(mid)

            ctx.solar_unsubs.append(
                async_track_state_change_event(self.hass, entities, _state_event)
            )
            ctx.solar_unsubs.append(async_track_time_interval(self.hass, _tick, SOLAR_TICK))
            self.hass.async_create_task(self._async_solar_evaluate(mid))
        self.async_update_listeners()

    def _read_solar(self, machine_id: str) -> SolarReadings:
        opts = self.machine_options(machine_id)
        surplus_id = opts.get(CONF_SOLAR_SURPLUS_ENTITY)
        soc_id = opts.get(CONF_SOLAR_SOC_ENTITY)
        imp_id = opts.get(CONF_SOLAR_IMPORT_ENTITY)
        return SolarReadings(
            surplus=watt_value(self.hass.states.get(surplus_id)) if surplus_id else None,
            soc=state_to_float(self.hass.states.get(soc_id)) if soc_id else None,
            grid_import=watt_value(self.hass.states.get(imp_id)) if imp_id else None,
        )

    async def _async_solar_evaluate(self, machine_id: str) -> None:
        # One decision at a time across machines: otherwise a lower-priority
        # machine could start while a higher-priority one's command is still
        # on its way, and both would switch at once.
        async with self._solar_lock:
            await self._async_solar_evaluate_locked(machine_id)

    async def _async_solar_evaluate_locked(self, machine_id: str) -> None:
        ctx = self.contexts.get(machine_id)
        if ctx is None or ctx.solar_logic is None:
            return
        logic = ctx.solar_logic
        readings = self._read_solar(machine_id)
        ctx.solar_readings = readings
        before = logic.status()
        may_start, may_stop = self._solar_priority_gates(machine_id)
        now = monotonic()
        action = logic.evaluate(readings, now, may_start=may_start, may_stop=may_stop)
        if action is not None:
            await self._async_solar_act(ctx, action, logic)
            # The load changed: other machines re-time their pending change so
            # they start/stop one after another, each seeing the new surplus.
            for other_id, other in self.contexts.items():
                if other_id != machine_id and other.solar_logic is not None:
                    other.solar_logic.restart_timer(now)
            for other_id in list(self.contexts):
                if other_id != machine_id:
                    self.hass.async_create_task(self._async_solar_evaluate(other_id))
        if action is not None or logic.status() != before:
            self.async_update_listeners()

    def _solar_priority_gates(self, machine_id: str) -> tuple[bool, bool]:
        """(may_start, may_stop) for a machine from the solar priorities.

        Priority 1 is highest. A machine may start only when every
        higher-priority, solar-controlled, online machine is already running,
        and may stop only once every lower-priority one has stopped.
        """
        me = self.contexts[machine_id].solar_logic
        if me is None:
            return True, True
        may_start = may_stop = True
        for other_id, other in self.contexts.items():
            logic = other.solar_logic
            if other_id == machine_id or logic is None or not other.machine.connected:
                continue
            if logic.settings.priority < me.settings.priority and not logic.on:
                may_start = False
            if logic.settings.priority > me.settings.priority and logic.on:
                may_stop = False
        return may_start, may_stop

    async def _async_solar_act(self, ctx: MachineContext, action: str, logic: SolarLogic) -> None:
        mach = ctx.machine
        view = build_machine(mach.state) if mach.state else None
        if action == "start":
            command = STATE_FOLD
        else:
            progress = max((u.progress for u in view.units), default=0.0) if view else 0.0
            command = logic.stop_command(progress)
        status = view.status if view else None
        name = self.machine_name(mach.id)
        _LOGGER.info("Solar control %s: %s -> %s (%s)", name, action, command, logic.reason)
        ctx.solar_last_action = f"{command}: {logic.reason}"
        ctx.solar_last_action_time = dt.datetime.now(dt.UTC)
        skip = (command == STATE_FOLD and status == STATUS_FOLDING) or (
            command == STATE_FINISH and status == STATUS_FINISHING
        )
        if not skip:
            try:
                await mach.set_state(command)
            except FahConnectionError as err:
                _LOGGER.warning("Solar control could not reach %s: %s", name, err)
                logic.on = action != "start"  # retry next tick
                return
        self.hass.bus.async_fire(
            EVENT_SOLAR_ACTION,
            {
                "device": name,
                "machine_id": mach.id,
                "config_entry_id": self.config_entry.entry_id,
                "action": action,
                "command": command,
                "reason": logic.reason,
            },
        )

    def solar_status(self, machine_id: str) -> str:
        """Status for the solar sensor."""
        opts = self.machine_options(machine_id)
        disabled = solar_status_disabled(
            opts[CONF_SOLAR_ENABLED], opts.get(CONF_SOLAR_SURPLUS_ENTITY)
        )
        ctx = self.contexts.get(machine_id)
        if disabled or ctx is None or ctx.solar_logic is None:
            return disabled or "disabled"
        return ctx.solar_logic.status()

    def solar_attributes(self, machine_id: str) -> dict[str, Any]:
        """Attributes for the solar sensor."""
        ctx = self.contexts.get(machine_id)
        if ctx is None:
            return {}
        r = ctx.solar_readings
        attrs: dict[str, Any] = {
            "surplus_w": r.surplus,
            "battery_soc": r.soc,
            "grid_import_w": r.grid_import,
            "last_action": ctx.solar_last_action,
            "last_action_time": (
                ctx.solar_last_action_time.isoformat() if ctx.solar_last_action_time else None
            ),
        }
        if ctx.solar_logic is not None:
            s = ctx.solar_logic.settings
            remaining = ctx.solar_logic.pending_remaining(monotonic())
            attrs.update(
                {
                    "decision": "fold" if ctx.solar_logic.on else "stopped",
                    "transition_in_s": round(remaining) if remaining is not None else None,
                    "start_w": s.start_w,
                    "stop_w": s.stop_w,
                    "stop_action": s.stop_action,
                    "priority": s.priority,
                }
            )
        return attrs

