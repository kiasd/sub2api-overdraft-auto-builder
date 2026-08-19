#!/usr/bin/env python3
"""Privileged systemd entrypoint for the native automatic update worker."""

from __future__ import annotations

import os
import subprocess
import sys


CONTROL = os.environ.get("SUB2API_PLUGIN_CONTROL", "/usr/local/sbin/sub2api-plugin-control")


def main() -> int:
    try:
        result = subprocess.run(
            [CONTROL, "auto-run"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=int(os.environ.get("SUB2API_AUTO_UPDATE_TIMEOUT", "10800")),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"automatic update worker failed: {exc}", file=sys.stderr, flush=True)
        return 1
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
