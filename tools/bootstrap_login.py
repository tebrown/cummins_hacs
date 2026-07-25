#!/usr/bin/env python3
"""
Cummins Connect Cloud — one-time login bootstrap (username/password, no MFA).

Runs a browser (Playwright) ONCE, logs in with the given credentials, catches
the connectcloud://authentication/callback?code=... redirect, exchanges the
code (PKCE) for tokens, and writes ~/.cummins_tokens.json — the same cache the
runtime client (cummins_connectcloud.py) reads. After this you never need a
browser again; the runtime is pure `requests` refreshing the token.

WHY A BROWSER AT ALL: the login is Salesforce Aura (JS-rendered, stateful,
fwuid-versioned) federated into Cognito via SAML — not reliably replayable in
raw requests. A real browser runs the page's JS and completes the SSO; we only
automate typing the credentials and grabbing the returned code.

HEADLESS is fine here because we DON'T need Chromium to *open* the custom
connectcloud:// scheme — we intercept that navigation at the network layer and
read the code off the URL. (Headed is only needed when a *human* must clear MFA,
which we're not supporting yet.)

SETUP (once, on whatever machine runs this — your laptop, not the HA box):
    pip install playwright requests
    playwright install chromium

USAGE:
    export CUMMINS_USERNAME='you@example.com'
    export CUMMINS_PASSWORD='...'
    python bootstrap_login.py
  or:
    python bootstrap_login.py --username you@example.com --password ...
  add --headed to WATCH it (useful the first time / for debugging selectors).

NOTE: This will break if the user has MFA enabled (by design, for now) or if
Cummins changes their login page. On any failure it saves login_error.png and
login_page.html next to this script so you can see what the page looked like.
"""

from __future__ import annotations
import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.parse
from pathlib import Path

import requests

# --- Constants (must match the mobile app's Cognito client) -----------------
COGNITO_DOMAIN = "https://da-pcc-auth-production.auth.us-east-1.amazoncognito.com"
AUTHORIZE_URL  = f"{COGNITO_DOMAIN}/oauth2/authorize"
TOKEN_URL      = f"{COGNITO_DOMAIN}/oauth2/token"
CLIENT_ID      = "28oqvfr332v3mp11u1tikqm565"
REDIRECT_URI   = "connectcloud://authentication/callback"
SCOPE          = "openid profile"
USER_AGENT     = "ConnectCloud_Maui/80 CFNetwork/3860.600.12 Darwin/25.5.0"
TOKEN_CACHE    = Path.home() / ".cummins_tokens.json"
HERE           = Path(__file__).resolve().parent

# Timeouts (ms)
NAV_TIMEOUT     = 45_000     # page loads / SSO hops can be slow
FIELD_TIMEOUT   = 20_000     # waiting for a login field to appear
CALLBACK_TIMEOUT = 60        # seconds to wait for the connectcloud:// redirect


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _authorize_url(challenge: str, state: str) -> str:
    q = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(q)}"


def _fill_first(page, candidates: list, value: str, what: str) -> bool:
    """Try each locator candidate; fill the first visible one. Returns success.

    Playwright locators pierce OPEN shadow DOM automatically, which matters here
    because the Cummins login inputs live inside Lightning Web Components.
    """
    for desc, locator in candidates:
        try:
            loc = locator.first
            loc.wait_for(state="visible", timeout=3000)
            loc.fill(value)
            print(f"  filled {what} via {desc}")
            return True
        except Exception:
            continue
    return False


def _click_first(page, candidates: list, what: str) -> bool:
    for desc, locator in candidates:
        try:
            loc = locator.first
            loc.wait_for(state="visible", timeout=3000)
            loc.click()
            print(f"  clicked {what} via {desc}")
            return True
        except Exception:
            continue
    return False


def _dump_debug(page, tag: str) -> None:
    try:
        page.screenshot(path=str(HERE / "login_error.png"), full_page=True)
        (HERE / "login_page.html").write_text(page.content())
        print(f"  [debug] saved login_error.png and login_page.html (stage: {tag})")
        print(f"  [debug] current URL: {page.url}")
    except Exception as e:
        print(f"  [debug] could not dump page: {e}")


