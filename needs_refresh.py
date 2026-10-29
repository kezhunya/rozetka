"""Decide whether the independent Rozetka workflow should rebuild the large feed."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def read_marker(path: Path) -> str:
    import csv
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "refresh_token" not in (reader.fieldnames or []):
            raise ValueError("Public control CSV lacks refresh_token")
        for row in reader:
            token = (row.get("refresh_token") or "").strip()
            if token:
                return token
    raise ValueError("Public control CSV contains no refresh_token")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controls-csv", type=Path, required=True)
    parser.add_argument("--state-json", type=Path, required=True)
    parser.add_argument("--decision-json", type=Path, required=True)
    parser.add_argument("--max-age-hours", type=float, default=3)
    args = parser.parse_args()

    token = read_marker(args.controls_csv)
    try:
        state = json.loads(args.state_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    try:
        checked_at = datetime.fromisoformat(
            (state.get("last_checked_at") or state["generated_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        checked_at = datetime.min.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    forced = (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
              and os.environ.get("ROZETKA_FORCE_REFRESH", "true").lower() == "true")
    marker_changed = token != state.get("refresh_token")
    expired = now - checked_at >= timedelta(hours=args.max_age_hours)
    full_build = forced or marker_changed or expired
    decision = {
        "run": full_build,
        "reason": "manual" if forced else "new_request" if marker_changed
                  else "age_check" if expired else "unchanged",
        "refresh_token": token,
        "checked_at": now.isoformat(),
    }
    args.decision_json.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as stream:
            stream.write("run=" + str(decision["run"]).lower() + "\n")
    print(json.dumps(decision, ensure_ascii=False))


if __name__ == "__main__":
    main()
