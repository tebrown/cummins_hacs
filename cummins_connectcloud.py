"""
Cummins Connect Cloud — mobile API client (pure requests, no browser at runtime).

Auth model (confirmed from a mitmproxy capture of the iOS app):
  * AWS Cognito hosted UI, PUBLIC PKCE client -> NO client secret needed.
  * You bootstrap a refresh_token ONCE (from the app via mitmproxy, or an
    interactive PKCE login). After that this client silently mints short-lived
    access tokens via grant_type=refresh_token and calls the mobile API with
    Authorization: Bearer <access_token>.
  * The refresh_token is the only long-lived secret. Guard it like a password.
    Cognito refresh tokens commonly last 30 days (sometimes far longer); when it
    finally expires you re-bootstrap once.

Endpoints seen in the capture (all GET -> JSON unless noted):
  /api/dashboard/v1/mobile/Profile
  /api/dashboard/v1/mobile/Sites/Personal            -> your sites + assets
  /api/dashboard/v1/mobile/Sites/GetAssets?id=<siteId>
  /api/dashboard/v1/mobile/Assets/Detail?id=<assetId>   -> live telemetry
  /api/dashboard/v1/mobile/Assets/Events?id=<assetId>&from=<epoch_ms>
  /api/dashboard/v1/mobile/Assets/Commands?id=<assetId>

Requires: requests
"""

from __future__ import annotations
import json
import time
import os
from pathlib import Path
import requests

# --- Constants pulled directly from the capture -----------------------------
COGNITO_DOMAIN = "https://da-pcc-auth-production.auth.us-east-1.amazoncognito.com"
TOKEN_URL      = f"{COGNITO_DOMAIN}/oauth2/token"
AUTHORIZE_URL  = f"{COGNITO_DOMAIN}/oauth2/authorize"
CLIENT_ID      = "28oqvfr332v3mp11u1tikqm565"          # public mobile client
REDIRECT_URI   = "connectcloud://authentication/callback"
SCOPE          = "openid profile"
API_BASE       = "https://cc.aws.powercommandcloud.com/api/dashboard/v1/mobile"

# The app's own UA — harmless to mirror, and safest for looking like the app.
USER_AGENT = "ConnectCloud_Maui/80 CFNetwork/3860.600.12 Darwin/25.5.0"

DEFAULT_TOKEN_CACHE = Path.home() / ".cummins_tokens.json"


class CumminsAuthError(RuntimeError):
    pass


class CumminsConnectCloud:
    """Refresh-token-driven client for the Cummins Connect Cloud mobile API."""

    def __init__(self, refresh_token: str | None = None,
                 token_cache: Path | str = DEFAULT_TOKEN_CACHE):
        self.token_cache = Path(token_cache)
        self._access_token: str | None = None
        self._id_token: str | None = None         # sent as the 'mobile-data' header
        self._access_expiry: float = 0.0          # epoch seconds
        self._refresh_token: str | None = refresh_token

        # Load a cached refresh (and maybe still-valid access) token if present.
        self._load_cache()
        if refresh_token:                          # explicit arg wins
            self._refresh_token = refresh_token

        if not self._refresh_token:
            raise CumminsAuthError(
                "No refresh_token. Pass one in, set CUMMINS_REFRESH_TOKEN, "
                "or bootstrap one (see interactive_login())."
            )

        self.session = requests.Session()
        self.session.headers.update({
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": USER_AGENT,
        })

    # --- token cache --------------------------------------------------------
    def _load_cache(self) -> None:
        if self.token_cache.exists():
            try:
                data = json.loads(self.token_cache.read_text())
                self._refresh_token = data.get("refresh_token") or self._refresh_token
                self._access_token = data.get("access_token")
                self._id_token = data.get("id_token")
                self._access_expiry = data.get("access_expiry", 0.0)
            except (json.JSONDecodeError, OSError):
                pass

    def _save_cache(self) -> None:
        try:
            self.token_cache.write_text(json.dumps({
                "refresh_token": self._refresh_token,
                "access_token": self._access_token,
                "id_token": self._id_token,
                "access_expiry": self._access_expiry,
            }))
            os.chmod(self.token_cache, 0o600)      # secret on disk -> lock it down
        except OSError:
            pass

    # --- auth ---------------------------------------------------------------
    def _refresh_access_token(self) -> None:
        """Exchange the refresh_token for a fresh access_token (Cognito)."""
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
            # A 400 "invalid_grant" here almost always means the refresh token
            # expired or was revoked -> you must re-bootstrap.
            raise CumminsAuthError(
                f"Refresh failed ({resp.status_code}): {resp.text[:300]}"
            )
        tok = resp.json()
        self._access_token = tok["access_token"]
        # The API reads identity from the ID token, sent as the 'mobile-data'
        # header (it carries sub/email/group). A refresh grant returns a fresh
        # id_token; keep it. Without it the API 401s ("Cannot read 'sub'").
        if tok.get("id_token"):
            self._id_token = tok["id_token"]
        # refresh cycles can (optionally) return a new refresh_token; keep it.
        if tok.get("refresh_token"):
            self._refresh_token = tok["refresh_token"]
        # renew 60s early to avoid edge-of-expiry 401s.
        self._access_expiry = time.time() + int(tok.get("expires_in", 3600)) - 60
        self._save_cache()

    def _valid_access_token(self) -> str:
        if not self._access_token or time.time() >= self._access_expiry:
            self._refresh_access_token()
        return self._access_token

    # --- core request helper ------------------------------------------------
    def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{API_BASE}{path}"
        for attempt in (1, 2):                     # one retry on a 401
            token = self._valid_access_token()
            r = self.session.get(
                url, params=params,
                headers={
                    "authorization": f"Bearer {token}",
                    "mobile-data": self._id_token or "",
                },
                timeout=30,
            )
            if r.status_code == 401 and attempt == 1:
                self._access_token = None          # force a refresh, retry once
                continue
            r.raise_for_status()
            return r.json()

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
        params = {"id": asset_id}
        if from_epoch_ms is not None:
            params["from"] = from_epoch_ms
        return self._get("/Assets/Events", params=params)

    def asset_commands(self, asset_id: str) -> dict:
        return self._get("/Assets/Commands", params={"id": asset_id})

    # --- convenience: flatten telemetry Properties into a plain dict ---------
    @staticmethod
    def telemetry(detail: dict) -> dict:
        """Turn Assets/Detail LastTelemetry.Properties into {name: typed_value}."""
        out: dict[str, object] = {}
        props = (detail.get("LastTelemetry") or {}).get("Properties", [])
        for p in props:
            name, raw = p.get("Name"), p.get("Value")
            # best-effort numeric coercion; leave strings as-is
            try:
                out[name] = float(raw) if ("." in str(raw)) else int(raw)
            except (TypeError, ValueError):
                out[name] = raw
        if detail.get("LastCheckIn"):
            out["LastCheckIn"] = detail["LastCheckIn"]
        return out

    def first_asset_id(self) -> str:
        """Handy for single-generator setups: id of the first asset found."""
        for site in self.sites():
            for s in site.get("Sites", []):
                for a in s.get("Assets", []):
                    return a["Id"]
        raise CumminsAuthError("No assets found on this account.")