def bootstrap(username: str, password: str, headed: bool = False) -> None:
    # Import here so `--help` works without playwright installed.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("Playwright not installed. Run:\n"
                 "  pip install playwright\n  playwright install chromium")

    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(32)
    auth_url = _authorize_url(challenge, state)

    captured: dict[str, str] = {}

    def _maybe_capture(url: str) -> None:
        if url.startswith("connectcloud://") and "url" not in captured:
            captured["url"] = url

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        page.set_default_navigation_timeout(NAV_TIMEOUT)

        # --- Intercept the connectcloud:// redirect two ways (belt & braces) ---
        # 1) as a request the browser tries to make for the custom scheme
        page.on("request", lambda r: _maybe_capture(r.url))
        # 2) as the Location header of the 3xx that points at it (fires earlier,
        #    before Chromium's external-protocol handling can swallow it)
        def _on_response(resp):
            try:
                loc = resp.headers.get("location", "")
                _maybe_capture(loc)
            except Exception:
                pass
        page.on("response", _on_response)
        # dismiss any "open external app?" dialog so it can't block us
        page.on("dialog", lambda d: d.dismiss())

        print("Opening login page ...")
        try:
            page.goto(auth_url, wait_until="domcontentloaded")
        except Exception:
            # A goto to a page that ends up redirecting to connectcloud:// can
            # itself raise — that's fine if we captured the code.
            pass

        if "url" not in captured:
            print("Filling credentials ...")
            # Wait for *some* login field to render first.
            email_candidates = [
                ("label email/username", page.get_by_label(re.compile(r"e-?mail|user\s*name|username", re.I))),
                ("input[type=email]", page.locator('input[type="email"]')),
                ("autocomplete=username", page.locator('input[autocomplete="username"]')),
                ("name~=username", page.locator('input[name*="username" i]')),
                ("name~=email", page.locator('input[name*="email" i]')),
                ("id~=username", page.locator('input[id*="username" i]')),
            ]
            pwd_candidates = [
                ("input[type=password]", page.locator('input[type="password"]')),
                ("label password", page.get_by_label(re.compile(r"password", re.I))),
                ("autocomplete=current-password", page.locator('input[autocomplete="current-password"]')),
            ]
            submit_candidates = [
                ("role=button login", page.get_by_role("button", name=re.compile(r"log\s*in|sign\s*in|login|continue|next", re.I))),
                ("button[type=submit]", page.locator('button[type="submit"]')),
                ("input[type=submit]", page.locator('input[type="submit"]')),
                ("text Log In", page.get_by_text(re.compile(r"^\s*(log\s*in|sign\s*in)\s*$", re.I))),
            ]

            try:
                # give the JS-rendered form time to appear
                page.wait_for_selector(
                    'input[type="email"], input[type="password"], input[autocomplete="username"]',
                    timeout=FIELD_TIMEOUT,
                )
            except Exception:
                _dump_debug(page, "waiting-for-login-form")
                browser.close()
                sys.exit("Could not find the login form. See login_page.html to "
                         "adjust selectors (the fields may be inside a shadow root "
                         "with unusual names).")

            if not _fill_first(page, email_candidates, username, "username"):
                _dump_debug(page, "fill-username")
                browser.close()
                sys.exit("Could not fill the username field — see debug dump.")

            if not _fill_first(page, pwd_candidates, password, "password"):
                # Some Salesforce flows are two-step: email, click next, THEN
                # password appears. Try clicking a 'next' then re-find password.
                _click_first(page, submit_candidates, "next (two-step?)")
                page.wait_for_timeout(1500)
                if not _fill_first(page, pwd_candidates, password, "password"):
                    _dump_debug(page, "fill-password")
                    browser.close()
                    sys.exit("Could not fill the password field — see debug dump.")

            if not _click_first(page, submit_candidates, "submit"):
                _dump_debug(page, "click-submit")
                browser.close()
                sys.exit("Could not find the submit button — see debug dump.")

        # --- Wait for the connectcloud:// callback to be captured -------------
        print("Waiting for login to complete ...")
        deadline = time.time() + CALLBACK_TIMEOUT
        while time.time() < deadline and "url" not in captured:
            page.wait_for_timeout(250)   # pumps event handlers

        if "url" not in captured:
            _dump_debug(page, "no-callback")
            browser.close()
            sys.exit("Never saw the connectcloud:// callback. Likely causes: "
                     "wrong credentials, MFA is enabled on this account (not "
                     "supported yet), or a login-page change. See debug dump.")

        browser.close()

    # --- Parse the code, verify state, exchange for tokens ------------------
    cb = urllib.parse.urlparse(captured["url"])
    q = urllib.parse.parse_qs(cb.query)
    code = q.get("code", [""])[0]
    returned_state = q.get("state", [None])[0]
    if returned_state and returned_state != state:
        sys.exit("State mismatch — aborting (possible cross-session confusion).")
    if not code:
        sys.exit(f"No code in callback URL: {captured['url']}")

    print("Exchanging authorization code for tokens ...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }, headers={"content-type": "application/x-www-form-urlencoded; charset=utf-8",
                "user-agent": USER_AGENT}, timeout=30)
    if resp.status_code != 200:
        sys.exit(f"Token exchange failed ({resp.status_code}): {resp.text[:300]}")
    tok = resp.json()

    TOKEN_CACHE.write_text(json.dumps({
        "refresh_token": tok["refresh_token"],
        "access_token": tok.get("access_token"),
        "id_token": tok.get("id_token"),
        "access_expiry": time.time() + int(tok.get("expires_in", 3600)) - 60,
    }))
    os.chmod(TOKEN_CACHE, 0o600)
    print(f"\n\u2713 Success. Tokens saved to {TOKEN_CACHE}")
    print("  You're bootstrapped — the runtime client is now browser-free.")
    print("  Verify with:  python tools/cummins_connectcloud.py test")
    print("  Then paste this refresh_token into the Home Assistant config flow.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Cummins Connect Cloud login bootstrap")
    ap.add_argument("--username", default=os.environ.get("CUMMINS_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("CUMMINS_PASSWORD"))
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window (debug / watch it work)")
    args = ap.parse_args()

    if not args.username or not args.password:
        ap.error("provide --username/--password or set "
                 "CUMMINS_USERNAME / CUMMINS_PASSWORD env vars")

    bootstrap(args.username, args.password, headed=args.headed)


if __name__ == "__main__":
    main()
