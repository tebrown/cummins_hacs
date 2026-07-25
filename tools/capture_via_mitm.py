#!/usr/bin/env python3
"""
Capture the Aura login sequence via mitmproxy, not Playwright's HAR/CDP body
capture — see _mitm_aura_addon.py for why (short version: CDP can't reliably
read Cache-Control: no-store response bodies, and the login POST is one).

Starts mitmdump as a local HTTPS-intercepting proxy, drives the same headless
login as bootstrap_login.py through it, and writes every Aura/token call's
full request+response body to a JSON file.

SETUP (same venv as bootstrap_login.py, plus mitmproxy — already present in
this repo's .venv):
    pip install mitmproxy

USAGE:
    export CUMMINS_USERNAME='you@example.com'
    export CUMMINS_PASSWORD='...'
    python tools/capture_via_mitm.py --out login_capture.mitm.json

Redact before sharing:
    python tools/redact_har.py login_capture.mitm.json login_capture.mitm.redacted.json
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADDON = HERE / "_mitm_aura_addon.py"
MITM_STARTUP_WAIT = 1.5  # seconds; time for mitmdump to bind before Chromium connects


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    sys.path.insert(0, str(HERE))  # so `import bootstrap_login` finds it regardless of cwd
    try:
        import bootstrap_login
    except ImportError as e:
        sys.exit(f"Could not import bootstrap_login.py from {HERE}: {e}")

    ap = argparse.ArgumentParser(description="Capture the Aura login via mitmproxy")
    ap.add_argument("--username", default=os.environ.get("CUMMINS_USERNAME"))
    ap.add_argument("--password", default=os.environ.get("CUMMINS_PASSWORD"))
    ap.add_argument("--headed", action="store_true",
                     help="show the browser window (debug / watch it work)")
    ap.add_argument("--out", default="login_capture.mitm.json",
                     help="output JSON file (default: login_capture.mitm.json)")
    args = ap.parse_args()

    if not args.username or not args.password:
        ap.error("provide --username/--password or set "
                 "CUMMINS_USERNAME / CUMMINS_PASSWORD env vars")

    port = _free_port()
    out_path = Path(args.out).resolve()

    print(f"Starting mitmdump on 127.0.0.1:{port} ...")
    mitm = subprocess.Popen(
        [
            "mitmdump",
            "-s", str(ADDON),
            "--set", f"aura_out={out_path}",
            "-p", str(port),
            "-q",
        ],
    )
    time.sleep(MITM_STARTUP_WAIT)
    if mitm.poll() is not None:
        sys.exit(f"mitmdump exited immediately (code {mitm.returncode}) — "
                 "is it installed? `pip install mitmproxy`")

    try:
        bootstrap_login.bootstrap(
            args.username,
            args.password,
            headed=args.headed,
            proxy={"server": f"http://127.0.0.1:{port}"},
        )
    finally:
        mitm.terminate()
        try:
            mitm.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mitm.kill()

    if out_path.exists():
        print(f"\nWrote {out_path}")
        print("Redact before sharing:")
        print(f"  python tools/redact_har.py {out_path} {out_path.with_suffix('.redacted.json')}")
    else:
        print(f"\n{out_path} was never written — check mitmdump output above "
              "for errors, or that traffic actually matched the addon's filter.")


if __name__ == "__main__":
    main()
