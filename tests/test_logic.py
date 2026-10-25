"""Pure-python tests: patches, model, projects, solar, validation, discovery."""


from custom_components.foldingathome.account import apply_update, redact
from custom_components.foldingathome.model import build_machine, parse_eta
from custom_components.foldingathome.projects import ProjectTracker
from custom_components.foldingathome.solar import (
    SolarLogic,
    SolarReadings,
    SolarSettings,
)
from custom_components.foldingathome.validation import is_valid_passkey, is_valid_user

from .fah_mock import make_state, wu_record


def test_apply_update():
    s = {"units": [{"id": 1}], "config": {"user": "a"}}
    apply_update(s, ["config", "user", "b"])
    apply_update(s, ["units", 0, "wu_progress", 0.5])
    apply_update(s, ["units", -1, {"id": 2}])
    apply_update(s, ["wus", [1]])
    apply_update(s, ["wus", -2, [2, 3]])
    apply_update(s, ["units", 0, None])
    apply_update(s, ["groups", "x", "config", {"paused": True}])
    assert s["config"]["user"] == "b"
    assert s["units"] == [{"id": 2}]
    assert s["wus"] == [1, 2, 3]
    assert s["groups"]["x"]["config"]["paused"] is True


def test_redact():
    assert redact({"config": {"passkey": "abc", "user": "u"}, "secret": "s"}) == {
        "config": {"passkey": "**REDACTED**", "user": "u"},
        "secret": "**REDACTED**",
    }


def test_parse_eta():
    assert parse_eta("1h 20m") == 4800
    assert parse_eta("2 hours 3 mins") == 7380
    assert parse_eta("45s") == 45
    assert parse_eta("1d 2h") == 93600
    assert parse_eta("") is None


def test_machine_view():
    st = make_state()
    m = build_machine(st)
    assert m.status == "folding"
    assert m.ppd == 9250000
    assert m.projects == [18251, 12345]
    assert m.cpus_used == 31
    slots = {u.slot: u for u in m.units}
    assert set(slots) == {"|cpu", "|gpu:gpu:01:00:00"}
    assert slots["|gpu:gpu:01:00:00"].slot_name == "GeForce RTX 4090"
    assert round(m.max_progress) == 95
    st["groups"][""]["config"]["finish"] = True
    assert build_machine(st).status == "finishing"
    assert build_machine(st).units[0].status == "Finishing"
    st["groups"][""]["config"]["paused"] = True
    assert build_machine(st).status == "paused"
    st["groups"][""]["config"]["paused"] = False
    st["groups"][""]["config"]["finish"] = False
    st["units"] = []
    assert build_machine(st).status == "idle"
    # waiting for idle
    st = make_state()
    for u in st["units"]:
        u["pause_reason"] = "Waiting for idle system"
    m = build_machine(st)
    assert m.status == "waiting"
    assert m.units[0].status == "Waiting for idle system"


def test_project_tracker():
    t = ProjectTracker()
    assert t.ingest(wu_record("a", 1)) is not None
    assert t.ingest(wu_record("a", 1)) is None  # duplicate
    t.ingest(wu_record("b", 1, end="2026-10-21T00:00:00Z"))
    t.ingest(wu_record("c", 2, "failed"))
    t.ingest({"id": "d", "state": "RUN", "assignment": {"project": 3}})  # unfinished
    assert t.project_count == 2
    assert t.wu_count == 3
    assert t.credited_count == 2
    assert t.base_credit_total == 2000
    assert t.last_wu["unit_id"] == "b"
    t.set_project_info(1, {"cause": "cancer", "description": "<p>Hello&nbsp;<b>world</b></p>"})
    assert t.project_info["1"]["description"] == "Hello world"
    assert t.by_cause() == {"cancer": 2, "unknown": 1}
    t2 = ProjectTracker(t.as_dict())
    assert t2.ingest(wu_record("a", 1)) is None
    assert t2.wu_count == 3


def _settings(**kw):
    base = dict(
        start_w=300, stop_w=-100, start_delay_s=60, stop_delay_s=60, use_soc=False,
        min_soc=0, stop_soc=0, max_import_w=0, stop_action="smart", finish_above=90,
    )
    base.update(kw)
    return SolarSettings(**base)


