# Cummins Connect Cloud → Home Assistant Integration

**Status:** Working proof-of-concept (auth + data pull). Ready to build into a HACS custom integration.
**Goal:** A `custom_components/cummins_connectcloud/` integration that polls a Cummins home-standby generator's telemetry (and optionally issues start/stop/exercise commands) via the Cummins Connect Cloud **mobile** API — with no browser dependency at runtime.

---

## 1. What we have (working today)

Two Python files, both verified end-to-end against a live account:

| File | Role | Runtime deps |
|---|---|---|
| `bootstrap_login.py` | **One-time** login. Headless Playwright drives username/password login, catches the OAuth `code`, exchanges it, writes `~/.cummins_tokens.json`. | playwright, requests |
| `cummins_connectcloud.py` | **Runtime** client. Pure `requests`. Refreshes the access + id token from the stored refresh token and calls the mobile API. Has endpoint wrappers + a telemetry flattener. | requests only |

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
- **Initial setup:** ask for username + password → run the bootstrap logic → obtain refresh token → store **only the refresh token** (+ id/access) in the encrypted config entry. Password is not persisted.
  - **Design decision to resolve:** where does the browser run? HA OS is headless and shouldn't bundle Chromium. Options:
    1. **Off-box bootstrap (recommended):** ship `bootstrap_login.py` as a standalone tool the user runs on their laptop; they paste the resulting **refresh token** into the HA config flow. Keeps Chromium out of HA entirely. Cleanest for HACS.
    2. **In-container Playwright:** integration installs Playwright + Chromium on first setup. Heavy, fragile on HA OS/Alpine, larger attack surface. Avoid unless (1) proves too clunky.
  - Leaning (1): HA config flow field = "refresh token" (+ a link to the bootstrap tool/instructions). The integration itself stays pure `requests`.
- **Reauth flow:** on `invalid_grant`, HA prompts the user to supply a fresh refresh token (re-run bootstrap). Use HA's built-in `async_step_reauth`.

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

## 7. Immediate next step
Scaffold `custom_components/cummins_connectcloud/` per §5 with the config-flow decision = **off-box bootstrap, paste refresh token**. Port `api.py` from `cummins_connectcloud.py`, wire the coordinator + read-only entities, defer commands to phase 2.
