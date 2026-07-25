#!/usr/bin/env python3
"""
Log raw generator telemetry over time to decode the gensetStatus/loadStatus/
powerSource enums (see docs/DESIGN.md §4 — Cummins doesn't publish these).

The approach: poll /Assets/Detail periodically and log every field, then
correlate the unknown enum values against fields we already understand
(isRunning, utilityAvailable, isExercising, faultType). Whatever gensetStatus
value co-occurs with isRunning=1 is "running", whatever co-occurs with
isExercising=1 is "exercising", etc. This needs data across a state
transition to be useful — a scheduled self-exercise, an actual utility
outage, or a manual start/stop via the ConnectCloud app all work. The
longer this runs and the more transitions it catches, the more complete
the mapping gets.

USAGE:
    # collect (leave running in the background; Ctrl+C to stop):
    export CUMMINS_REFRESH_TOKEN='...'   # or rely on ~/.cummins_tokens.json
    python tools/monitor_telemetry.py --out telemetry_log.jsonl

    # once you've got some data spanning a state change, see what's known:
    python tools/monitor_telemetry.py --analyze telemetry_log.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cummins_connectcloud import CumminsAuthError, CumminsConnectCloud  # noqa: E402

DEFAULT_INTERVAL = 300  # 5 min — gentle, matches the HA coordinator's cadence


def collect(out_path: Path, interval: int) -> None:
    client = CumminsConnectCloud(refresh_token=os.environ.get("CUMMINS_REFRESH_TOKEN"))
    asset_id = client.first_asset_id()
    print(f"Logging asset {asset_id} to {out_path} every {interval}s. Ctrl+C to stop.")
    print("Trigger a manual start/stop/exercise via the app while this runs "
          "to catch a state transition faster.")

    last_signature = None
    with open(out_path, "a", encoding="utf-8") as f:
        while True:
            try:
                detail = client.asset_detail(asset_id)
                telemetry = client.telemetry(detail)
            except CumminsAuthError as e:
                sys.exit(f"Auth failed: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] poll failed: {e}")
                time.sleep(interval)
                continue

            row = {"polled_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **telemetry}
            f.write(json.dumps(row) + "\n")
            f.flush()

            # Only print a line when something interesting changes, so a
            # long-running session in a terminal doesn't scroll uselessly.
            signature = (
                telemetry.get("isRunning"),
                telemetry.get("utilityAvailable"),
                telemetry.get("isExercising"),
                telemetry.get("gensetStatus"),
                telemetry.get("loadStatus"),
                telemetry.get("powerSource"),
            )
            if signature != last_signature:
                print(f"{row['polled_at']}  isRunning={telemetry.get('isRunning')} "
                      f"utilityAvailable={telemetry.get('utilityAvailable')} "
                      f"isExercising={telemetry.get('isExercising')}  |  "
                      f"gensetStatus={telemetry.get('gensetStatus')} "
                      f"loadStatus={telemetry.get('loadStatus')} "
                      f"powerSource={telemetry.get('powerSource')}")
                last_signature = signature

            time.sleep(interval)


def analyze(log_path: Path) -> None:
    # {enum_field: {enum_value: {(isRunning, utilityAvailable, isExercising, faultType): count}}}
    ENUM_FIELDS = ("gensetStatus", "loadStatus", "powerSource")
    KNOWN_FIELDS = ("isRunning", "utilityAvailable", "isExercising", "faultType")

    correlations: dict[str, dict] = {f: {} for f in ENUM_FIELDS}
    rows = 0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            rows += 1
            known = tuple(row.get(k) for k in KNOWN_FIELDS)
            for field in ENUM_FIELDS:
                value = row.get(field)
                if value is None:
                    continue
                bucket = correlations[field].setdefault(value, {})
                bucket[known] = bucket.get(known, 0) + 1

    print(f"{rows} rows in {log_path}.\n")
    for field in ENUM_FIELDS:
        values = correlations[field]
        if not values:
            print(f"{field}: no data\n")
            continue
        print(f"{field}:")
        for value, contexts in sorted(values.items()):
            print(f"  {value!r} seen with (isRunning, utilityAvailable, isExercising, faultType) =")
            for known, count in sorted(contexts.items(), key=lambda kv: -kv[1]):
                print(f"    {known}  x{count}")
        print()

    print("Read this as: a gensetStatus value that ONLY ever appears with "
          "isRunning=1 is very likely a 'running' state; one that only "
          "appears with isExercising=1 is likely 'exercising'; etc. Values "
          "seen under multiple contexts need more data (more transitions) "
          "to pin down, or represent sub-states within a context (e.g. "
          "'starting' vs 'running' — both isRunning=1) this simple "
          "correlation can't distinguish on its own.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="telemetry_log.jsonl", help="log file to append to")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                     help=f"seconds between polls (default {DEFAULT_INTERVAL})")
    ap.add_argument("--analyze", metavar="LOGFILE",
                     help="don't collect — analyze an existing log file instead")
    args = ap.parse_args()

    if args.analyze:
        analyze(Path(args.analyze))
    else:
        try:
            collect(Path(args.out), args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()
