"""End-to-end: HA <-> fake F@h API + node + machines (through encryption)."""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.foldingathome.const import DOMAIN

from .fah_cloud_mock import EMAIL, PASSKEY, PASSPHRASE, MockCloud


async def _wait(cond, timeout=8.0):
    for _ in range(int(timeout / 0.05)):
        try:
            if cond():
                return
        except (AttributeError, KeyError, TypeError, AssertionError):
            pass
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def _login(hass: HomeAssistant) -> config_entries.ConfigEntry:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "passphrase": PASSPHRASE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    await hass.async_block_till_done()
    return result["result"]


def _eid(hass, platform, unique_id):
    eid = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert eid, unique_id
    return eid


async def test_login_flow_errors(hass: HomeAssistant, cloud: MockCloud) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "passphrase": "wrong passphrase"}
    )
    assert result["errors"] == {"base": "invalid_auth"}
    entry = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL.upper(), "passphrase": PASSPHRASE}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == cloud.account_id
    assert entry.title == "Folding@home (tester)"
    # The passphrase is never stored
    assert PASSPHRASE not in json.dumps(dict(entry.data))
    assert "passphrase" not in entry.data
    await hass.async_block_till_done()
    # Same account again -> abort
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"email": EMAIL, "passphrase": PASSPHRASE}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_machines_appear_and_work(hass: HomeAssistant, cloud: MockCloud) -> None:
    rig1, rig2 = list(cloud.machines.values())
    entry = await _login(hass)
    # node login was signed with the account key and accepted
    assert cloud.login_ok
    # one device per machine + account device
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert {d.name for d in devices} == {"rig-1", "rig-2", "Folding@home account"}

    status1 = _eid(hass, "sensor", f"{rig1.id}_status")
    await _wait(lambda: hass.states.get(status1).state == "folding")
    assert hass.states.get(_eid(hass, "sensor", f"{rig1.id}_ppd")).state == "9250000"
    assert hass.states.get(_eid(hass, "sensor", f"{rig2.id}_ppd")).state == "250000"
    total = _eid(hass, "sensor", f"{cloud.account_id}_total_ppd")
    assert hass.states.get(total).state == "9500000"
    assert hass.states.get(_eid(hass, "sensor", f"{cloud.account_id}_machines_online")).state == "2"
    gpu = _eid(hass, "sensor", f"{rig1.id}_slot_|gpu:gpu:01:00:00_progress")
    assert float(hass.states.get(gpu).state) == 95.0
    # history arrived through the encrypted session
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_projects_worked")).state == "2")

    # Pause only rig-1 through the node
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, "switch", f"{rig1.id}_folding_switch")}, blocking=True
    )
    await _wait(lambda: rig1.state["groups"][""]["config"]["paused"] is True)
    assert rig2.state["groups"][""]["config"]["paused"] is False
    await _wait(lambda: hass.states.get(status1).state == "paused")
    # two commands in the same second are both accepted
    await hass.services.async_call(
        "button", "press", {"entity_id": _eid(hass, "button", f"{rig1.id}_button_fold")}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option",
        {"entity_id": _eid(hass, "select", f"{rig1.id}_mode"), "option": "finish"}, blocking=True,
    )
    await _wait(lambda: rig1.state["groups"][""]["config"]["finish"] is True)
    assert not rig1.rejected

    # Account buttons: all machines
    await hass.services.async_call(
        "button", "press", {"entity_id": _eid(hass, "button", f"{cloud.account_id}_button_pause_all")}, blocking=True
    )
    await _wait(lambda: rig1.state["groups"][""]["config"]["paused"] and rig2.state["groups"][""]["config"]["paused"])
    # Service on the account device = all of its machines
    account_dev = dr.async_get(hass).async_get_device({(DOMAIN, cloud.account_id)})
    await hass.services.async_call(DOMAIN, "fold", {"device_id": [account_dev.id]}, blocking=True)
    await _wait(lambda: not rig1.state["groups"][""]["config"]["paused"] and not rig2.state["groups"][""]["config"]["paused"])

    # Group config on one machine
    await hass.services.async_call(
        "number", "set_value", {"entity_id": _eid(hass, "number", f"{rig2.id}_group__cpus"), "value": 12}, blocking=True
    )
    await _wait(lambda: rig2.state["groups"][""]["config"]["cpus"] == 12)
    assert rig2.received[-1]["config"] == {"groups": {"": {"cpus": 12}}}

    # WU completes on rig-2 -> event
    events = []
    hass.bus.async_listen("foldingathome_wu_completed", callback(lambda e: events.append(e)))
    await rig2.complete_wu("rig2-new", 99999)
    await _wait(lambda: len(events) == 1)
    assert events[0].data["device"] == "rig-2"
    assert events[0].data["project"] == 99999

    # Machine goes offline / comes back with a NEW session key
    await cloud.machine_offline(rig1)
    await _wait(lambda: hass.states.get(status1).state == "offline")
    assert hass.states.get(_eid(hass, "binary_sensor", f"{rig1.id}_online")).state == "off"
    assert hass.states.get(_eid(hass, "sensor", f"{rig1.id}_ppd")).state == "unavailable"
    await cloud.machine_online(rig1)
    await _wait(lambda: hass.states.get(status1).state == "folding")

    # A brand new machine is linked to the account -> appears by itself
    rig3 = cloud.add_machine("rig-3", gpu=False)
    await cloud.machine_online(rig3)
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig3.id}_status")).state == "folding")
    await hass.services.async_call(
        "button", "press", {"entity_id": _eid(hass, "button", f"{cloud.account_id}_button_refresh")}, blocking=True
    )
    dev3 = dr.async_get(hass).async_get_device({(DOMAIN, rig3.id)})
    assert dev3.name == "rig-3"

    # Node drops -> everything offline -> reconnects and recovers
    for ws in list(cloud.account_ws):
        await ws.close()
    await _wait(lambda: hass.states.get(_eid(hass, "binary_sensor", f"{cloud.account_id}_node_connected")).state == "off")
    await _wait(lambda: hass.states.get(status1).state == "folding", 20)

    # Unlink rig-2 from the account -> device removed
    await cloud.machine_offline(rig2)
    del cloud.machines[rig2.id]
    await hass.services.async_call(DOMAIN, "refresh_account", {}, blocking=True)
    await hass.async_block_till_done()
    assert dr.async_get(hass).async_get_device({(DOMAIN, rig2.id)}) is None
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_big_snapshot_is_gzipped(hass: HomeAssistant, cloud: MockCloud) -> None:
    for m in cloud.machines.values():
        m.big_snapshot = True
    await _login(hass)
    rig1 = next(iter(cloud.machines.values()))
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_status")).state == "folding")


