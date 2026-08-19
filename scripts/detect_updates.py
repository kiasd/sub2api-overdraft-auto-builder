#!/usr/bin/env python3
"""Resolve immutable upstream inputs and decide whether a fusion build is needed."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


OFFICIAL_REPOSITORY = os.environ.get("SUB2API_OFFICIAL_REPOSITORY", "Wei-Shaw/sub2api")
FORK_REPOSITORY = os.environ.get("SUB2API_FORK_REPOSITORY", "DeanZFC/sub2api-overdraft")
FORK_BRANCH = os.environ.get("SUB2API_FORK_BRANCH", "codex-overdraft")
API_ROOT = "https://api.github.com"
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
FORK_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)-overdraft\.(\d+)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class DetectionError(RuntimeError):
    pass


def version_key(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise DetectionError(f"invalid official version: {value!r}")
    return tuple(int(part) for part in match.group(1).split("."))


def normalize_version(value: str) -> str:
    match = VERSION_RE.fullmatch(value.strip())
    if not match:
        raise DetectionError(f"invalid official version: {value!r}")
    return match.group(1)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class GitHubClient:
    def __init__(self, token: str = "") -> None:
        self.token = token.strip()

    def get(self, path: str) -> Any:
        if not path.startswith("/repos/"):
            raise DetectionError(f"refusing unexpected GitHub API path: {path}")
        request = urllib.request.Request(
            API_ROOT + path,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "sub2api-overdraft-auto-builder/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response)
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise DetectionError(f"GitHub API request failed for {path}: {exc}") from exc


def resolve_overlay(root: Path, official_version: str) -> dict[str, Any]:
    target_key = version_key(official_version)
    candidates: list[tuple[tuple[int, int, int], Path]] = []
    if root.is_dir():
        for directory in root.iterdir():
            if not directory.is_dir():
                continue
            try:
                key = version_key(directory.name)
            except DetectionError:
                continue
            if key <= target_key and (directory / "manifest.json").is_file():
                candidates.append((key, directory))
    if not candidates:
        return {"available": False, "mode": "missing", "target_version": official_version}
    _, selected = max(candidates, key=lambda item: item[0])
    manifest = selected / "manifest.json"
    return {
        "available": True,
        "mode": "exact" if selected.name == official_version else "forward-replay",
        "source_version": selected.name,
        "target_version": official_version,
        "manifest_sha256": sha256_file(manifest),
    }


def read_fork_version(client: GitHubClient, commit: str) -> str:
    payload = client.get(
        f"/repos/{FORK_REPOSITORY}/contents/FORK_VERSION?ref={urllib.parse.quote(commit, safe='')}"
    )
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        raise DetectionError("Fork returned an unsupported FORK_VERSION payload")
    try:
        encoded = "".join(str(payload.get("content", "")).split())
        value = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise DetectionError("Fork returned an invalid FORK_VERSION payload") from exc
    if not FORK_VERSION_RE.fullmatch(value):
        raise DetectionError(f"invalid Fork version: {value!r}")
    return value


def resolve_snapshot(repository_root: Path, client: GitHubClient) -> dict[str, Any]:
    release = client.get(f"/repos/{OFFICIAL_REPOSITORY}/releases/latest")
    if not isinstance(release, dict):
        raise DetectionError("official latest release response is invalid")
    tag = str(release.get("tag_name", ""))
    official_version = normalize_version(tag)
    official_commit_data = client.get(
        f"/repos/{OFFICIAL_REPOSITORY}/commits/{urllib.parse.quote(tag, safe='')}"
    )
    official_commit = str(official_commit_data.get("sha", "")).lower()
    if not COMMIT_RE.fullmatch(official_commit):
        raise DetectionError("official release did not resolve to an immutable commit")

    fork_data = client.get(
        f"/repos/{FORK_REPOSITORY}/commits/{urllib.parse.quote(FORK_BRANCH, safe='')}"
    )
    fork_commit = str(fork_data.get("sha", "")).lower()
    if not COMMIT_RE.fullmatch(fork_commit):
        raise DetectionError("Fork branch did not resolve to an immutable commit")
    fork_version = read_fork_version(client, fork_commit)
    fork_match = FORK_VERSION_RE.fullmatch(fork_version)
    assert fork_match is not None
    fork_base_version = fork_match.group(1)
    if version_key(fork_base_version) > version_key(official_version):
        raise DetectionError(
            f"Fork base {fork_base_version} is newer than official latest {official_version}"
        )

    base_tag = f"v{fork_base_version}"
    fork_base_data = client.get(
        f"/repos/{OFFICIAL_REPOSITORY}/commits/{urllib.parse.quote(base_tag, safe='')}"
    )
    fork_base_commit = str(fork_base_data.get("sha", "")).lower()
    if not COMMIT_RE.fullmatch(fork_base_commit):
        raise DetectionError("Fork base version did not resolve to an official commit")

    overlay = resolve_overlay(repository_root / "payload" / "ui", official_version)
    inputs = {
        "official": {
            "repository": OFFICIAL_REPOSITORY,
            "version": official_version,
            "tag": tag,
            "commit": official_commit,
            "published_at": str(release.get("published_at", "")),
            "url": str(release.get("html_url", "")),
        },
        "fork": {
            "repository": FORK_REPOSITORY,
            "branch": FORK_BRANCH,
            "version": fork_version,
            "base_version": fork_base_version,
            "base_commit": fork_base_commit,
            "commit": fork_commit,
            "url": f"https://github.com/{FORK_REPOSITORY}/commit/{fork_commit}",
        },
        "overlay": overlay,
    }
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    release_version = (
        fork_version
        if fork_base_version == official_version
        else f"{official_version}-overdraft.{fork_match.group(2)}"
    )
    release_tag = (
        f"fusion-v{release_version}-{official_commit[:8]}-"
        f"{fork_commit[:8]}-u{str(overlay.get('manifest_sha256', 'missing'))[:8]}"
    )
    return {
        "schema": 1,
        "fingerprint": fingerprint,
        "release_version": release_version,
        "release_tag": release_tag,
        **inputs,
    }


def build_decision(
    snapshot: dict[str, Any], state: dict[str, Any], force: bool = False
) -> tuple[bool, bool]:
    last_success = state.get("last_success")
    previous_fingerprint = (
        last_success.get("fingerprint") if isinstance(last_success, dict) else None
    )
    changed = previous_fingerprint != snapshot.get("fingerprint")
    return changed, bool(changed or force)


def read_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": 1, "last_success": None}
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise DetectionError("state file must contain a JSON object")
    return value


def write_github_output(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise DetectionError(f"GitHub output {key} contains a newline")
            handle.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("state/upstreams.json"))
    parser.add_argument("--output", type=Path, default=Path("build/detection.json"))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(__file__).resolve().parents[1]
    token = os.environ.get("GITHUB_TOKEN", os.environ.get("UPDATE_GITHUB_TOKEN", ""))
    snapshot = resolve_snapshot(root, GitHubClient(token))
    state = read_state(args.state)
    changed, should_build = build_decision(snapshot, state, args.force)
    snapshot["changed"] = changed
    snapshot["forced"] = bool(args.force)
    snapshot["should_build"] = should_build

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")

    if args.github_output:
        write_github_output(
            args.github_output,
            {
                "changed": str(changed).lower(),
                "should_build": str(snapshot["should_build"]).lower(),
                "official_version": snapshot["official"]["version"],
                "official_commit": snapshot["official"]["commit"],
                "fork_version": snapshot["fork"]["version"],
                "fork_commit": snapshot["fork"]["commit"],
                "overlay_version": str(snapshot["overlay"].get("source_version", "missing")),
                "release_version": snapshot["release_version"],
                "release_tag": snapshot["release_tag"],
                "fingerprint": snapshot["fingerprint"],
            },
        )
    print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