# ---------------------------------------------------------------------------
# One-time bootstrap helper: interactive PKCE login (no mitmproxy needed).
# Only use this if you'd rather not pull the refresh token from a capture.
# It opens the Cognito hosted UI in your browser; after login the app would
# normally receive connectcloud://authentication/callback?code=... — you paste
# that code back here and it exchanges it for tokens.
# ---------------------------------------------------------------------------
def interactive_login(token_cache: Path | str = DEFAULT_TOKEN_CACHE) -> None:
    import base64, hashlib, secrets, urllib.parse, webbrowser

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    print("\n" + "="*70)
    print("STEP 1 — open this URL in your browser and sign in (MFA is fine):\n")
    print(url)
    print("\nSTEP 2 — after login, your browser will try to open a page at")
    print(f"  {REDIRECT_URI}?code=...&state=...")
    print("It will FAIL to load (that's expected — it's a phone-app link your")
    print("desktop browser can't open). The full URL is still in the address")
    print("bar. Copy the ENTIRE address and paste it below.")
    print("="*70 + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    pasted = input("Paste the full redirect URL (or just the code): ").strip()

    # Accept either the whole connectcloud://... URL or a bare code.
    if "code=" in pasted:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
        code = q.get("code", [""])[0]
        returned_state = q.get("state", [None])[0]
        if returned_state and returned_state != state:
            raise CumminsAuthError(
                "State mismatch — the pasted URL doesn't match this session. "
                "Restart the login and paste the URL from THIS run."
            )
    else:
        code = pasted

    if not code:
        raise CumminsAuthError("No authorization code found in what you pasted.")

    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }, headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8",
                "user-agent": USER_AGENT}, timeout=30)
    if resp.status_code != 200:
        raise CumminsAuthError(
            f"Token exchange failed ({resp.status_code}): {resp.text[:300]}\n"
            "Common cause: the code is single-use and expires in ~minutes — "
            "if you were slow, just rerun and go quickly."
        )
    tok = resp.json()
    Path(token_cache).write_text(json.dumps({
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token"),
        "id_token": tok.get("id_token"),
        "access_expiry": time.time() + int(tok.get("expires_in", 3600)) - 60,
    }))
    os.chmod(token_cache, 0o600)
    print(f"\n✓ Saved tokens to {token_cache}. You're bootstrapped — no browser")
    print("  needed from here on. Run `test` to verify.")


# ---------------------------------------------------------------------------
# CLI: quick self-test.  Bootstrap first (one of):
#   * export CUMMINS_REFRESH_TOKEN=<the refresh_token from your capture>
#   * python cummins_connectcloud.py login      # interactive PKCE
# Then:
#   python cummins_connectcloud.py test          # refresh + dump telemetry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "test"

    if cmd == "login":
        interactive_login()
        sys.exit(0)

    rt = os.environ.get("CUMMINS_REFRESH_TOKEN")
    client = CumminsConnectCloud(refresh_token=rt)

    if cmd == "test":
        print("Refreshing access token ...")
        client._refresh_access_token()
        print("  OK, access token acquired.\n")

        sites = client.sites()
        print("Sites/Personal OK.")
        asset_id = client.first_asset_id()
        print(f"First asset id: {asset_id}\n")

        detail = client.asset_detail(asset_id)
        t = client.telemetry(detail)
        print("Telemetry:")
        for k, v in t.items():
            print(f"  {k:22s} {v}")
    else:
        print(f"Unknown command: {cmd!r}. Use 'login' or 'test'.")