async def test_identity_account_level(hass: HomeAssistant, cloud: MockCloud) -> None:
    entry = await _login(hass)
    rig1, rig2 = list(cloud.machines.values())
    await _wait(lambda: rig1.sessions and rig2.sessions)
    await hass.async_block_till_done()
    # user name entity on the account device -> PUT /account + push to machines
    await hass.services.async_call(
        "text", "set_value",
        {"entity_id": _eid(hass, "text", f"{cloud.account_id}_user"), "value": "newname"}, blocking=True,
    )
    assert cloud.puts[-1]["user"] == "newname"
    assert cloud.puts[-1]["passkey"] == PASSKEY  # kept
    await _wait(lambda: rig1.state["config"]["user"] == "newname" and rig2.state["config"]["user"] == "newname")

    # Options flow: new passkey
    new_key = "fedcba9876543210fedcba9876543210"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "identity"})
    assert result["description_placeholders"]["passkey_state"] == "set"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"user": "newname", "team": 99, "passkey": "bad", "clear_passkey": False, "cause": "any"}
    )
    assert result["errors"] == {"passkey": "invalid_passkey"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"user": "newname", "team": 99, "passkey": new_key, "clear_passkey": False, "cause": "cancer"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert cloud.account["passkey"] == new_key and cloud.account["team"] == 99
    await _wait(lambda: rig1.state["config"]["passkey"] == new_key)
    # the passkey never shows up in any entity state or attribute
    for state in hass.states.async_all():
        text = str(state.state) + str(state.attributes)
        assert new_key not in text and PASSKEY not in text
    # stats
    await hass.services.async_call(DOMAIN, "refresh_account", {}, blocking=True)
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{cloud.account_id}_score")).state == "123456")


async def test_reauth_when_session_expires(hass: HomeAssistant, cloud: MockCloud) -> None:
    entry = await _login(hass)
    rig1 = next(iter(cloud.machines.values()))
    cloud.sid_valid = False
    await hass.services.async_call(DOMAIN, "refresh_account", {}, blocking=True)
    await hass.async_block_till_done()
    # machines keep working through the node with the stored key
    await hass.services.async_call(
        "button", "press", {"entity_id": _eid(hass, "button", f"{rig1.id}_button_pause")}, blocking=True
    )
    await _wait(lambda: rig1.state["groups"][""]["config"]["paused"] is True)
    node = _eid(hass, "sensor", f"{cloud.account_id}_node")
    assert hass.states.get(node).state == "login_expired"
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {"passphrase": "nope"})
    assert result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {"passphrase": PASSPHRASE})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.LOADED


