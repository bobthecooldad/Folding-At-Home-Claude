"""Track the projects (and work units) a machine has worked on."""

from __future__ import annotations

from collections import OrderedDict
import datetime as dt
import html
import re
from typing import Any

MAX_SEEN_IDS = 3000
MAX_RECENT = 5000
RECENT_DAYS = 31
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


def parse_time(value: Any) -> dt.datetime | None:
    """Parse the client's ISO time (UTC, with Z) into an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.UTC)


def cause_key(cause: Any) -> str:
    """Normalise a project's cause: known names, "unspecified", or "unknown".

    "unknown" means the project's description hasn't been fetched yet.
    """
    if not cause or not isinstance(cause, str):
        return "unknown"
    key = cause.strip().lower()
    if key in ("", "any", "unspecified", "none"):
        return "unspecified"
    return key


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
        # uid -> [end_time, project] for WUs of the last RECENT_DAYS
        self.recent: OrderedDict[str, list[Any]] = OrderedDict(data.get("recent", []))

    def as_dict(self) -> dict[str, Any]:
        """Serialise."""
        return {
            "projects": self.projects,
            "seen": list(self.seen.items()),
            "project_info": self.project_info,
            "last_wu": self.last_wu,
            "recent": list(self.recent.items()),
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
        end_time = wu.get("end_time") or assignment.get("time") or ""
        if uid in self.seen:
            # Counted before; still fill the recent log (upgrades from 1.2)
            if uid not in self.recent:
                self._add_recent(uid, end_time, int(project))
            return None

        self.seen[uid] = end_time
        while len(self.seen) > MAX_SEEN_IDS:
            self.seen.popitem(last=False)
        self._add_recent(uid, end_time, int(project))

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

    def _add_recent(self, uid: str, end_time: str, project: int) -> None:
        """Remember when a WU finished, for "last N hours" statistics."""
        when = parse_time(end_time)
        if when is None:
            return
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=RECENT_DAYS)
        if when < cutoff:
            return
        self.recent[uid] = [when.isoformat(), project]
        self.prune_recent()

    def prune_recent(self) -> None:
        """Drop entries older than RECENT_DAYS, and cap the size."""
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=RECENT_DAYS)
        for uid in [u for u, (t, _p) in self.recent.items() if (parse_time(t) or cutoff) <= cutoff]:
            del self.recent[uid]
        if len(self.recent) > MAX_RECENT:
            for uid in sorted(self.recent, key=lambda u: self.recent[u][0])[: len(self.recent) - MAX_RECENT]:
                del self.recent[uid]

    def recent_since(self, since: dt.datetime) -> list[tuple[dt.datetime, int]]:
        """(finish time, project) of WUs finished after ``since``."""
        out = []
        for t, project in self.recent.values():
            when = parse_time(t)
            if when is not None and when >= since:
                out.append((when, project))
        return out

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
            cause = cause_key((self.project_info.get(key) or {}).get("cause"))
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
                    "description": info.get("description"),
                }
            )
        return out


# ----------------------------------------------------------------- topics
# What a project researches, worked out from its description. Most projects
# have the cause "unspecified", so the description is the better guide.
# Order matters: the first match wins. Rules before the cause fallback are
# subjects that a description names directly; rules after it are broader.
_T = re.compile
TOPIC_RULES_FIRST: list[tuple[str, str, re.Pattern[str]]] = [
    ("misfolding", "Protein misfolding", _T(r"misfold", re.I)),
    ("methods", "Simulation methods / force fields", _T(r"force field|benchmark", re.I)),
    ("pfas", "PFAS / toxic chemicals", _T(r"\bpfas\b|fluoroalkyl|pollutant", re.I)),
    ("alzheimers", "Alzheimer's research", _T(r"alzheimer|amyloid|neurofibrillary|\btau\b", re.I)),
    ("parkinsons", "Parkinson's research", _T(r"parkinson|synuclein", re.I)),
    ("huntingtons", "Huntington's research", _T(r"huntington", re.I)),
    ("cancer", "Cancer research", _T(r"cancer|oncogen|tumou?r|leuka?emia|carcinoma|\bkras\b", re.I)),
    ("autoimmune", "Autoimmune disease", _T(r"autoimmun|lupus|rheumatoid|multiple sclerosis", re.I)),
    ("covid-19", "COVID-19 research", _T(r"sars-cov|covid|coronavirus", re.I)),
    ("influenza", "Influenza research", _T(r"influenza|hemagglutinin|haemagglutinin", re.I)),
    ("ebola", "Ebola research", _T(r"ebola", re.I)),
    ("viruses", "Virus research", _T(r"\bvirus|viral\b|nipah|zika|dengue|\bhiv\b", re.I)),
    ("bacteria", "Bacteria & microbiome", _T(r"antibiotic|antimicrobial|bacteri|microbiome|tuberculosis", re.I)),
    ("diabetes", "Diabetes research", _T(r"diabet|insulin", re.I)),
]
# The F@h "cause" of a project, used when the description names no subject
CAUSE_TOPICS: dict[str, str] = {
    "alzheimers": "Alzheimer's research",
    "cancer": "Cancer research",
    "huntingtons": "Huntington's research",
    "parkinsons": "Parkinson's research",
    "influenza": "Influenza research",
    "diabetes": "Diabetes research",
    "covid-19": "COVID-19 research",
}
TOPIC_RULES_LATER: list[tuple[str, str, re.Pattern[str]]] = [
    ("brain", "Brain & nervous system", _T(r"neuro|schizophren|depression|anxiety|\bbrain\b", re.I)),
    ("plants", "Plants & crops", _T(r"\bplants?\b|arabidopsis|\bcrops?\b|photosynth|rubisco|pollen", re.I)),
    ("immune", "Antibodies & immune system", _T(r"antibod|antigen|immune|interleukin", re.I)),
    ("disordered", "Disordered proteins", _T(r"disordered", re.I)),
    ("genes", "Gene regulation", _T(r"transcription|gene regulation|\bdna\b", re.I)),
    ("drugs", "Drug design", _T(r"\bdrugs?\b|ligand|medicinal|inhibitor", re.I)),
    ("enzymes", "Enzyme dynamics", _T(r"enzym", re.I)),
    ("folding", "Protein folding & design", _T(r"folding|hairpin|mini-?protein|peptide", re.I)),
]
TOPIC_OTHER = ("other", "Other / not yet identified")
TOPIC_LABELS: dict[str, str] = {
    **{k: label for k, label, _ in TOPIC_RULES_FIRST + TOPIC_RULES_LATER},
    **CAUSE_TOPICS,
    TOPIC_OTHER[0]: TOPIC_OTHER[1],
}


def project_topic(info: dict[str, Any] | None) -> tuple[str, str]:
    """(key, label) of what a project researches."""
    info = info or {}
    text = " ".join(str(info.get(k) or "") for k in ("description",))
    for key, label, rule in TOPIC_RULES_FIRST:
        if rule.search(text):
            return key, label
    cause = cause_key(info.get("cause"))
    if cause in CAUSE_TOPICS:
        return cause, CAUSE_TOPICS[cause]
    for key, label, rule in TOPIC_RULES_LATER:
        if rule.search(text):
            return key, label
    return TOPIC_OTHER
