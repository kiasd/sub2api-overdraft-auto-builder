#!/usr/bin/env python3
"""Validate tracked builder assets without contacting upstream services."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = (
    re.compile(rb"ghp_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def validate_overlays() -> list[str]:
    errors: list[str] = []
    overlay_root = ROOT / "payload" / "ui"
    for manifest_path in overlay_root.glob("*/manifest.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            errors.append(f"{manifest_path}: files must be a non-empty list")
            continue
        seen: set[str] = set()
        for entry in files:
            if not isinstance(entry, dict):
                errors.append(f"{manifest_path}: file entry must be an object")
                continue
            value = str(entry.get("path", ""))
            relative = Path(value)
            if relative.is_absolute() or value in {"", "."} or any(
                part in {"", ".", ".."} for part in relative.parts
            ):
                errors.append(f"{manifest_path}: unsafe path {relative}")
                continue
            normalized = relative.as_posix()
            if normalized in seen:
                errors.append(f"{manifest_path}: duplicate path {relative}")
                continue
            seen.add(normalized)
            payload = manifest_path.parent / relative
            if not payload.is_file() or payload.is_symlink():
                errors.append(f"{manifest_path}: missing {relative}")
                continue
            expected = str(entry.get("sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                errors.append(f"{manifest_path}: invalid checksum for {relative}")
                continue
            actual = sha256_file(payload)
            if actual != expected:
                errors.append(f"{manifest_path}: checksum mismatch for {relative}")
    return errors


def validate_secrets() -> list[str]:
    errors: list[str] = []
    for path in tracked_files():
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            continue
        data = path.read_bytes()
        for pattern in SECRET_PATTERNS:
            if pattern.search(data):
                errors.append(f"possible secret in {path.relative_to(ROOT)}")
                break
    return errors


def main() -> int:
    errors = validate_overlays() + validate_secrets()
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("repository validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
