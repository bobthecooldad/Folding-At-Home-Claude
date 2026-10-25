"""Sample Folding@home client state (v8.5 format) for tests."""

from __future__ import annotations

import datetime as dt
from typing import Any


def make_state() -> dict[str, Any]:
    now = dt.datetime.now(dt.UTC)
    assign = (now - dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "info": {
            "version": "8.5.6",
            "os": "linux",
            "cpu_brand": "AMD Ryzen 9 7950X",
            "cpus": 32,
            "gpus": {"gpu:01:00:00": {"description": "GeForce RTX 4090", "type": "NVIDIA"}},
            "mach_name": "rig-1",
            "hostname": "rig-1",
            "id": "MACHINE-ID-1",
            "account": "",
        },
        "config": {"user": "Anonymous", "team": 0, "passkey": "", "cause": "any"},
        "groups": {
            "": {
                "config": {
                    "on_idle": False,
                    "on_battery": True,
                    "keep_awake": True,
                    "paused": False,
                    "finish": False,
                    "beta": False,
                    "cuda": True,
                    "hip": True,
                    "key": 0,
                    "cpus": 30,
                    "gpus": {"gpu:01:00:00": {"enabled": True}},
                }
            }
        },
        "units": [
            {
                "id": "unit-cpu",
                "number": 1,
                "group": "",
                "state": "RUN",
                "cpus": 30,
                "gpus": [],
                "assignment": {
                    "project": 18251,
                    "time": assign,
                    "deadline": 86400 * 2,
                    "timeout": 86400,
                    "credit": 5000,
                    "core": {"type": "0xa8"},
                },
                "wu": {"run": 1, "clone": 2, "gen": 3},
                "wu_progress": 0.42,
                "ppd": 250000,
                "eta": "1h 20m",
            },
            {
                "id": "unit-gpu",
                "number": 2,
                "group": "",
                "state": "RUN",
                "cpus": 1,
                "gpus": ["gpu:01:00:00"],
                "assignment": {
                    "project": 12345,
                    "time": assign,
                    "deadline": 86400,
                    "timeout": 43200,
                    "credit": 90000,
                    "core": {"type": "0x23"},
                },
                "wu": {"run": 7, "clone": 8, "gen": 9},
                "wu_progress": 0.95,
                "ppd": 9000000,
                "eta": "5m 2s",
            },
        ],
    }


def wu_record(uid: str, project: int, result: str = "credited", end: str = "2026-10-20T10:00:00Z"):
    return {
        "id": uid,
        "state": "DONE",
        "result": result,
        "end_time": end,
        "run_time": 3600,
        "group": "",
        "assignment": {"project": project, "credit": 1000, "core": {"type": "0xa8"}, "time": end},
        "wu": {"run": 1, "clone": 1, "gen": 1},
    }
