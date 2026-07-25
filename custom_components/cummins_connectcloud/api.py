"""Cummins Connect Cloud mobile API client.

Pure ``requests`` — no browser at runtime. This is a Home-Assistant-flavored
port of the standalone client in ``tools/cummins_connectcloud.py`` (see that
file and ``docs/DESIGN.md`` for how the auth was reverse-engineered and why a
browser is only ever needed once, out-of-band, via ``tools/bootstrap_login.py``).

All methods that hit the network are synchronous (``requests``) — callers in
this integration always run them via ``hass.async_add_executor_job``.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

from .const import API_BASE, CLIENT_ID, TOKEN_URL, USER_AGENT


class CumminsAuthError(Exception):
    """Raised when the refresh token is invalid, expired, or revoked."""


class CumminsApiError(Exception):
    """Raised for any other (non-auth) API failure."""


class CumminsConnectCloudApi:
    """Refresh-token-driven client for the Cummins Connect Cloud mobile API."""

    def __init__(
        self,
        refresh_token: str,
        session: requests.Session | None = None,
        token_update_callback: Callable[[str], None] | None = None,
    ) -> None:
        """Set up the client.

        ``token_update_callback``, if given, is called with the new refresh
        token on the (uncommon) occasions Cognito rotates it on refresh, so
        the caller can persist it back into the config entry.
        """
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._id_token: str | None = None  # sent as the 'mobile-data' header
        self._access_expiry: float = 0.0
        self._token_update_callback = token_update_callback

        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "accept": "*/*",
                "accept-language": "en-US,en;q=0.9",
                "user-agent": USER_AGENT,
            }
        )

    # --- auth -----------------------------------------------------------
    def _refresh_access_token(self) -> None:
        """Exchange the refresh token for a fresh access + id token."""
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": self._refresh_token,
            },
            headers={
                "content-type": "application/x-www-form-urlencoded; charset=utf-8",
                "user-agent": USER_AGENT,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            # A 400 invalid_grant here means the refresh token expired or was
            # revoked -> the user must re-bootstrap and re-authenticate in HA.
            raise CumminsAuthError(
                f"Refresh failed ({resp.status_code}): {resp.text[:300]}"
            )
        tok = resp.json()
        self._access_token = tok["access_token"]
        # The API reads identity off the ID token via the 'mobile-data'
        # header. Without it every call 401s ("Cannot read 'sub' of null").
        if tok.get("id_token"):
            self._id_token = tok["id_token"]
        new_refresh = tok.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self._token_update_callback:
                self._token_update_callback(new_refresh)
        # renew 60s early to avoid edge-of-expiry 401s
        self._access_expiry = time.time() + int(tok.get("expires_in", 3600)) - 60

    def _valid_access_token(self) -> str:
        if not self._access_token or time.time() >= self._access_expiry:
            self._refresh_access_token()
        return self._access_token

    # --- core request helper --------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{API_BASE}{path}"
        last_status = None
        for attempt in (1, 2):  # one retry on a 401 (forces a fresh token)
            token = self._valid_access_token()
            resp = self.session.get(
                url,
                params=params,
                headers={
                    "authorization": f"Bearer {token}",
                    "mobile-data": self._id_token or "",
                },
                timeout=30,
            )
            if resp.status_code == 401 and attempt == 1:
                self._access_token = None
                continue
            last_status = resp.status_code
            if resp.status_code >= 400:
                raise CumminsApiError(
                    f"{path} failed ({resp.status_code}): {resp.text[:300]}"
                )
            return resp.json()
        raise CumminsApiError(f"{path} failed ({last_status}) after retry")

    # --- validation -------------------------------------------------------
    def validate(self) -> None:
        """Cheap round trip to confirm the refresh token actually works."""
        self._refresh_access_token()
        self.profile()

    # --- endpoint wrappers --------------------------------------------------
    def profile(self) -> dict:
        return self._get("/Profile")

    def sites(self) -> list:
        """Your sites, each with its assets (generators)."""
        return self._get("/Sites/Personal")

    def site_assets(self, site_id: str) -> dict:
        return self._get("/Sites/GetAssets", params={"id": site_id})

    def asset_detail(self, asset_id: str) -> dict:
        """Live telemetry snapshot for one generator."""
        return self._get("/Assets/Detail", params={"id": asset_id})

    def asset_events(self, asset_id: str, from_epoch_ms: int | None = None) -> dict:
        params: dict[str, Any] = {"id": asset_id}
        if from_epoch_ms is not None:
            params["from"] = from_epoch_ms
        return self._get("/Assets/Events", params=params)

    def asset_commands(self, asset_id: str) -> dict:
        return self._get("/Assets/Commands", params={"id": asset_id})

    # --- convenience --------------------------------------------------------
    def list_assets(self) -> list[dict[str, Any]]:
        """Flatten Sites/Personal into [{id, name, site_name}, ...]."""
        assets: list[dict[str, Any]] = []
        for entry in self.sites():
            for site in entry.get("Sites", []):
                site_name = site.get("Name") or site.get("SiteName") or "Cummins Site"
                for asset in site.get("Assets", []):
                    assets.append(
                        {
                            "id": asset["Id"],
                            "name": asset.get("Name")
                            or asset.get("AssetName")
                            or "Generator",
                            "site_name": site_name,
                        }
                    )
        return assets

    @staticmethod
    def telemetry(detail: dict) -> dict[str, Any]:
        """Turn Assets/Detail's LastTelemetry.Properties into {name: value}."""
        out: dict[str, Any] = {}
        props = (detail.get("LastTelemetry") or {}).get("Properties", [])
        for prop in props:
            name, raw = prop.get("Name"), prop.get("Value")
            # best-effort numeric coercion; leave strings (and None) as-is
            try:
                out[name] = float(raw) if ("." in str(raw)) else int(raw)
            except (TypeError, ValueError):
                out[name] = raw
        if detail.get("LastCheckIn"):
            out["LastCheckIn"] = detail["LastCheckIn"]
        return out
