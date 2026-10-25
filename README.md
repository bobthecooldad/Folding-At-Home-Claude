<p align="center"><img src="images/icon.png" alt="Icon" width="128"></p>

# Folding@home for Home Assistant

A custom integration that controls your **Folding@home v8 machines through your Folding@home account**, the same way <https://app.foldingathome.org> does. Home Assistant never talks to a machine's IP address, so changing IPs, other networks and firewalls don't matter. Every machine linked to your account shows up as its own device, and new machines appear by themselves.

* **Control:** Fold / Pause / Finish per machine or for all machines, turn individual GPUs on/off, CPUs per resource group, and the idle/battery/keep-awake/beta switches.
* **Account settings:** user name, team, cause and passkey for the whole account (the same as the web app's Account settings).
* **Sensors:** status, PPD, running WUs and CPUs in use per machine. Each CPU and GPU slot gets progress, PPD, ETA, project and status. The account gets total PPD, machines online, donor score, WUs, rank and team score.
* **Project tracking:** every finished WU is recorded per machine and project (count, credited, failed, base credit), with the project's cause, institution and manager. This survives the client's own 500-WU history limit. A `foldingathome_wu_completed` event fires for each WU.
* **Solar control per machine:** folds on spare power and finishes or pauses when it runs out. It uses hysteresis and delays, has optional battery and grid-import guards, and staggered thresholds so machines start one after another.

## How it connects

```
Home Assistant ──HTTPS──▶ api.foldingathome.org   (login, account, stats)
      │
      └──WSS──▶ your F@h node ◀──WSS── each linked machine
               (end-to-end encrypted per machine)
```

This is the web app's own protocol, re-implemented from the open-source web client and client code:

1. You log in once with the account **email + passphrase**. A key derived from the passphrase (PBKDF2-SHA256, 100 000 rounds) unlocks the account's RSA key, which is stored encrypted on the F@h server. The passphrase itself is **not stored**.
2. HA connects to your account's node and signs in with the account key.
3. Each machine sends its own AES-256 session key, encrypted to your account key. All traffic with that machine is encrypted with it, and the node can't read it.

The config entry keeps the unlocked account key and the API session, just like the browser keeps them in local storage. Treat your HA backups accordingly.

## Requirements

* Folding@home client **v8.3+** on each machine (tested against the 8.5 protocol).
* Each machine **linked to your F@h account**, i.e. it shows up when you log in at app.foldingathome.org. To link a new machine, open its local web control (or app.foldingathome.org on that machine) while logged in. It links automatically. You can also start the client with your account token (*Account settings → Token*).
* An account **passphrase**. If you only ever signed in with Google or GitHub, set a passphrase in the web app first.
* Home Assistant **2025.2** or newer, with internet access (2026.3+ to show the integration's icon).

Nothing has to be changed on the machines: no `config.xml` edits, open ports or fixed IPs.

## Install

**HACS (recommended):**

1. In Home Assistant open **HACS**, then the **⋮** menu (top right), then **Custom repositories**.
2. Repository: `https://github.com/bobthecooldad/Folding-At-Home-Claude`, Type: **Integration**, then **Add**.
3. Search HACS for **Folding@home**, open it, click **Download**, then **Download** again.
4. **Settings → System → Restart Home Assistant.**

**Manual:** copy `custom_components/foldingathome` into `/config/custom_components/` (e.g. with the Samba share or Studio Code Server add-on) and restart HA.

Then go to *Settings → Devices & services → Add integration → Folding@home*, enter the account email and passphrase, and you're done. You get one *Folding@home account* device plus one device per machine.

If the session ever expires or you change the passphrase, HA shows a **Re-authenticate** prompt. Machines keep working in the meantime, because control goes through the node using the stored key.

Optional check from any PC: `tools/fah_account_check.py --email you@example.com` logs in, lists your machines with their status, and can send one test command (`--command pause --machine rig-1`). It's read-only unless you pass `--command`, and it never prints secrets.

## User, team and passkey

These are account settings, so they apply to every linked machine, and new machines get them automatically from the account.

* **User name**, **Team** and **Cause** are entities on the account device. They can also be changed with the `foldingathome.set_identity` action.
* The **passkey** is set only under *Configure → User, team and passkey*. It is deliberately not an entity or an action field, because entity states and action calls are written to HA's history database. It must be 32 hex characters (get one at <https://apps.foldingathome.org/getpasskey>).

Changes are saved to the account (`PUT /account`, like the web app), then pushed straight to each online machine over its encrypted session.

## Solar control (no-export systems)

*Configure → Solar control* → pick a machine:

| Setting | Meaning |
|---|---|
| Surplus power sensor | Any sensor in W or kW where positive = spare power |
| Start when surplus ≥ | e.g. 300 W |
| Stop when surplus < | e.g. −100 W (must be below start) |
| Start / stop delay | how long the condition must hold (minutes) |
| When solar runs out | **Pause** now, **Finish** the current WU, or **Smart**: finish if a WU is ≥ X % done, else pause |
| Battery SoC sensor (optional) | only start above *min*, stop below *stop* |
| Grid import sensor (optional) | stop if importing more than *max* W |

Each machine's thresholds, stop action and *Solar control* switch are also entities, so you can tune them from a dashboard. The **Solar status** sensor shows `waiting_for_surplus` / `starting` / `running_on_solar` / `stopping`, with the live readings and the countdown to the next change.

Solar control only sends a command when its own decision changes. If you press Fold or Pause yourself, that holds until the next solar transition.

**What is "surplus" with zero export?** The inverter throttles the panels, so spare power isn't visible directly. With a battery, use what's going into the battery:

```yaml
template:
  - sensor:
      - name: "Solar surplus"
        unique_id: solar_surplus
        unit_of_measurement: W
        device_class: power
        state_class: measurement
        state: >
          {{ states('sensor.battery_charge_power') | float(0)
             - states('sensor.battery_discharge_power') | float(0) }}
```

Also set the battery guard: start above ~95 %, stop below ~85 %. Folding then soaks up the power that would otherwise be clipped once the battery is full.

Without a battery, use PV power minus house load, or a forecast sensor minus load.

**Several machines:** stagger the start thresholds (e.g. 300 W, 700 W, 1100 W). Each machine's own draw lowers the surplus once it starts, so keep the gap between start and stop larger than that draw.

## Entities

**Account device**

| Type | Entities |
|---|---|
| Sensor | Node (connected / disconnected / login expired), Machines online (list in attributes), Total points per day, Donor name, Donor score / work units / rank, Team score |
| Binary sensor | Node connected |
| Button | Fold all machines, Pause all machines, Finish all machines, Refresh |
| Text / Number / Select | User name, Team, Cause |

**Each machine**

| Type | Entities |
|---|---|
| Sensor | Status (incl. *offline*), Points per day, Running work units, CPUs in use, Current project, Projects worked, Work units completed, Base credit earned, Last work unit finished, Solar status, Client version |
| Sensor (CPU + **every detected GPU**) | progress, PPD, ETA, project, status (*Running*, *Idle*, *Disabled*, …), shown even when no work unit is assigned. Identical cards are numbered (GPU 1, GPU 2 …). The progress sensor carries the full WU details as attributes. |
| Binary sensor | Online, Computing |
| Button | Fold, Pause, Finish |
| Select | Mode (fold/pause/finish), Solar stop action |
| Switch | Folding (on = fold, off = pause), Solar control, **one switch per GPU** (fold on that GPU, like the web app's *Enabled* box), and per group: Only when idle, While on battery, Keep awake, Beta projects |
| Number | CPUs (per group), solar thresholds/delays |

A machine you unlink from the account is removed from HA at the next account refresh (every 15 min, or press *Refresh*).

## Actions

| Action | Fields |
|---|---|
| `foldingathome.fold` / `pause` / `finish` | `device_id`: machines, or the account device for all of its machines. Empty = all online machines. Optional `group`. |
| `foldingathome.set_identity` | `user`, `team`, `cause` (account-wide) |
| `foldingathome.set_group_config` | `device_id`, `group`, `cpus`, `on_idle`, `on_battery`, `keep_awake`, `beta`, `cuda`, `hip` |
| `foldingathome.refresh_account` | re-read machines, names and settings, plus stats |

## Events

* `foldingathome_wu_completed`: `device`, `machine_id`, `project`, `run`, `clone`, `gen`, `result`, `base_credit`, `run_time`, `end_time`, `core`
* `foldingathome_solar_action`: `device`, `machine_id`, `action` (start/stop), `command`, `reason`

## Examples

Phone notification for each finished work unit:

```yaml
automation:
  - alias: "F@h WU finished"
    triggers:
      - trigger: event
        event_type: foldingathome_wu_completed
    actions:
      - action: notify.mobile_app_phone
        data:
          message: >
            {{ trigger.event.data.device }} finished project {{ trigger.event.data.project }}
            ({{ trigger.event.data.result }})
```

Finish everything before the peak tariff:

```yaml
automation:
  - alias: "F@h finish at peak"
    triggers:
      - trigger: time
        at: "16:00:00"
    actions:
      - action: foldingathome.finish
```

See `examples/dashboard.yaml` for a ready-made dashboard, and [SECURITY.md](SECURITY.md) for the security review.

## Troubleshooting

* **invalid_auth**: use the account *passphrase* (not the donor passkey). The email is not case-sensitive.
* **no_secret**: log in once at app.foldingathome.org with the passphrase, then try again.
* **Machine stays offline**: check that it appears in the web app under your account and that its client is running and online.
* **Commands ignored**: the client drops a command whose timestamp isn't newer than the last one it accepted. The integration handles this, but a machine or browser with a clock far in the future can block changes. Keep clocks in sync (NTP).
* Debug logging: `logger: logs: custom_components.foldingathome: debug` (secrets are redacted).
