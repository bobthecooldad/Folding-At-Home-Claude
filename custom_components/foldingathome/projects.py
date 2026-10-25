"""Track the projects (and work units) a machine has worked on."""

from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import html
import re
from typing import Any

MAX_SEEN_IDS = 3000
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None, limit: int = 400) -> str | None:
    """Turn the project description HTML into short plain text."""
    if not text:
        return None
    plain = html.unescape(_TAG_RE.sub(" ", text))
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) > limit:
        plain = plain[: limit - 1].rsplit(" ", 1)[0] + "…"
    return plain


def wu_is_finished(wu: dict[str, Any]) -> bool:
    """True if the WU-log record is a finished unit."""
    return bool(wu.get("result")) or wu.get("state") == "DONE"


class ProjectTracker:
    """Persistent per-machine project statistics.

    ``data`` is JSON-serialisable and is what gets saved to HA storage.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        self.projects: dict[str, dict[str, Any]] = data.get("projects", {})
        self.seen: OrderedDict[str, str] = OrderedDict(data.get("seen", []))
        self.project_info: dict[str, dict[str, Any]] = data.get("project_info", {})
        self.last_wu: dict[str, Any] | None = data.get("last_wu")

    def as_dict(self) -> dict[str, Any]:
        """Serialise."""
        return {
            "projects": self.projects,
            "seen": list(self.seen.items()),
            "project_info": self.project_info,
            "last_wu": self.last_wu,
        }

    # ------------------------------------------------------------- ingest
    def ingest(self, wu: dict[str, Any]) -> dict[str, Any] | None:
        """Add a finished WU. Returns a summary if it was new, else None."""
        if not isinstance(wu, dict) or not wu_is_finished(wu):
            return None
        uid = str(wu.get("id") or "")
        assignment = wu.get("assignment") or {}
        project = assignment.get("project")
        if not uid or project is None:
            return None
        if uid in self.seen:
            return None

        end_time = wu.get("end_time") or assignment.get("time") or ""
        self.seen[uid] = end_time
        while len(self.seen) > MAX_SEEN_IDS:
            self.seen.popitem(last=False)

        result = str(wu.get("result") or "done").lower()
        key = str(project)
        proj = self.projects.setdefault(
            key,
            {
                "project": int(project),
                "wus": 0,
                "credited": 0,
                "failed": 0,
                "base_credit": 0,
                "run_time": 0,
                "first_seen": end_time,
                "last_seen": end_time,
            },
        )
        proj["wus"] += 1
        if result == "credited":
            proj["credited"] += 1
            proj["base_credit"] += int(assignment.get("credit") or 0)
        elif result not in ("done",):
            proj["failed"] += 1
        proj["run_time"] += int(wu.get("run_time") or 0)
        if end_time and (not proj["first_seen"] or end_time < proj["first_seen"]):
            proj["first_seen"] = end_time
        if end_time and end_time >= (proj["last_seen"] or ""):
            proj["last_seen"] = end_time
            w = wu.get("wu") or {}
            proj["last_prcg"] = f"{project} ({w.get('run')}, {w.get('clone')}, {w.get('gen')})"

        summary = {
            "unit_id": uid,
            "project": int(project),
            "run": (wu.get("wu") or {}).get("run"),
            "clone": (wu.get("wu") or {}).get("clone"),
            "gen": (wu.get("wu") or {}).get("gen"),
            "result": result,
            "base_credit": assignment.get("credit"),
            "run_time": wu.get("run_time"),
            "end_time": end_time,
            "core": (assignment.get("core") or {}).get("type"),
        }
        if not self.last_wu or end_time >= (self.last_wu.get("end_time") or ""):
            self.last_wu = summary
        return summary

    def set_project_info(self, project: int | str, info: dict[str, Any]) -> None:
        """Store description data from the public API."""
        self.project_info[str(project)] = {
            "cause": info.get("cause"),
            "manager": info.get("manager"),
            "institution": info.get("institution"),
            "description": strip_html(info.get("description")),
            "url": info.get("url") or None,
            "fetched": dt.datetime.now(dt.UTC).isoformat(),
        }

    def missing_info(self, extra: list[int] | None = None) -> list[str]:
        """Projects we have no description for yet."""
        ids = set(self.projects) | {str(p) for p in extra or []}
        return sorted(p for p in ids if p not in self.project_info)

    # ------------------------------------------------------------- totals
    @property
    def project_count(self) -> int:
        """Number of distinct projects."""
        return len(self.projects)

    @property
    def wu_count(self) -> int:
        """All finished WUs."""
        return sum(p["wus"] for p in self.projects.values())

    @property
    def credited_count(self) -> int:
        """Credited WUs."""
        return sum(p["credited"] for p in self.projects.values())

    @property
    def base_credit_total(self) -> int:
        """Sum of base credit of credited WUs (excludes quick-return bonus)."""
        return sum(p["base_credit"] for p in self.projects.values())

    def by_cause(self) -> dict[str, int]:
        """WU count per cause."""
        out: dict[str, int] = {}
        for key, proj in self.projects.items():
            cause = (self.project_info.get(key) or {}).get("cause") or "unknown"
            out[cause] = out.get(cause, 0) + proj["wus"]
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    def summary_list(self, limit: int = 25) -> list[dict[str, Any]]:
        """Most recent projects with info, for sensor attributes."""
        items = sorted(
            self.projects.values(), key=lambda p: p.get("last_seen") or "", reverse=True
        )
        out = []
        for proj in items[:limit]:
            info = self.project_info.get(str(proj["project"])) or {}
            out.append(
                {
                    "project": proj["project"],
                    "wus": proj["wus"],
                    "credited": proj["credited"],
                    "base_credit": proj["base_credit"],
                    "last_seen": proj.get("last_seen"),
                    "cause": info.get("cause"),
                    "institution": info.get("institution"),
                    "manager": info.get("manager"),
                }
            )
        return out
