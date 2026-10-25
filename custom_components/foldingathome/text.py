"""Text entity: donor user name."""

from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import FahAccountCoordinator, FahConfigEntry
from .entity import FahAccountEntity
from .validation import is_valid_user

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up text entities."""
    coordinator = entry.runtime_data
    # The passkey is deliberately NOT a text entity: entity states and
    # service calls are written to the recorder database. Set it from the
    # integration's Configure dialog instead (stored only in the config entry).
    async_add_entities([FahUserText(coordinator)])


class FahUserText(FahAccountEntity, TextEntity):
    """Donor user name of the account."""

    _attr_translation_key = "user"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_max = 100
    _attr_icon = "mdi:account"

    def __init__(self, coordinator: FahAccountCoordinator) -> None:
        super().__init__(coordinator, "user")

    @property
    def native_value(self) -> str:
        """User."""
        return self.coordinator.account.get("user") or "Anonymous"

    async def async_set_value(self, value: str) -> None:
        """Set user."""
        value = value.strip()
        if not is_valid_user(value):
            raise ServiceValidationError("User name must be 1-100 characters with no spaces")
        await self.coordinator.async_set_identity(user=value)
        self.async_write_ha_state()