async def test_starts_without_api(hass: HomeAssistant, cloud: MockCloud) -> None:
    """API down at start: cached account + stored key still reach the node."""
    entry = await _login(hass)
    rig1 = next(iter(cloud.machines.values()))
    assert await hass.config_entries.async_unload(entry.entry_id)
    cloud.sid_valid = False  # /account now fails
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_status")).state == "folding")


async def test_solar_per_machine(hass: HomeAssistant, cloud: MockCloud) -> None:
    rig1, rig2 = list(cloud.machines.values())
    rig1.state["groups"][""]["config"]["paused"] = True
    rig2.state["groups"][""]["config"]["paused"] = True
    hass.states.async_set("sensor.surplus", "0", {"unit_of_measurement": "W"})
    entry = await _login(hass)
    await _wait(lambda: rig1.sessions and rig2.sessions)
    await hass.async_block_till_done()
    # Options flow for rig-1: start at 300 W ; rig-2 via entities: start at 1000 W
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "solar_machine"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"machine": rig1.id})
    assert result["step_id"] == "solar"
    base = {"solar_stop_w": -100, "solar_start_delay": 0, "solar_stop_delay": 0, "solar_stop_action": "smart",
            "solar_finish_above": 90, "solar_min_soc": 0, "solar_stop_soc": 0, "solar_max_import_w": 0}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "solar_enabled": True, "solar_start_w": 100, "solar_stop_w": 200}
    )
    assert result["errors"] == {"solar_surplus_entity": "surplus_required"} or result["errors"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "solar_enabled": True, "solar_surplus_entity": "sensor.surplus", "solar_start_w": 300}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    coord = entry.runtime_data
    coord.async_update_machine_options(
        rig2.id, {**base, "solar_enabled": True, "solar_surplus_entity": "sensor.surplus", "solar_start_w": 1000}
    )
    await hass.async_block_till_done()
    solar1 = _eid(hass, "sensor", f"{rig1.id}_solar_status")
    assert hass.states.get(solar1).state == "waiting_for_surplus"

    hass.states.async_set("sensor.surplus", "500", {"unit_of_measurement": "W"})
    await _wait(lambda: rig1.state["groups"][""]["config"]["paused"] is False)
    await asyncio.sleep(0.3)
    assert rig2.state["groups"][""]["config"]["paused"] is True  # staggered
    hass.states.async_set("sensor.surplus", "1.2", {"unit_of_measurement": "kW"})
    await _wait(lambda: rig2.state["groups"][""]["config"]["paused"] is False)
    # shortfall: rig-1 has a 95 % GPU unit -> finish ; rig-2 at 42 % -> pause
    hass.states.async_set("sensor.surplus", "-500", {"unit_of_measurement": "W"})
    await _wait(lambda: rig1.state["groups"][""]["config"]["finish"] is True)
    await _wait(lambda: rig2.state["groups"][""]["config"]["paused"] is True)
    # disable from the switch
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": _eid(hass, "switch", f"{rig1.id}_solar_control")}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get(solar1).state == "disabled"


