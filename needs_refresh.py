"""Decide whether the independent Rozetka workflow should rebuild the large feed."""

from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def read_token(path: Path) -> str:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if "refresh_token" not in (reader.fieldnames or []):
            raise ValueError("Public control CSV lacks refresh_token")
        first = next(reader, None)
        if not first:
            raise ValueError("Public control CSV contains no products")
        return first["refresh_token"].strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controls-csv", type=Path, required=True)
    parser.add_argument("--state-json", type=Path, required=True)
    parser.add_argument("--decision-json", type=Path, required=True)
    parser.add_argument("--max-age-hours", type=float, default=3)
    args = parser.parse_args()

    token = read_token(args.controls_csv)
    try:
        state = json.loads(args.state_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    try:
        generated_at = datetime.fromisoformat(state["generated_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        generated_at = datetime.min.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    forced = (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
              and os.environ.get("ROZETKA_FORCE_REFRESH", "true").lower() == "true")
    marker_changed = token != state.get("refresh_token")
    expired = now - generated_at >= timedelta(hours=args.max_age_hours)
    decision = {
        "run": forced or marker_changed or expired,
        "reason": "manual" if forced else "marker" if marker_changed else "age" if expired else "unchanged",
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
