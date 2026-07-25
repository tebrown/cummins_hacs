# Cummins Connect Cloud for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/tebrown/cummins_hacs/actions/workflows/validate.yml/badge.svg)](https://github.com/tebrown/cummins_hacs/actions/workflows/validate.yml)

A Home Assistant custom integration that polls a Cummins home-standby
generator's telemetry (battery voltage, running state, load, faults, etc.)
through the **Cummins Connect Cloud mobile API** — the same backend the
ConnectCloud phone app uses. No browser or headless Chromium runs on your
Home Assistant instance; a one-time login happens on your own laptop instead.

Read-only in this release: sensors and binary sensors only. Start/Stop/Exercise
commands are a planned phase 2 — see [`docs/DESIGN.md`](docs/DESIGN.md).

## Why a separate login step?

The Cummins login is Salesforce SSO federated through AWS Cognito — a
JS-rendered, stateful page that isn't practical (or safe) to reproduce with a
bundled headless browser inside Home Assistant. Instead:

1. You run a small script **once, on your own computer**, which drives a
   real (headless) browser through the login and captures a Cognito
   **refresh token**.
2. You paste that refresh token into Home Assistant's config flow.
3. From then on, the integration talks to Cummins with plain HTTPS calls —
   refreshing the access token as needed. No browser, no stored password.

Full write-up of the auth mechanics: [`docs/DESIGN.md`](docs/DESIGN.md).

## Installation

### 1. Install the integration via HACS

This repository isn't in the HACS default store yet, so add it as a
**custom repository**:

1. HACS → the "⋮" menu (top right) → **Custom repositories**.
2. Repository: `https://github.com/tebrown/cummins_hacs`, Category: **Integration**.
3. Find **Cummins Connect Cloud** in HACS and install it.
4. Restart Home Assistant.

### 2. Get a refresh token (once, on your own computer — not the HA box)

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

This logs in headlessly, catches the OAuth callback, exchanges it for tokens,
and prints/saves a **refresh token**. Copy it — that's the only thing you
paste into Home Assistant.

> MFA is not supported by the bootstrap script yet. If your account has MFA,
> add `--headed` and clear the MFA prompt by hand when the browser opens.

### 3. Add the integration in Home Assistant

Settings → Devices & Services → **Add Integration** → **Cummins Connect
Cloud** → paste the refresh token. If your account has more than one
generator, you'll be asked which one to add (repeat setup to add others).

### Re-authenticating later

Refresh tokens don't last forever. When yours expires, Home Assistant will
surface a **Reauthenticate** notification for this integration — re-run
`tools/bootstrap_login.py` and paste the new token in.

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
custom_components/cummins_connectcloud/   the Home Assistant integration (HACS installs this)
tools/bootstrap_login.py                  one-time, off-box login (Playwright) -> refresh token
tools/cummins_connectcloud.py             standalone reference client / CLI (same API as the integration)
docs/DESIGN.md                            how the auth was reverse-engineered, API notes, roadmap
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