async def test_bad_stored_key_triggers_reauth(hass: HomeAssistant, cloud: MockCloud) -> None:
    entry = await _login(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "private_key": "AAAA"})
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_wrong_account_machine_ignored(hass: HomeAssistant, cloud: MockCloud) -> None:
    """A connect message for another account (or forged) is ignored."""
    await _login(hass)
    other = MockCloud()
    intruder = other.add_machine("intruder")
    await cloud.to_account({"type": "connect", "client": intruder.login_message()})
    msg = cloud.add_machine("forged").login_message()
    msg["payload"]["time"] = "2000-01-01T00:00:00Z"  # breaks the signature
    await cloud.to_account({"type": "connect", "client": msg})
    await asyncio.sleep(0.5)
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("sensor", DOMAIN, f"{intruder.id}_status") is None


async def test_diagnostics_have_no_secrets(hass: HomeAssistant, cloud: MockCloud) -> None:
    from custom_components.foldingathome.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    entry = await _login(hass)
    await _wait(lambda: len(entry.runtime_data.data) == 2)
    diag = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for secret in (PASSKEY, PASSPHRASE, cloud.sid, entry.data["private_key"][:40], EMAIL.lower()):
        assert secret not in diag
    assert '"machine_count": 2' in diag


def _multi_gpu(machine) -> None:
    """Make a machine look like DESKTOP-FNUNSNS: 3 GPUs, one unit, one GPU disabled."""
    st = machine.state
    st["info"]["gpus"] = {
        "gpu:01:00:00": {"description": "Quadro RTX 4000", "type": "NVIDIA", "supported": True},
        "gpu:02:00:00": {"description": "Quadro P4000", "type": "NVIDIA", "supported": True},
        "gpu:03:00:00": {"description": "Quadro P4000", "type": "NVIDIA", "supported": True},
    }
    st["groups"][""]["config"]["gpus"] = {
        "gpu:01:00:00": {"enabled": True},
        "gpu:02:00:00": {"enabled": False},
        "gpu:03:00:00": {"enabled": True},
    }
    # the GPU unit on GPU 1; GPU 3 has no unit yet, GPU 2 is disabled
    st["units"][1]["gpus"] = ["gpu:01:00:00"]


