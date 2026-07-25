"""Config flow for Cummins Connect Cloud.

There is no interactive OAuth here: HA is headless and shouldn't bundle a
browser just to complete a JS-rendered Salesforce SSO login. Instead the user
runs ``tools/bootstrap_login.py`` once, off-box, and pastes the resulting
refresh token in. See docs/DESIGN.md section 5 for why.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import CumminsApiError, CumminsAuthError, CumminsConnectCloudApi
from .const import CONF_ASSET_ID, CONF_ASSET_NAME, CONF_REFRESH_TOKEN, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_REFRESH_TOKEN): str})


class CumminsConnectCloudConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Cummins Connect Cloud."""

    VERSION = 1

    def __init__(self) -> None:
        self._refresh_token: str | None = None
        self._assets: list[dict[str, Any]] = []
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def _validate_and_list_assets(
        self, refresh_token: str
    ) -> list[dict[str, Any]]:
        """Raises CumminsAuthError/CumminsApiError on failure."""
        api = CumminsConnectCloudApi(refresh_token=refresh_token)
        await self.hass.async_add_executor_job(api.validate)
        return await self.hass.async_add_executor_job(api.list_assets)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First step: collect and validate the refresh token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                assets = await self._validate_and_list_assets(
                    user_input[CONF_REFRESH_TOKEN]
                )
            except CumminsAuthError:
                errors["base"] = "invalid_auth"
            except CumminsApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating refresh token")
                errors["base"] = "unknown"
            else:
                if not assets:
                    errors["base"] = "no_assets"
                else:
                    self._refresh_token = user_input[CONF_REFRESH_TOKEN]
                    self._assets = assets
                    if len(assets) == 1:
                        return await self._async_create_entry(assets[0])
                    return await self.async_step_asset()

        return self.async_show_form(
            step_id="user", data_schema=STEP_TOKEN_SCHEMA, errors=errors
        )

    async def async_step_asset(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Second step (multi-generator accounts only): pick which asset."""
        if user_input is not None:
            asset = next(a for a in self._assets if a["id"] == user_input[CONF_ASSET_ID])
            return await self._async_create_entry(asset)

        options = {a["id"]: f'{a["name"]} ({a["site_name"]})' for a in self._assets}
        return self.async_show_form(
            step_id="asset",
            data_schema=vol.Schema({vol.Required(CONF_ASSET_ID): vol.In(options)}),
        )

    async def _async_create_entry(self, asset: dict[str, Any]) -> FlowResult:
        await self.async_set_unique_id(asset["id"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=asset["name"],
            data={
                CONF_REFRESH_TOKEN: self._refresh_token,
                CONF_ASSET_ID: asset["id"],
                CONF_ASSET_NAME: asset["name"],
            },
        )

    # --- reauth -------------------------------------------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Triggered by ConfigEntryAuthFailed when the refresh token dies."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                api = CumminsConnectCloudApi(refresh_token=user_input[CONF_REFRESH_TOKEN])
                await self.hass.async_add_executor_job(api.validate)
            except CumminsAuthError:
                errors["base"] = "invalid_auth"
            except CumminsApiError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating refresh token")
                errors["base"] = "unknown"
            else:
                assert self._reauth_entry is not None
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={
                        **self._reauth_entry.data,
                        CONF_REFRESH_TOKEN: user_input[CONF_REFRESH_TOKEN],
                    },
                )
                await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm", data_schema=STEP_TOKEN_SCHEMA, errors=errors
        )
