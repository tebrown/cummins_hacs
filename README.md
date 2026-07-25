# Cummins Connect Cloud for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/tebrown/cummins_hacs/actions/workflows/validate.yml/badge.svg)](https://github.com/tebrown/cummins_hacs/actions/workflows/validate.yml)

A Home Assistant custom integration that polls a Cummins home-standby
generator's telemetry (battery voltage, running state, load, faults, etc.)
through the **Cummins Connect Cloud mobile API** — the same backend the
ConnectCloud phone app uses. No browser or headless Chromium runs on your
Home Assistant instance, even during setup: you sign in with your Cummins
username and password right in the Home Assistant UI.

Read-only in this release: sensors and binary sensors only. Start/Stop/Exercise
commands are a planned phase 2 — see [`docs/DESIGN.md`](docs/DESIGN.md).

## How sign-in works (no browser, anywhere)

The Cummins login is Salesforce SSO federated through AWS Cognito. Rather
than driving a real browser through that (which wouldn't even be possible on
Home Assistant OS — it's a read-only appliance with no way to run Chromium),
this integration talks to the same login endpoints directly over HTTPS: it
submits your username/password to Cummins' identity service, follows the
SAML handshake back to Cognito, and stores only the resulting session
(refresh) token — never your password.

This was reverse-engineered by capturing the real login traffic and
confirming every step is a static, parseable HTTP exchange (no JavaScript
execution required) — see [`docs/DESIGN.md`](docs/DESIGN.md) for the
full write-up, including the known fragility: if Cummins changes their login
page, this can break. If it does, there's a fallback (below).

## Installation

### 1. Install via HACS

This repository isn't in the HACS default store yet, so add it as a
**custom repository**:

1. HACS → the "⋮" menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/tebrown/cummins_hacs`, Category: **Integration**.
3. Find **Cummins Connect Cloud** in HACS and install it.
4. Restart Home Assistant.

### 2. Add the integration

Settings → Devices & Services → **Add Integration** → **Cummins Connect
Cloud** → **Username and password** → enter your Cummins Connect Cloud
credentials. If your account has more than one generator, you'll be asked
which one to add (repeat setup to add others).

> MFA is not supported — if your account has it enabled, sign-in will fail.

### Fallback: refresh token (if username/password sign-in doesn't work)

If Cummins changes their login page and the built-in sign-in breaks before
this integration is updated, there's a manual fallback: run a one-time,
off-box login script that drives a real (headless) browser through the login
and produces a refresh token, then paste that into the config flow's
**Refresh token (advanced)** option instead.

```bash
git clone https://github.com/tebrown/cummins_hacs
cd cummins_hacs
python3 -m venv .venv && source .venv/bin/activate
pip install -r tools/requirements.txt
playwright install chromium

export CUMMINS_USERNAME='you@example.com'
export CUMMINS_PASSWORD='...'
python tools/bootstrap_login.py
```

This prints/saves a **refresh token** — paste that into Home Assistant.

### Re-authenticating later

Sessions don't last forever. When yours expires, Home Assistant will surface
a **Reauthenticate** notification for this integration — just sign in again
with your username and password.

## Entities

**Sensors:** battery voltage, engine runtime, load %, output voltage, output
frequency, engine speed, firmware version, last check-in (timestamp).

**Binary sensors:** running, utility power available, exercising, standby
enabled, remote control enabled, fault (problem), data stale (no check-in in
25h — a proxy for "generator looks offline").

Enum fields (`gensetStatus`, `loadStatus`, `powerSource`) are exposed raw as
integers for now; their code→label mapping hasn't been fully reverse-engineered
yet (contributions welcome — see `docs/DESIGN.md` §4).

## Repository layout

```
custom_components/cummins_connectcloud/          the Home Assistant integration (HACS installs this)
custom_components/cummins_connectcloud/aura_auth.py  pure-HTTP username/password login (no browser)
tools/bootstrap_login.py                         fallback: off-box login (Playwright) -> refresh token
tools/cummins_connectcloud.py                    standalone reference client / CLI (same API as the integration)
tools/capture_via_mitm.py, tools/redact_har.py   dev tools used to reverse-engineer the login flow
docs/DESIGN.md                                   how the auth was reverse-engineered, API notes, roadmap
```

## Credit / prior art

- [wareed1/Cummins-Generator-to-Home-Assistant](https://github.com/wareed1/Cummins-Generator-to-Home-Assistant) —
  Selenium + MQTT bridge against an earlier (Microsoft B2C) auth stack;
  several entity/freshness ideas here are borrowed from it.
- [Home Assistant community thread](https://community.home-assistant.io/t/cummins-cloud-connect-generators/398442)
  documenting the migration history of Cummins's auth backend.

## Disclaimer

This is an unofficial, community-built integration reverse-engineered from
the ConnectCloud mobile app's network traffic. It is not affiliated with or
endorsed by Cummins Inc. Use at your own risk — this project has no ability
to prevent Cummins from changing or restricting the API it relies on.
