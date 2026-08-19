#!/usr/bin/env python3
"""Systemd entrypoint that monitors and stages verified GitHub Releases."""

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
            timeout=int(os.environ.get("SUB2API_RELEASE_MONITOR_TIMEOUT", "900")),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"Release monitor failed: {exc}", file=sys.stderr, flush=True)
        return 1
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
