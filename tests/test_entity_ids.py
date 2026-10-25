"""Entity ids used in examples/dashboard.yaml exist."""

import re
from pathlib import Path

from homeassistant.core import HomeAssistant

from .test_integration import _login, _wait


async def test_dashboard_entity_ids(hass: HomeAssistant, cloud) -> None:
    await _login(hass)
    await _wait(lambda: hass.states.get("sensor.rig_1_geforce_rtx_4090_progress") is not None)
    ids = {s.entity_id for s in hass.states.async_all()}
    dash = (Path(__file__).parent.parent / "examples" / "dashboard.yaml").read_text()
    wanted = set(re.findall(r"\b(?:sensor|select|switch|number|button)\.(?:rig_1|folding_home_account)_\w+", dash))
    assert wanted - ids == set(), sorted(i for i in ids if "rig_1" in i or "account" in i)