def test_solar_hysteresis():
    logic = SolarLogic(_settings(), running=False)
    assert logic.evaluate(SolarReadings(500), 0) is None
    assert logic.status() == "starting"
    assert logic.evaluate(SolarReadings(200), 30) is None  # dip resets timer
    assert logic.status() == "waiting_for_surplus"
    assert logic.evaluate(SolarReadings(500), 40) is None
    assert logic.evaluate(SolarReadings(500), 100) == "start"
    assert logic.status() == "running_on_solar"
    # running: surplus drops (machine load) but above stop threshold -> keep going
    assert logic.evaluate(SolarReadings(0), 200) is None
    assert logic.evaluate(SolarReadings(-200), 300) is None
    assert logic.status() == "stopping"
    assert logic.evaluate(SolarReadings(-200), 361) == "stop"
    assert logic.stop_command(95) == "finish"
    assert logic.stop_command(40) == "pause"
    # sensor unavailable while running -> stop after delay
    logic = SolarLogic(_settings(stop_delay_s=0), running=True)
    assert logic.evaluate(SolarReadings(None), 0) == "stop"


def test_solar_guards():
    logic = SolarLogic(_settings(use_soc=True, min_soc=80, stop_soc=50, start_delay_s=0, stop_delay_s=0), False)
    assert logic.evaluate(SolarReadings(1000, soc=70), 0) is None
    assert logic.evaluate(SolarReadings(1000, soc=85), 1) == "start"
    assert logic.evaluate(SolarReadings(1000, soc=60), 2) is None
    assert logic.evaluate(SolarReadings(1000, soc=40), 3) == "stop"
    logic = SolarLogic(_settings(max_import_w=200, start_delay_s=0, stop_delay_s=0), True)
    assert logic.evaluate(SolarReadings(100, grid_import=500), 0) == "stop"


def test_validation():
    assert is_valid_passkey("")
    assert is_valid_passkey("0123456789abcdef0123456789ABCDEF")
    assert not is_valid_passkey("short!")
    assert is_valid_user("some_user")
    assert not is_valid_user("has space")


def test_zip_bomb_refused():
    import gzip

    import pytest

    from custom_components.foldingathome import fahcrypto as fc

    key, iv = fc.random_bytes(32), fc.random_bytes(16)
    bomb = gzip.compress(b"0" * (fc.MAX_DECOMPRESSED + 10))
    payload = fc.urlb64encode(fc.aes_encrypt(key, iv, bomb))
    with pytest.raises(fc.FahCryptoError):
        fc.decrypt_message(key, fc.urlb64encode(iv), payload, "gzip")


def test_node_host_validation():
    from custom_components.foldingathome.account import valid_node_host

    assert valid_node_host("node1.foldingathome.org")
    assert valid_node_host("127.0.0.1:8080")
    for bad in ("evil.com/x", "user:pw@evil.com", "evil.com?a=b", "localhost", "", None, "a.b:99999999"):
        assert not valid_node_host(bad), bad


def test_project_topics():
    from custom_components.foldingathome.projects import project_topic

    cases = [
        ("unspecified", "Human Pin1 ... a target for cancer therapeutics.", "cancer"),
        ("unspecified", "binding of ligands to the CD40L receptor, a target for autoimmune diseases.", "autoimmune"),
        ("unspecified", "AtWRI1 is a central regulator of lipid biosynthesis in plants.", "plants"),
        ("unspecified", "variability across a diverse set of enzymes", "enzymes"),
        ("unspecified", "Nipah virus (NiV) is a highly dangerous pathogen", "viruses"),
        ("cancer", "PFAS (per and poly-fluoroalkyl substances) are synthetic chemicals", "pfas"),
        ("cancer", "folding dynamics of several peptide variants", "cancer"),  # cause fallback
        ("alzheimers", "GLYT1 ... associated with neurological diseases", "alzheimers"),
        ("influenza", "antibodies undergo affinity maturation", "influenza"),
        ("unspecified", "antibodies undergo affinity maturation", "immune"),
        ("unspecified", "Protein misfolding ... diseases such as cancer and Alzheimer's", "misfolding"),
        ("unspecified", "Ebola virus glycoprotein", "ebola"),
        ("unspecified", None, "other"),
        (None, None, "other"),
    ]
    for cause, desc, expected in cases:
        assert project_topic({"cause": cause, "description": desc})[0] == expected, (cause, desc)