async def test_all_gpus_shown_and_toggle(hass: HomeAssistant, cloud: MockCloud) -> None:
    rig1, rig2 = list(cloud.machines.values())
    _multi_gpu(rig1)
    rig2.state["groups"][""]["config"]["cpus"] = 0  # like DESKTOP-FK6PH6R: CPU off
    await _login(hass)
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_status")).state == "folding")
    await hass.async_block_till_done()

    def st(uid):
        return hass.states.get(_eid(hass, "sensor", uid))

    # every GPU has sensors, identical cards are numbered
    assert st(f"{rig1.id}_slot_|gpu:gpu:01:00:00_status").state == "Running"
    assert st(f"{rig1.id}_slot_|gpu:gpu:02:00:00_status").state == "Disabled"
    assert st(f"{rig1.id}_slot_|gpu:gpu:03:00:00_status").state == "Idle"
    assert st(f"{rig1.id}_slot_|gpu:gpu:02:00:00_status").attributes["friendly_name"] == "rig-1 GPU 2 Quadro P4000 status"
    assert st(f"{rig1.id}_slot_|gpu:gpu:03:00:00_status").attributes["friendly_name"] == "rig-1 GPU 3 Quadro P4000 status"
    # CPU slot shows Disabled when the group has 0 CPUs, even with a unit present it is Running
    assert st(f"{rig2.id}_slot_|cpu_status").state in ("Running", "Disabled")

    # GPU switches mirror the web app's "Enabled" boxes
    sw2 = _eid(hass, "switch", f"{rig1.id}_gpu_gpu:02:00:00_enabled")
    sw1 = _eid(hass, "switch", f"{rig1.id}_gpu_gpu:01:00:00_enabled")
    assert hass.states.get(sw2).state == "off"
    assert hass.states.get(sw1).state == "on"
    assert hass.states.get(sw2).attributes["friendly_name"] == "rig-1 GPU 2 Quadro P4000"

    await hass.services.async_call("switch", "turn_on", {"entity_id": sw2}, blocking=True)
    await _wait(lambda: rig1.state["groups"][""]["config"]["gpus"]["gpu:02:00:00"]["enabled"] is True)
    # the whole map is sent, the other GPUs keep their setting
    sent = rig1.received[-1]["config"]["groups"][""]["gpus"]
    assert sent == {
        "gpu:01:00:00": {"enabled": True},
        "gpu:02:00:00": {"enabled": True},
        "gpu:03:00:00": {"enabled": True},
    }
    await _wait(lambda: hass.states.get(sw2).state == "on")
    await _wait(lambda: st(f"{rig1.id}_slot_|gpu:gpu:02:00:00_status").state == "Idle")

    await hass.services.async_call("switch", "turn_off", {"entity_id": sw1}, blocking=True)
    await _wait(lambda: rig1.state["groups"][""]["config"]["gpus"]["gpu:01:00:00"]["enabled"] is False)
    assert rig1.state["groups"][""]["config"]["gpus"]["gpu:03:00:00"]["enabled"] is True
    assert not rig1.rejected


async def test_cpu_slot_disabled_when_no_cpus(hass: HomeAssistant, cloud: MockCloud) -> None:
    _rig1, rig2 = list(cloud.machines.values())
    rig2.state["groups"][""]["config"]["cpus"] = 0
    rig2.state["units"] = []
    await _login(hass)
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig2.id}_slot_|cpu_status")).state == "Disabled")


async def test_dashboard_websocket(hass: HomeAssistant, cloud: MockCloud, hass_ws_client) -> None:
    rig1, _rig2 = list(cloud.machines.values())
    _multi_gpu(rig1)
    await _login(hass)
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_status")).state == "folding")
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_projects_worked")).state == "2")
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "foldingathome/dashboard"})
    msg = await client.receive_json()
    assert msg["success"], msg
    acc = msg["result"]["accounts"][0]
    assert acc["account"]["button_fold_all"].startswith("button.")
    names = [m["name"] for m in acc["machines"]]
    assert names == ["rig-1", "rig-2"]
    m1 = acc["machines"][0]
    assert m1["entities"]["status"] == _eid(hass, "sensor", f"{rig1.id}_status")
    assert m1["entities"]["solar_priority"].startswith("number.")
    assert [g["name"] for g in m1["gpus"]] == ["GPU 1 Quadro RTX 4000", "GPU 2 Quadro P4000", "GPU 3 Quadro P4000"]
    assert all(g["switch"] and g["sensors"]["status"] for g in m1["gpus"])
    assert m1["cpus"][0]["cpus"].startswith("number.")
    assert m1["group_switches"]["default"]["on_idle"].startswith("switch.")
    # every referenced entity exists
    ids = [*acc["account"].values()]
    for m in acc["machines"]:
        ids += [*m["entities"].values()]
        for c in m["cpus"]:
            ids += [*c["sensors"].values(), c["cpus"]]
        for g in m["gpus"]:
            ids += [*g["sensors"].values(), g["switch"]]
    assert all(hass.states.get(i) is not None for i in ids), [i for i in ids if hass.states.get(i) is None]
    projects = {p["project"]: p for p in acc["projects"]}
    # current projects first, history merged across machines
    assert acc["projects"][0]["current_on"]
    assert set(projects[18251]["machines"]) == {"rig-1", "rig-2"}
    assert projects[18251]["wus"] == 2
    assert set(projects[18251]["current_on"]) == {"rig-1", "rig-2"}


