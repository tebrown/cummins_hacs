# Cummins Connect Cloud → Home Assistant Integration

**Status:** §5 is scaffolded and the config flow now supports native username/password sign-in (`aura_auth.py`, no browser anywhere) with the refresh-token paste flow kept as a fallback — `custom_components/cummins_connectcloud/` is installable via HACS (read-only sensors/binary sensors). This document is kept as the design record; see the repo [README](../README.md) for user-facing install/usage instructions.
**Goal:** A `custom_components/cummins_connectcloud/` integration that polls a Cummins home-standby generator's telemetry (and optionally issues start/stop/exercise commands) via the Cummins Connect Cloud **mobile** API — with no browser dependency at runtime.

---

## 1. What we have (working today)

Two Python files (now under `tools/`), both verified end-to-end against a live account, plus the scaffolded integration under `custom_components/cummins_connectcloud/` (ported from the client below):

| File | Role | Runtime deps |
|---|---|---|
| `tools/bootstrap_login.py` | **One-time** login. Headless Playwright drives username/password login, catches the OAuth `code`, exchanges it, writes `~/.cummins_tokens.json`. | playwright, requests |
| `tools/cummins_connectcloud.py` | **Standalone reference** client / CLI. Pure `requests`. Refreshes the access + id token from the stored refresh token and calls the mobile API. Has endpoint wrappers + a telemetry flattener. `custom_components/cummins_connectcloud/api.py` is the HA-flavored port of this. | requests only |

Confirmed working: bootstrap logs in headlessly (first try, Shadow-DOM login form and all), runtime pulls the full telemetry set with no browser.

---

## 2. Auth architecture (the hard-won part — don't re-derive this)

