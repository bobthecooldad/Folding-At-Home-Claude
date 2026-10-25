"""Serve the dashboard card and load it into the HA frontend."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = Path(__file__).parent / "frontend" / "foldingathome-card.js"
CARD_URL = f"/{DOMAIN}/foldingathome-card.js"


async def async_register_card(hass: HomeAssistant, version: str) -> None:
    """Register the static file and add it to every dashboard page.

    After this, ``type: custom:foldingathome-card`` works and the card is in
    the dashboard's card picker; no manual "resources" entry is needed.
    """
    if getattr(hass, "http", None) is None or "frontend" not in hass.config.components:
        return  # e.g. tests without the frontend
    from homeassistant.components.frontend import add_extra_js_url  # noqa: PLC0415
    from homeassistant.components.http import StaticPathConfig  # noqa: PLC0415

    try:
        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_FILE), cache_headers=False)]
        )
    except RuntimeError:
        pass  # already registered (integration reloaded)
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    _LOGGER.debug("Folding@home dashboard card registered at %s", CARD_URL)