async def test_solar_priority(hass: HomeAssistant, cloud: MockCloud) -> None:
    """Priority 1 machine starts first; priority 2 only after it; 2 stops first."""
    rig1, rig2 = list(cloud.machines.values())
    for m in (rig1, rig2):
        m.state["groups"][""]["config"]["paused"] = True
    hass.states.async_set("sensor.surplus", "0", {"unit_of_measurement": "W"})
    entry = await _login(hass)
    await _wait(lambda: rig1.sessions and rig2.sessions)
    await hass.async_block_till_done()
    coord = entry.runtime_data
    base = {"solar_enabled": True, "solar_surplus_entity": "sensor.surplus", "solar_start_delay": 0,
            "solar_stop_delay": 0, "solar_stop_action": "pause", "solar_start_w": 300, "solar_stop_w": -100}
    # the "extra" machine has the same thresholds but lower priority
    coord.async_update_machine_options(rig2.id, {**base, "solar_priority": 2})
    coord.async_update_machine_options(rig1.id, {**base, "solar_priority": 1})
    await hass.async_block_till_done()

    order = []
    hass.bus.async_listen(
        "foldingathome_solar_action",
        callback(lambda e: order.append((e.data["device"], e.data["action"]))),  # in order, on the loop
    )
    hass.states.async_set("sensor.surplus", "800", {"unit_of_measurement": "W"})
    await _wait(lambda: not rig1.state["groups"][""]["config"]["paused"] and not rig2.state["groups"][""]["config"]["paused"])
    assert order[:2] == [("rig-1", "start"), ("rig-2", "start")]

    order.clear()
    hass.states.async_set("sensor.surplus", "-500", {"unit_of_measurement": "W"})
    await _wait(lambda: rig1.state["groups"][""]["config"]["paused"] and rig2.state["groups"][""]["config"]["paused"])
    assert order[:2] == [("rig-2", "stop"), ("rig-1", "stop")]


def test_priority_queue_logic():
    from custom_components.foldingathome.solar import SolarLogic, SolarReadings

    from .test_logic import _settings

    low = SolarLogic(_settings(start_delay_s=0, stop_delay_s=0), running=False)
    assert low.evaluate(SolarReadings(900), 0, may_start=False) is None
    assert low.status() == "queued"
    assert low.evaluate(SolarReadings(900), 1, may_start=True) == "start"
    assert low.evaluate(SolarReadings(-900), 2, may_stop=False) is None
    assert low.status() == "stopping"
    assert low.evaluate(SolarReadings(-900), 3, may_stop=True) == "stop"