def test_recent_log_and_backfill():
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    t = ProjectTracker()
    t.ingest(wu_record("new", 1, end=iso(now - dt.timedelta(hours=2))))
    t.ingest(wu_record("day", 1, end=iso(now - dt.timedelta(hours=30))))
    t.ingest(wu_record("old", 2, end=iso(now - dt.timedelta(days=60))))  # counted, not logged
    assert t.wu_count == 3
    assert set(t.recent) == {"new", "day"}
    assert [p for _w, p in t.recent_since(now - dt.timedelta(hours=12))] == [1]
    assert len(t.recent_since(now - dt.timedelta(hours=48))) == 2
    # a 1.2 tracker (no "recent") fills the log from the client's history
    old = t.as_dict()
    old.pop("recent")
    t2 = ProjectTracker(old)
    assert t2.ingest(wu_record("new", 1, end=iso(now - dt.timedelta(hours=2)))) is None
    assert t2.wu_count == 3 and list(t2.recent) == ["new"]


def test_watt_units_case_sensitive():
    from types import SimpleNamespace

    from custom_components.foldingathome.solar import watt_value

    def st(value, unit):
        return SimpleNamespace(state=str(value), attributes={"unit_of_measurement": unit})

    assert watt_value(st(1.5, "kW")) == 1500
    assert watt_value(st(800, "W")) == 800
    assert watt_value(st(2500, "mW")) == 2.5  # milliwatt, not megawatt
    assert watt_value(st(0.002, "MW")) == 2000
    assert watt_value(st("unavailable", "W")) is None


def test_threshold_error():
    from custom_components.foldingathome.solar import threshold_error

    assert threshold_error({"solar_start_w": 300, "solar_stop_w": -100}) is None
    assert threshold_error({"solar_start_w": 300, "solar_stop_w": 300})[1] == "stop_above_start"
    soc = {"solar_soc_entity": "sensor.soc", "solar_min_soc": 90, "solar_stop_soc": 95}
    assert threshold_error(soc)[1] == "stop_soc_above_min"
    assert threshold_error({**soc, "solar_soc_entity": ""}) is None  # no battery sensor: ignored
    assert threshold_error({**soc, "solar_stop_soc": 90}) is None


def test_unpublished_projects_and_recent_strings():
    import datetime as dt

    from custom_components.foldingathome.projects import info_is_current

    t = ProjectTracker()
    t.ingest(wu_record("a", 13017))
    assert t.missing_info() == ["13017"]
    t.set_project_unpublished(13017)
    assert t.missing_info() == []  # remembered for a day
    t.project_info["13017"]["fetched"] = (dt.datetime.now(dt.UTC) - dt.timedelta(days=2)).isoformat()
    assert t.missing_info() == ["13017"]  # asked again after a day
    assert info_is_current({"cause": "cancer"}) and not info_is_current(None)
    # empty entries stored by 1.3.0 for unknown projects are fetched again
    t.project_info["13017"] = {"cause": None, "description": None, "fetched": "2026-10-01T00:00:00+00:00"}
    assert t.missing_info() == ["13017"]
    # finish times are normalised strings; mixed input formats still compare right
    now = dt.datetime.now(dt.UTC)
    t2 = ProjectTracker()
    t2.ingest(wu_record("z", 1, end=(now - dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")))
    t2.ingest(wu_record("y", 1, end=(now - dt.timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")))
    assert len(t2.recent_since(now - dt.timedelta(hours=1))) == 1
    assert len(t2.recent_since(now - dt.timedelta(hours=4))) == 2
    assert all(v[0].endswith("+00:00") for v in t2.recent.values())
