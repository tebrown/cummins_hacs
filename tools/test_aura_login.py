#!/usr/bin/env python3
"""
Standalone test harness for aura_auth.py — the pure-HTTP username/password
login used by the config flow. Runs the exact same code Home Assistant
would, but from your laptop against a live account in a few seconds,
without a HACS update / HA restart / config-flow click cycle in between.

USAGE:
    export CUMMINS_USERNAME='you@example.com'
    export CUMMINS_PASSWORD='...'
    python tools/test_aura_login.py

Add -v for verbose logging (every request aura_auth.py makes internally
isn't logged today, but this flag is here for when diagnostics get added).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# aura_auth.py normally does `from .const import ...` (package-relative,
# for Home Assistant). Its import has a fallback to a bare `from const
# import ...` specifically so this script can load both modules as
# top-level modules by putting their directory on sys.path, no Home
# Assistant or package machinery required.
INTEGRATION_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "cummins_connectcloud"
sys.path.insert(0, str(INTEGRATION_DIR))

import aura_auth  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Test the pure-HTTP Cummins login")
    ap.add_argument("--username", default=os.environ.get("CUMMINS_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("CUMMINS_PASSWORD"))
    args = ap.parse_args()

    if not args.username or not args.password:
        ap.error("provide --username/--password or set "
                 "CUMMINS_USERNAME / CUMMINS_PASSWORD env vars")

    print("Logging in ...")
    try:
        tokens = aura_auth.login(args.username, args.password)
    except aura_auth.AuraLoginError as e:
        sys.exit(f"\nLogin failed: {e}")

    print("\n✓ Login succeeded.")
    print(f"  access_token:  {tokens['access_token'][:24]}...")
    print(f"  refresh_token: {tokens['refresh_token'][:24]}...")
    print(f"  expires_in:    {tokens.get('expires_in')}s")


if __name__ == "__main__":
    main()
