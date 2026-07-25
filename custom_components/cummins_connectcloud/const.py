"""Constants for the Cummins Connect Cloud integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "cummins_connectcloud"

# --- Cummins Connect Cloud mobile API (reverse-engineered from an mitmproxy
# capture of the iOS app; see docs/DESIGN.md for the full write-up). ----------
COGNITO_DOMAIN = "https://da-pcc-auth-production.auth.us-east-1.amazoncognito.com"
TOKEN_URL = f"{COGNITO_DOMAIN}/oauth2/token"
AUTHORIZE_URL = f"{COGNITO_DOMAIN}/oauth2/authorize"
CLIENT_ID = "28oqvfr332v3mp11u1tikqm565"  # public PKCE client, no secret
REDIRECT_URI = "connectcloud://authentication/callback"
SCOPE = "openid profile"
API_BASE = "https://cc.aws.powercommandcloud.com/api/dashboard/v1/mobile"
USER_AGENT = "ConnectCloud_Maui/80 CFNetwork/3860.600.12 Darwin/25.5.0"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_ASSET_ID = "asset_id"
CONF_ASSET_NAME = "asset_name"

# Telemetry looks event-driven rather than truly real-time; this is gentle on
# the API while staying well ahead of anything a human would notice as stale.
DEFAULT_SCAN_INTERVAL = timedelta(minutes=2)

# How long without a check-in before we flag the data as stale/generator
# offline. Borrowed from prior art (wareed1's MQTT bridge) rather than a
# documented SLA from Cummins.
STALE_AFTER = timedelta(hours=25)
