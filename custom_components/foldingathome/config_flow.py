"""Config flow: log in to a Folding@home account."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .account import (
    AccountCredentials,
    FahAccountApi,
    FahAuthError,
    FahConnectionError,
    valid_node_host,
)
from .const import (
    CONF_ACCOUNT,
    CONF_CAUSE,
    CONF_EMAIL,
    CONF_PASSKEY,
    CONF_PASSPHRASE,
    CONF_PRIVATE_KEY,
    CONF_SID,
    CONF_SOLAR_ENABLED,
    CONF_SOLAR_FINISH_ABOVE,
    CONF_SOLAR_IMPORT_ENTITY,
    CONF_SOLAR_MAX_IMPORT_W,
    CONF_SOLAR_MIN_SOC,
    CONF_SOLAR_PRIORITY,
    CONF_SOLAR_SOC_ENTITY,
    CONF_SOLAR_START_DELAY,
    CONF_SOLAR_START_W,
    CONF_SOLAR_STOP_ACTION,
    CONF_SOLAR_STOP_DELAY,
    CONF_SOLAR_STOP_SOC,
    CONF_SOLAR_STOP_W,
    CONF_SOLAR_SURPLUS_ENTITY,
    CONF_TEAM,
    CONF_USER,
    DOMAIN,
    KNOWN_CAUSES,
    STOP_ACTIONS,
)
from .fahcrypto import FahCryptoError
from .solar import threshold_error
from .validation import is_valid_passkey, is_valid_user

_LOGGER = logging.getLogger(__name__)

CONF_CLEAR_PASSKEY = "clear_passkey"
CONF_MACHINE = "machine"
ACCOUNT_CACHE_KEYS = ("pubkey", "node", "machines", "user", "team", "passkey", "cause", "avatar", "config", "id")


def _entry_data(creds: AccountCredentials) -> dict[str, Any]:
    return {
        CONF_EMAIL: creds.email,
        CONF_SID: creds.sid,
        CONF_PRIVATE_KEY: creds.private_key_b64,
        CONF_ACCOUNT: {k: creds.account.get(k) for k in ACCOUNT_CACHE_KEYS},
    }


class FahConfigFlow(ConfigFlow, domain=DOMAIN):
    """Log in with the account email + passphrase."""

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FahOptionsFlow:
        """Options."""
        return FahOptionsFlow()

    async def _login(self, email: str, passphrase: str) -> tuple[AccountCredentials | None, str | None]:
        api = FahAccountApi(async_get_clientsession(self.hass))
        try:
            return await api.login(email, passphrase), None
        except (FahAuthError, FahCryptoError) as err:
            _LOGGER.debug("Folding@home login failed: %s", err)
            if "no stored key" in str(err):
                return None, "no_secret"
            return None, "invalid_auth"
        except FahConnectionError:
            return None, "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error logging in to Folding@home")
            return None, "unknown"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Email + passphrase."""
        errors: dict[str, str] = {}
        if user_input is not None:
            creds, error = await self._login(user_input[CONF_EMAIL], user_input[CONF_PASSPHRASE])
            if error:
                errors["base"] = error
            else:
                assert creds is not None
                if not valid_node_host(creds.account.get("node")):
                    errors["base"] = "no_node"
                else:
                    await self.async_set_unique_id(creds.account_id)
                    self._abort_if_unique_id_configured()
                    count = len(creds.account.get("machines") or [])
                    title = f"Folding@home ({creds.account.get('user') or creds.email})"
                    _LOGGER.info("Logged in to Folding@home account with %d machine(s)", count)
                    return self.async_create_entry(title=title, data=_entry_data(creds))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                    ),
                    vol.Required(CONF_PASSPHRASE): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD, autocomplete="current-password"
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Session expired or account key changed."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the passphrase again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            creds, error = await self._login(entry.data[CONF_EMAIL], user_input[CONF_PASSPHRASE])
            if error:
                errors["base"] = error
            else:
                assert creds is not None
                await self.async_set_unique_id(creds.account_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=_entry_data(creds))
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSPHRASE): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


def _watts(min_v: float = -5000, max_v: float = 20000) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=min_v, max=max_v, step=10, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
    )


def _pct() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=0, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.SLIDER)
    )


def _minutes() -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=0, max=120, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX)
    )