The login is **AWS Cognito** federating via **SAML** to **Salesforce** (the user's credentials live in Salesforce, not Cognito). Key facts:

- **Cognito user pool:** `us-east-1_rUcTfwn3b`
- **Hosted UI domain:** `https://da-pcc-auth-production.auth.us-east-1.amazoncognito.com`
- **App client (public, PKCE, NO secret):** `28oqvfr332v3mp11u1tikqm565`
- **Redirect URI (locked):** `connectcloud://authentication/callback` — a custom scheme. `localhost` / other redirects are **rejected** by the client, so the RFC-8252 loopback trick is NOT available.
- **Scope:** `openid profile`
- **PKCE:** S256

### Why a browser is needed once (and only once)
Salesforce's login is an **Aura / Lightning** page: JS-rendered, stateful, with a per-release `fwuid` token and a multi-call Aura action sequence (`getWWIDLoginInstructions` → `getAppMappingDetails` → `getDoLogin`). Reproducing that in raw `requests` is brittle and breaks entirely on MFA. A real browser runs the JS and completes the SSO; we only automate typing credentials and intercepting the returned `code`. **Headless is fine** — we intercept the `connectcloud://…?code=…` redirect at the network layer (request URL + the 302 `Location` header, belt-and-braces), so Chromium never needs to actually *open* the custom scheme.

### The token exchange (runtime, pure requests)
`POST https://da-pcc-auth-production.auth.us-east-1.amazoncognito.com/oauth2/token`

- Bootstrap: `grant_type=authorization_code` + `code` + `code_verifier` + `client_id` + `redirect_uri` → returns `access_token`, `id_token`, `refresh_token` (`expires_in: 3600`).
- Runtime: `grant_type=refresh_token` + `client_id` + `refresh_token` → fresh `access_token` + `id_token`.

### ⚠️ The non-obvious API auth requirement
The mobile API needs **BOTH** tokens on every call:

- `Authorization: Bearer <access_token>`
- `mobile-data: <id_token>`   ← the ID token, carrying `sub`/identity

Sending only the bearer returns **401** ("Cannot read property 'sub' of null" — the same wall the HA forum hit years ago). The runtime client already sends both.

---

## 3. The mobile API

**Base:** `https://cc.aws.powercommandcloud.com/api/dashboard/v1/mobile`
**User-Agent seen from app:** `ConnectCloud_Maui/80 CFNetwork/3860.600.12 Darwin/25.5.0` (mirroring it is safest)

All GET → JSON unless noted:

| Endpoint | Returns |
|---|---|
| `/Profile` | Email + `Accounts[]` (AccountId, `CommandsEnabled`, AccountType) |
| `/Sites/Personal` | Your sites, each with `Assets[]` (generators) — this is where you get the **asset Id** |
| `/Sites/GetAssets?id=<siteId>` | Assets for a site incl. `LastTelemetry` |
| `/Assets/Detail?id=<assetId>` | **Live telemetry snapshot** (primary data source) |
| `/Assets/Events?id=<assetId>&from=<epoch_ms>` | Event/fault history (Severity, Code, Message, Timestamp, Acknowledged) |
| `/Assets/Commands?id=<assetId>` | Available commands + `IsEnabled` |

### Telemetry fields (`/Assets/Detail` → `LastTelemetry.Properties[]`, plus `LastCheckIn`)
Values come as strings; the client coerces to int/float.

| Field | Meaning / notes |
|---|---|
| `isRunning` | 0/1 — generator running |
| `utilityAvailable` | 0/1 — grid power present (combine w/ `isRunning` for "on generator") |
| `isExercising` | 0/1 — in a scheduled self-test |
| `isStandbyEnabled` | 0/1 |
| `isRemoteEnabled` | 0/1 — remote start/stop allowed |
| `batteryVoltage` | float (V) — the "will it start" health signal |
| `engineRuntime` | float (hours) — cumulative, good for maintenance |
| `faultType` | 0 = no fault |
| `gensetStatus` | int enum (needs decoding) |
| `loadStatus` | int enum (needs decoding) |
| `gensetPercentLoad` | int (%) |
| `averageEngineSpeed` | int (RPM) |
| `frequencyOP` | output frequency (Hz) |
| `gensetVoltage` | output voltage (V) |
| `powerSource` | int enum (needs decoding) |
| `SoftwareVersion` | firmware string |
| `LastCheckIn` | ISO timestamp — freshness / stale-data detection |

### Commands (available per `/Assets/Commands`)
`StartGenset`, `StopGenset`, `SetExerciseSchedule` (each with `IsEnabled`). The web app POSTs commands to `/assets/{id}/command/<name>`; the exact mobile command POST body/path should be re-captured before implementing control (we only have the command *list*, not a confirmed invocation). **Treat control as phase 2** — read-only first.

---

## 4. Known constraints / gotchas

- **Refresh-token lifetime is unknown.** Cognito default is 30 days but Cummins may have set otherwise. When it lapses, refresh returns `400 invalid_grant` → user must re-run the bootstrap. **Design HA to surface a `reauth` flow, not a silent failure.**
- **MFA is not supported** (by design, for now). Bootstrap assumes username/password only. If a user has MFA, the headless fill won't complete; a `--headed` mode (user clears MFA by hand) is the future fallback.
- **Bot-detection wildcard:** Salesforce *may* treat headless Chromium differently. If headed works but headless doesn't, run headed. (Unconfirmed; headless worked in testing.)
- **Login form is Shadow-DOM** (Lightning Web Components). Playwright pierces open shadow roots automatically (Selenium struggled here). Selectors are defensive with fallbacks + debug dump (`login_error.png` / `login_page.html`) on failure.
- **Auth `code` is single-use and short-lived** (~minutes). Bootstrap does the exchange immediately.
- **`connectcloud://` never appears in the address bar** (browser can't load it) — hence the network-layer interception. Manual copy-paste onboarding is NOT viable for end users; the automated bootstrap is the path.
- **Enum decoding TODO:** `gensetStatus`, `loadStatus`, `powerSource` are integer enums we haven't mapped. Cross-reference the app UI or `/Assets/Events` messages.

---

## 5. HACS integration plan

Target layout: `custom_components/cummins_connectcloud/`

```
custom_components/cummins_connectcloud/
├── __init__.py            # setup entry, create coordinator, forward platforms
├── manifest.json          # domain, name, requirements (playwright only for bootstrap? see note), iot_class=cloud_polling
├── api.py                 # port cummins_connectcloud.py: CognitoAuth + CumminsClient
├── config_flow.py         # user + reauth flows
├── coordinator.py         # DataUpdateCoordinator polling /Assets/Detail
├── sensor.py              # numeric telemetry entities
├── binary_sensor.py       # boolean telemetry entities
├── const.py               # DOMAIN, endpoints, field maps, enum maps
└── strings.json / translations/
```

### Config flow

**v0.1 (shipped):** paste-a-refresh-token flow — user runs `tools/bootstrap_login.py` off-box (Playwright), pastes the resulting refresh token into HA. Chosen because most users are on **Home Assistant OS**, which can't run a bundled Chromium at all (read-only appliance, no apt/package access) — ruling out in-container Playwright entirely, not just deprioritizing it.

**v0.2 (shipped):** `custom_components/cummins_connectcloud/aura_auth.py` reimplements the Salesforce/Cognito login as pure HTTP — no browser anywhere, works on every HA install type including HAOS. The config flow's first step is now a menu: **username/password** (default, calls `aura_auth.login()`) or **refresh token** (advanced, the old v0.1 paste-a-token flow, kept as a fallback per the risk noted below). Reauth also asks for username/password.

**v0.2.1 fix — TLS fingerprinting blocks plain `requests`.** The first live test failed at step 2 below (missing Aura bootstrap on the login page) with a real account, not a bad-password issue. Direct A/B testing nailed the cause: identical requests (same URL, same headers, same User-Agent, same cookies) get the correct Cummins-branded login page from a real browser (Chrome, or Playwright-driven Chromium) and from `curl_cffi` impersonating Chrome, but get served a generic, unbranded Salesforce template — missing the Aura app bootstrap entirely — from plain `requests`/urllib3. Since headers were byte-identical across all three, this isn't a header check; it's almost certainly Salesforce's edge (`server: sfdcedge`) fingerprinting the TLS ClientHello itself (JA3/JA4), which differs between a real browser's TLS stack and Python's OpenSSL-based one regardless of claimed headers. Fix: `aura_auth.py` uses `curl_cffi.requests` (libcurl + a Chrome-matching TLS fingerprint) instead of `requests`, and deliberately does NOT override its User-Agent — letting curl_cffi's impersonation profile set a fully internally-consistent header set matters more than any specific header value. `api.py` (the actual telemetry REST API, `cc.aws.powercommandcloud.com`) is untouched — that endpoint isn't behind the same Salesforce edge and plain `requests` works fine there.

This also incidentally surfaced the wrong-password error shape that was previously unconfirmed: `{"state": "ERROR", "error": [{"message": "Your login attempt has failed. Make sure the username and password are correct."}]}` — `_post_aura` now extracts that message directly instead of falling back to a generic failure string.

**Debugging workflow lesson:** don't test changes to this module by going through a full HACS-update → HA-restart → config-flow-click cycle. `tools/test_aura_login.py` runs the exact same `aura_auth.login()` Home Assistant would, standalone, in about a second — use it first every time.

How the login chain actually works, confirmed by capturing full traffic with mitmproxy (Playwright's own HAR/CDP body capture unreliably drops `Cache-Control: no-store` response bodies — several hops here use exactly that, so mitmproxy was necessary, not just convenient — see `tools/capture_via_mitm.py` / `tools/_mitm_aura_addon.py`):

1. GET the Cognito authorize URL (PKCE) → `requests` auto-follows the leading 302s → lands on a 401 from `/clw/idp/login` whose body is a **static** bounce page (either `var url = '...'` or a literal `window.location.replace('...')` — both forms appear at different hops; neither computes anything dynamically, both are plain regex-extractable).
2. Follow that to the actual (guest-session) login page. Its `fwuid`/`app`/`loaded`-map (the Aura "context" needed on every subsequent call) are embedded directly in the HTML inside a `<script src="/clw/s/sfsites/l/{...json...}/...">` tag — also just a regex away, not exposed any other way.
3. POST credentials as an Aura `ApexActionController/ACTION$execute` call to Cummins' own custom Apex bridge, `IAM_VisualforceToLightning.getDoLogin` (`fedID`/`password`/`startURL` as form fields inside a `message=` JSON blob, form-urlencoded). This is Cummins' own SSO glue, not a generic Salesforce Experience Cloud login — the class name and `WWID` (WorldWide ID) references are Cummins-internal identity concepts.
4. On success, the response's `returnValue.returnValue` is a **fully-formed `frontdoor.jsp?...&sid=...` URL**, constructed server-side by that same Apex bridge — no client-side session-ID handling needed at all.
5. GET that URL → another static JS bounce → back to `/clw/idp/login`, now authenticated (200 instead of 401) → its body is a standard auto-submitting SAML form (`RelayState` + `SAMLResponse` hidden inputs, HTML-entity-encoded attribute values). POST those two fields to Cognito's `/saml2/idpresponse` ourselves (`allow_redirects=False`) instead of running the page's `onload` JS.
6. Cognito responds 302 with `Location: connectcloud://authentication/callback?code=...` — extract `code`, verify `state`, exchange for tokens exactly as the existing PKCE flow already does.

Nothing in this chain executes JavaScript — every hop that looked like it might need a JS engine turned out to be a static, server-rendered redirect or form. Known risks/limits:
- **Requires `curl_cffi`, not just `requests`** (see the TLS-fingerprinting fix above) — one more compiled dependency than originally planned, though `curl_cffi` ships prebuilt wheels for the platforms HA runs on, so this shouldn't mean a build toolchain on the HA host.
- **Still no MFA support** — same limitation as the Playwright bootstrap; if a given account has MFA, this will just fail somewhere in the chain with an unhelpful error, not a clear "needs MFA" message.
- **`getWWIDLoginInstructions`/`getAppMappingDetails`** (called by the real browser flow before `getDoLogin`, seemingly just to render UI label text) are skipped — unconfirmed whether the server requires them first. First thing to check if `getDoLogin` starts failing while the browser-based bootstrap still works.
- If Cummins changes their login page in a way this can't follow, the v0.1 refresh-token fallback is still there — by design, so a breakage here doesn't mean bundling a browser into the integration.
- The stored secret is unchanged either way: only the **refresh token** ends up in the config entry. Username/password are used transiently to obtain it and are never persisted.

**A meta-lesson from building this:** every redaction pass on the capture data used to build this (`tools/redact_har.py`) missed something on the first attempt — a live Salesforce session ID hidden in a raw URL string, a plaintext password inside a form-urlencoded field nested in JSON, the same session ID again behind percent-encoding, and a full SAMLResponse assertion inside a raw HTML `<input>` tag never seen by the JSON/form-aware redaction paths. Each was a different encoding layer or body format the *previous* fix didn't anticipate. If capturing this login flow again (e.g. after a Cummins login-page change), don't assume `redact_har.py` catches everything on the first pass — verify by recursively walking the JSON and unquoting every string several layers deep before trusting a capture is safe to share.

### Coordinator
- `DataUpdateCoordinator`, poll interval ~60s (tune vs. `LastCheckIn` cadence; device telemetry looked ~event-driven, so 1–5 min is plenty and gentle on the API).
- On each cycle: ensure valid access token (refresh if expired), GET `/Assets/Detail?id=<assetId>`, flatten `Properties` → dict.
- Handle 401 → force refresh once → retry (client already does this).
- Multi-asset: iterate `/Sites/Personal`; create a device per asset.

### Entity mapping (read-only, phase 1)
Sensors:
- `batteryVoltage` → voltage (V), `state_class: measurement`
- `engineRuntime` → duration (h), `state_class: total_increasing`
- `gensetPercentLoad` → % , `gensetVoltage` → V, `frequencyOP` → Hz, `averageEngineSpeed` → RPM
- `SoftwareVersion` → diagnostic
- `LastCheckIn` → timestamp
- `gensetStatus` / `loadStatus` / `powerSource` → enum sensors (after decoding)

Binary sensors:
- `isRunning` → running
- `utilityAvailable` → power (grid present) — invert for "on generator power"
- `isExercising`, `isStandbyEnabled`, `isRemoteEnabled`
- `faultType != 0` → problem
- Data-freshness: `now() - LastCheckIn > 25h` → problem (borrow wareed1's template idea)

### Phase 2 (control — optional, gated on `CommandsEnabled` / `IsEnabled`)
- Switch or buttons for `StartGenset` / `StopGenset`.
- **Re-capture the mobile command POST first** (body shape). Guard heavily — these act on real equipment.

### manifest notes
- `"iot_class": "cloud_polling"`
- `"requirements": ["requests"]` (if off-box bootstrap) — Playwright stays out of the HA runtime.
- `"config_flow": true`, `"integration_type": "hub"` (site) or `"device"`.

---

## 6. Reference

- Prior art (Selenium + MQTT, current auth stack): `github.com/wareed1/Cummins-Generator-to-Home-Assistant` — reuse its `configuration.yaml` MQTT sensor + freshness/exercise template ideas even if going native.
- HA forum thread: `community.home-assistant.io/t/cummins-cloud-connect-generators/398442` (history: old Microsoft B2C stack → migrated to current Salesforce/Cognito).

## 7. Status / next steps

Done: `custom_components/cummins_connectcloud/` scaffolded per §5; `api.py` ported, coordinator + read-only sensor/binary_sensor entities wired up; repo has `hacs.json`, README, and a hassfest/HACS validation GitHub Action. Config flow supports both v0.2 (username/password via `aura_auth.py`, no browser) and v0.1 (paste a refresh token, fallback) — see §5 for how the login reimplementation works and its confirmed-vs-assumed pieces.

Remaining:
- **Not yet confirmed with a real successful live-account login.** `tools/test_aura_login.py` reaches `getDoLogin` correctly and returns the confirmed wrong-password error shape with bad credentials (proving the whole chain up through that point works against the live site with `curl_cffi`), but no one has yet run it with real, correct credentials all the way through to a working refresh token, nor exercised `async_step_credentials` inside actual Home Assistant. Top priority before relying on this.
- Decode the `gensetStatus` / `loadStatus` / `powerSource` enums (§4) and expose them as proper enum sensors instead of raw integers.
- Phase 2: re-capture the mobile command POST shape and add `StartGenset`/`StopGenset` controls, gated on `CommandsEnabled`/`IsEnabled`.
- Confirm whether skipping `getWWIDLoginInstructions`/`getAppMappingDetails` before `getDoLogin` is actually safe (see §5's known-risk list) — only matters if login starts failing.
