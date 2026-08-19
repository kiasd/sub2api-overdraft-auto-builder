#!/usr/bin/env python3
"""Record the last successfully published immutable input set."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("detection", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("--run-url", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    detection = load_json(args.detection)
    required = ("fingerprint", "release_tag", "official", "fork", "overlay")
    missing = [key for key in required if key not in detection]
    if missing:
        raise ValueError(f"detection snapshot is missing: {', '.join(missing)}")
    state = {
        "schema": 1,
        "last_success": {
            "fingerprint": detection["fingerprint"],
            "release_tag": detection["release_tag"],
            "release_version": detection["release_version"],
            "official": detection["official"],
            "fork": detection["fork"],
            "overlay": detection["overlay"],
            "published_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
            "run_url": args.run_url,
        },
    }
    write_json_atomic(args.state, state)
    print(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