async def test_research_topics(hass: HomeAssistant, cloud: MockCloud, hass_ws_client) -> None:
    """Work units per research topic: websocket, topic sensors, last-24h sensor."""
    rig1, _rig2 = list(cloud.machines.values())
    entry = await _login(hass)
    await _wait(lambda: hass.states.get(_eid(hass, "sensor", f"{rig1.id}_projects_worked")).state == "2")
    coord = entry.runtime_data
    # project descriptions arrive -> topics known (mock: 18251 alzheimers, 17000 influenza)
    await _wait(lambda: {"alzheimers", "influenza"} <= {t["topic"] for t in coord.topic_stats()}, timeout=20)
    await hass.async_block_till_done()
    alz = _eid(hass, "sensor", f"{coord.account_id}_topic_alzheimers")
    assert alz and hass.states.get(alz).state == "2"  # one old WU on each machine
    assert hass.states.get(alz).attributes["unit_of_measurement"] == "WUs"
    assert hass.states.get(alz).attributes["state_class"] == "total"  # may drop: not total_increasing
    assert hass.states.get(alz).name.endswith("Alzheimer's research work units")
    # the old history is outside the last 24 h; a WU finishing now is inside
    wus24 = _eid(hass, "sensor", f"{coord.account_id}_wus_24h")
    assert hass.states.get(wus24).state == "0"
    await rig1.complete_wu("rig1-now", 12345)  # mock: 12345 = cancer
    await _wait(lambda: hass.states.get(wus24).state == "1")
    assert hass.states.get(wus24).attributes["topics"] == {"Cancer research": 1}
    await _wait(lambda: _eid(hass, "sensor", f"{coord.account_id}_topic_cancer") is not None)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "foldingathome/research", "hours": 24})
    msg = await client.receive_json()
    assert msg["success"], msg
    acc = msg["result"]["accounts"][0]
    assert acc["total"] == 1 and acc["hours"] == 24
    cancer = next(t for t in acc["topics"] if t["topic"] == "cancer")
    assert cancer["wus"] == 1 and cancer["label"] == "Cancer research"
    assert cancer["running"] >= 1 and 12345 in cancer["projects"]  # 12345 is also running
    assert cancer["entity"] == _eid(hass, "sensor", f"{coord.account_id}_topic_cancer")
    await client.send_json({"id": 2, "type": "foldingathome/research", "hours": 0})
    msg = await client.receive_json()
    alltime = {t["topic"]: t["wus"] for t in msg["result"]["accounts"][0]["topics"]}
    assert alltime["alzheimers"] == 2 and alltime["influenza"] == 2 and alltime["cancer"] == 1
    await client.send_json({"id": 3, "type": "foldingathome/research", "hours": -5})
    assert not (await client.receive_json())["success"]
    # dashboard projects carry their topic
    await client.send_json({"id": 4, "type": "foldingathome/dashboard"})
    msg = await client.receive_json()
    topics = {p["project"]: p["topic_label"] for p in msg["result"]["accounts"][0]["projects"]}
    assert topics[18251] == "Alzheimer's research"


async def test_solar_numbers_refuse_flapping_thresholds(hass: HomeAssistant, cloud: MockCloud) -> None:
    """Stop >= start (or battery stop above start) is refused from entities/the card."""
    from homeassistant.exceptions import ServiceValidationError

    rig1, _rig2 = list(cloud.machines.values())
    entry = await _login(hass)
    await _wait(lambda: rig1.sessions)
    await hass.async_block_till_done()
    stop = _eid(hass, "number", f"{rig1.id}_solar_stop_w")
    start = _eid(hass, "number", f"{rig1.id}_solar_start_w")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("number", "set_value", {"entity_id": stop, "value": 300}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("number", "set_value", {"entity_id": start, "value": -200}, blocking=True)
    await hass.services.async_call("number", "set_value", {"entity_id": stop, "value": 200}, blocking=True)
    assert entry.runtime_data.machine_options(rig1.id)["solar_stop_w"] == 200
    # battery levels only matter with a battery sensor
    entry.runtime_data.async_update_machine_options(rig1.id, {"solar_soc_entity": "sensor.soc", "solar_min_soc": 90})
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": _eid(hass, "number", f"{rig1.id}_solar_stop_soc"), "value": 95},
            blocking=True,
        )


async def test_unpublished_project_retried_daily(hass: HomeAssistant, cloud: MockCloud) -> None:
    """A project the public API doesn't know is remembered, not re-fetched on every WU."""
    rig1, _rig2 = list(cloud.machines.values())
    entry = await _login(hass)
    coord = entry.runtime_data
    await _wait(lambda: "18251" in coord.project_info_all(), timeout=20)
    cloud.missing_projects = {"55555"}
    await rig1.complete_wu("rig1-x", 55555)
    await _wait(lambda: (coord.project_info_all().get("55555") or {}).get("unpublished"), timeout=20)
    asked = cloud.project_requests.count("55555")
    assert asked == 1
    await rig1.complete_wu("rig1-y", 55555)
    await asyncio.sleep(1.5)
    assert cloud.project_requests.count("55555") == asked  # not asked again today
    assert coord._missing_project_info() == []