class FahOptionsFlow(OptionsFlow):
    """Account identity and per machine solar settings."""

    def __init__(self) -> None:
        self._machine: str | None = None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu."""
        return self.async_show_menu(step_id="init", menu_options=["identity", "solar_machine"])

    async def async_step_identity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """User / team / passkey / cause for the whole account."""
        coordinator = self.config_entry.runtime_data
        acc = coordinator.account
        errors: dict[str, str] = {}
        if user_input is not None:
            user = str(user_input.get(CONF_USER) or "").strip()
            passkey = str(user_input.get(CONF_PASSKEY) or "").strip()
            values: dict[str, Any] = {
                CONF_USER: user,
                CONF_TEAM: int(user_input.get(CONF_TEAM) or 0),
                CONF_CAUSE: user_input.get(CONF_CAUSE) or "any",
            }
            if not is_valid_user(user):
                errors[CONF_USER] = "invalid_user"
            if user_input.get(CONF_CLEAR_PASSKEY):
                values[CONF_PASSKEY] = ""
            elif passkey:
                if is_valid_passkey(passkey):
                    values[CONF_PASSKEY] = passkey
                else:
                    errors[CONF_PASSKEY] = "invalid_passkey"
            if not errors:
                try:
                    await coordinator.async_set_identity(**values)
                except HomeAssistantError as err:
                    _LOGGER.warning("Saving identity failed: %s", err)
                    errors["base"] = "cannot_save"
            if not errors:
                return self.async_create_entry(data=dict(self.config_entry.options))

        cause = acc.get("cause") or "any"
        schema = vol.Schema(
            {
                vol.Required(CONF_USER, default=acc.get("user") or "Anonymous"): str,
                vol.Required(CONF_TEAM, default=int(acc.get("team") or 0)): vol.All(
                    vol.Coerce(int), vol.Range(min=0)
                ),
                vol.Optional(CONF_PASSKEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_CLEAR_PASSKEY, default=False): BooleanSelector(),
                vol.Required(
                    CONF_CAUSE, default=cause if cause in KNOWN_CAUSES else "any"
                ): SelectSelector(SelectSelectorConfig(options=KNOWN_CAUSES, translation_key="cause")),
            }
        )
        return self.async_show_form(
            step_id="identity",
            data_schema=schema,
            errors=errors,
            description_placeholders={"passkey_state": "set" if acc.get("passkey") else "not set"},
        )

    async def async_step_solar_machine(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the machine."""
        coordinator = self.config_entry.runtime_data
        if user_input is not None:
            self._machine = user_input[CONF_MACHINE]
            return await self.async_step_solar()
        options = [
            SelectOptionDict(value=mid, label=coordinator.machine_name(mid))
            for mid in coordinator.contexts
        ]
        if not options:
            return self.async_abort(reason="no_machines")
        return self.async_show_form(
            step_id="solar_machine",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MACHINE, default=options[0]["value"]): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_solar(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Solar surplus control for one machine."""
        coordinator = self.config_entry.runtime_data
        assert self._machine is not None
        opts = coordinator.machine_options(self._machine)
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(CONF_SOLAR_ENABLED) and not user_input.get(CONF_SOLAR_SURPLUS_ENTITY):
                errors[CONF_SOLAR_SURPLUS_ENTITY] = "surplus_required"
            elif bad := threshold_error(user_input):
                errors[bad[0]] = bad[1]
            else:
                values = dict(user_input)
                for key in (CONF_SOLAR_SURPLUS_ENTITY, CONF_SOLAR_SOC_ENTITY, CONF_SOLAR_IMPORT_ENTITY):
                    values.setdefault(key, "")
                coordinator.async_update_machine_options(self._machine, values)
                return self.async_create_entry(data=dict(self.config_entry.options))
            opts.update(user_input)

        def sv(key: str) -> dict[str, Any]:
            return {"suggested_value": opts.get(key)}

        sensor_sel = EntitySelector(EntitySelectorConfig(domain=["sensor", "input_number"]))
        schema = vol.Schema(
            {
                vol.Required(CONF_SOLAR_ENABLED, default=opts[CONF_SOLAR_ENABLED]): BooleanSelector(),
                vol.Optional(CONF_SOLAR_SURPLUS_ENTITY, description=sv(CONF_SOLAR_SURPLUS_ENTITY)): sensor_sel,
                vol.Required(CONF_SOLAR_PRIORITY, default=opts[CONF_SOLAR_PRIORITY]): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_SOLAR_START_W, default=opts[CONF_SOLAR_START_W]): _watts(),
                vol.Required(CONF_SOLAR_STOP_W, default=opts[CONF_SOLAR_STOP_W]): _watts(),
                vol.Required(CONF_SOLAR_START_DELAY, default=opts[CONF_SOLAR_START_DELAY]): _minutes(),
                vol.Required(CONF_SOLAR_STOP_DELAY, default=opts[CONF_SOLAR_STOP_DELAY]): _minutes(),
                vol.Required(CONF_SOLAR_STOP_ACTION, default=opts[CONF_SOLAR_STOP_ACTION]): SelectSelector(
                    SelectSelectorConfig(
                        options=STOP_ACTIONS, translation_key="solar_stop_action", mode=SelectSelectorMode.LIST
                    )
                ),
                vol.Required(CONF_SOLAR_FINISH_ABOVE, default=opts[CONF_SOLAR_FINISH_ABOVE]): _pct(),
                vol.Optional(CONF_SOLAR_SOC_ENTITY, description=sv(CONF_SOLAR_SOC_ENTITY)): sensor_sel,
                vol.Required(CONF_SOLAR_MIN_SOC, default=opts[CONF_SOLAR_MIN_SOC]): _pct(),
                vol.Required(CONF_SOLAR_STOP_SOC, default=opts[CONF_SOLAR_STOP_SOC]): _pct(),
                vol.Optional(CONF_SOLAR_IMPORT_ENTITY, description=sv(CONF_SOLAR_IMPORT_ENTITY)): sensor_sel,
                vol.Required(CONF_SOLAR_MAX_IMPORT_W, default=opts[CONF_SOLAR_MAX_IMPORT_W]): _watts(0),
            }
        )
        return self.async_show_form(
            step_id="solar",
            data_schema=schema,
            errors=errors,
            description_placeholders={"machine": coordinator.machine_name(self._machine)},
        )
