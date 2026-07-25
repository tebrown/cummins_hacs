"""The Cummins Connect Cloud integration.

Polls a Cummins home-standby generator's telemetry via the Cummins Connect
Cloud mobile API. Read-only in this version — no start/stop/exercise
commands yet (see docs/DESIGN.md, phase 2).
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .api import CumminsApiError, CumminsAuthError, CumminsConnectCloudApi
from .const import CONF_ASSET_ID, CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import CumminsDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Cummins Connect Cloud from a config entry."""

    def _persist_refresh_token(new_token: str) -> None:
        # Cognito occasionally rotates the refresh token on refresh; keep the
        # config entry in sync so a restart doesn't hand it a stale one.
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: new_token}
        )

    api = CumminsConnectCloudApi(
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        token_update_callback=_persist_refresh_token,
    )

    try:
        await hass.async_add_executor_job(api.validate)
    except CumminsAuthError as err:
        raise ConfigEntryAuthFailed(
            "Refresh token was rejected — re-authenticate"
        ) from err
    except CumminsApiError as err:
        raise ConfigEntryNotReady(f"Cannot reach Cummins Connect Cloud: {err}") from err

    coordinator = CumminsDataUpdateCoordinator(hass, entry, api, entry.data[CONF_ASSET_ID])
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
