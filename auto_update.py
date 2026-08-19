#!/usr/bin/env python3
"""Compatibility wrapper for the verified Release monitor."""

from __future__ import annotations

from release_monitor import main


if __name__ == "__main__":
    raise SystemExit(main())
