"""Constants for the Folding@home integration."""

from __future__ import annotations

import datetime as dt
from typing import Final

DOMAIN: Final = "foldingathome"
MANUFACTURER: Final = "Folding@home"


# ---------------------------------------------------------------------------
# Client states / commands
# ---------------------------------------------------------------------------
STATE_FOLD: Final = "fold"
STATE_PAUSE: Final = "pause"
STATE_FINISH: Final = "finish"
CLIENT_STATES: Final = [STATE_FOLD, STATE_PAUSE, STATE_FINISH]

# Machine status values exposed by the "status" sensor
STATUS_FOLDING: Final = "folding"
STATUS_FINISHING: Final = "finishing"
STATUS_PAUSED: Final = "paused"
STATUS_WAITING: Final = "waiting"
STATUS_IDLE: Final = "idle"
STATUS_OFFLINE: Final = "offline"
MACHINE_STATUSES: Final = [
    STATUS_FOLDING,
    STATUS_FINISHING,
    STATUS_PAUSED,
    STATUS_WAITING,
    STATUS_IDLE,
    STATUS_OFFLINE,
]

KNOWN_CAUSES: Final = [
    "any",
    "alzheimers",
    "cancer",
    "huntingtons",
    "parkinsons",
    "influenza",
    "diabetes",
    "covid-19",
]

# Per-group boolean settings that can be toggled from HA
GROUP_BOOL_KEYS: Final = ["on_idle", "on_battery", "keep_awake", "beta", "cuda", "hip"]

# Human readable unit states (from the web client's unit.js)
UNIT_STATUS: Final = {
    "ASSIGN": "Assigning",
    "DOWNLOAD": "Downloading",
    "CORE": "Core",
    "RUN": "Running",
    "FINISH": "Finishing",
    "UPLOAD": "Uploading",
    "CLEAN": "Ended",
    "WAIT": "Waiting",
    "PAUSE": "Paused",
    "DUMP": "Dumping",
    "DUMPED": "Dumped",
    "EXPIRED": "Expired",
    "ABORTED": "Aborted",
    "MISSING": "Missing Data",
    "RETRIES": "Max Retries",
    "FAILED": "Failed",
    "REJECTED": "Rejected",
    "CREDITED": "Credited",
    "DONE": "Done",
}

# Public F@h stats refresh
STATS_INTERVAL_SECONDS: Final = 60 * 60  # the web client caches stats for 1h

# ---------------------------------------------------------------------------
# Config entry data / options
# ---------------------------------------------------------------------------
CONF_EMAIL: Final = "email"
CONF_PASSPHRASE: Final = "passphrase"
CONF_SID: Final = "sid"
CONF_PRIVATE_KEY: Final = "private_key"
CONF_ACCOUNT: Final = "account"
CONF_MACHINES: Final = "machines"  # options: per machine solar settings

CONF_USER: Final = "user"
CONF_TEAM: Final = "team"
CONF_PASSKEY: Final = "passkey"
CONF_CAUSE: Final = "cause"

ACCOUNT_REFRESH: Final = dt.timedelta(minutes=15)

# Solar options
CONF_SOLAR_ENABLED: Final = "solar_enabled"
CONF_SOLAR_SURPLUS_ENTITY: Final = "solar_surplus_entity"
CONF_SOLAR_START_W: Final = "solar_start_w"
CONF_SOLAR_STOP_W: Final = "solar_stop_w"
CONF_SOLAR_START_DELAY: Final = "solar_start_delay"
CONF_SOLAR_STOP_DELAY: Final = "solar_stop_delay"
CONF_SOLAR_SOC_ENTITY: Final = "solar_soc_entity"
CONF_SOLAR_MIN_SOC: Final = "solar_min_soc"
CONF_SOLAR_STOP_SOC: Final = "solar_stop_soc"
CONF_SOLAR_IMPORT_ENTITY: Final = "solar_import_entity"
CONF_SOLAR_MAX_IMPORT_W: Final = "solar_max_import_w"
CONF_SOLAR_STOP_ACTION: Final = "solar_stop_action"
CONF_SOLAR_FINISH_ABOVE: Final = "solar_finish_above"

STOP_ACTION_PAUSE: Final = "pause"
STOP_ACTION_FINISH: Final = "finish"
STOP_ACTION_SMART: Final = "smart"
STOP_ACTIONS: Final = [STOP_ACTION_PAUSE, STOP_ACTION_FINISH, STOP_ACTION_SMART]

SOLAR_DEFAULTS: Final = {
    CONF_SOLAR_ENABLED: False,
    CONF_SOLAR_START_W: 300.0,
    CONF_SOLAR_STOP_W: -100.0,
    CONF_SOLAR_START_DELAY: 5,  # minutes
    CONF_SOLAR_STOP_DELAY: 5,  # minutes
    CONF_SOLAR_MIN_SOC: 0.0,
    CONF_SOLAR_STOP_SOC: 0.0,
    CONF_SOLAR_MAX_IMPORT_W: 0.0,  # 0 = guard disabled
    CONF_SOLAR_STOP_ACTION: STOP_ACTION_SMART,
    CONF_SOLAR_FINISH_ABOVE: 90.0,  # percent
}

# Solar controller status values
SOLAR_DISABLED: Final = "disabled"
SOLAR_NO_SENSOR: Final = "no_sensor"
SOLAR_WAITING: Final = "waiting_for_surplus"
SOLAR_STARTING: Final = "starting"
SOLAR_RUNNING: Final = "running_on_solar"
SOLAR_STOPPING: Final = "stopping"
SOLAR_STATUSES: Final = [
    SOLAR_DISABLED,
    SOLAR_NO_SENSOR,
    SOLAR_WAITING,
    SOLAR_STARTING,
    SOLAR_RUNNING,
    SOLAR_STOPPING,
]

# ---------------------------------------------------------------------------
# Events / services
# ---------------------------------------------------------------------------
EVENT_WU_COMPLETED: Final = "foldingathome_wu_completed"
EVENT_SOLAR_ACTION: Final = "foldingathome_solar_action"

SERVICE_FOLD: Final = "fold"
SERVICE_PAUSE: Final = "pause"
SERVICE_FINISH: Final = "finish"
SERVICE_SET_IDENTITY: Final = "set_identity"
SERVICE_SET_GROUP_CONFIG: Final = "set_group_config"
SERVICE_REFRESH_STATS: Final = "refresh_stats"
SERVICE_REFRESH_ACCOUNT: Final = "refresh_account"

ATTR_GROUP: Final = "group"
ATTR_CPUS: Final = "cpus"

STORAGE_VERSION: Final = 1
