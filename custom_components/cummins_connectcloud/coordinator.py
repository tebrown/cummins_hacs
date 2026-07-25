"""DataUpdateCoordinator for Cummins Connect Cloud."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import CumminsApiError, CumminsAuthError, CumminsConnectCloudApi
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class CumminsDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls a single generator asset's live telemetry."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: CumminsConnectCloudApi,
        asset_id: str,
    ) -> None:
        self.api = api
        self.asset_id = asset_id
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{asset_id}",
            update_interval=DEFAULT_SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            detail = await self.hass.async_add_executor_job(
                self.api.asset_detail, self.asset_id
            )
        except CumminsAuthError as err:
            # Surfaces HA's built-in reauth flow instead of failing silently.
            raise ConfigEntryAuthFailed(str(err)) from err
        except CumminsApiError as err:
            raise UpdateFailed(str(err)) from err

        return self.api.telemetry(detail)
