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
FORK_REPOSITORY = os.environ.get(
    "SUB2API_FORK_REPOSITORY", "kiasd/sub2api-overdraft-auto-builder"
)
FORK_BRANCH = os.environ.get("SUB2API_FORK_BRANCH", "fusion-proof-v0.2.3-codexrip.7")
API_ROOT = "https://api.github.com"
VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")
FORK_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)-(overdraft|custom|codexrip)\.(\d+)$")
ALLOWED_REPLAY_EXCLUDED_PATHS = frozenset(
    {
        ".github/workflows/production-deploy.yml",
        ".github/workflows/ssh-deploy-key-probe.yml",
        ".github/workflows/upstream-auto-deploy.yml",
    }
)
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
REPLAY_MANIFEST_PATH = Path("payload/fork-replays/manifest.json")
BUILD_DEFINITION_PATHS = (
    ".gitattributes",
    "manager.py",
    "plugin.json",
    "scripts/build_candidate.py",
    "scripts/detect_updates.py",
    "scripts/record_success.py",
    "scripts/render_release_notes.py",
    ".github/workflows/auto-build.yml",
    "payload/fork-replays/manifest.json",
    "payload/migrationcheck/main.go",
    "LICENSE",
    "NOTICE",
)


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


def build_definition_sha256(repository_root: Path) -> str:
    """Hash the files that define a verified release's build and validation path."""
    digest = hashlib.sha256()
    for relative in BUILD_DEFINITION_PATHS:
        path = repository_root / relative
        if not path.is_file() or path.is_symlink():
            raise DetectionError(f"build definition file is missing or unsafe: {relative}")
        # GitHub Actions checks out these text files with LF. Normalize locally too
        # so a Windows checkout cannot produce a different release identity.
        content = path.read_bytes().replace(b"\r\n", b"\n")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def release_identity_sha256(
    inputs: dict[str, Any], builder_definition_sha256: str
) -> str:
    material = json.dumps(
        {
            "builder_definition_sha256": builder_definition_sha256,
            # The selected source version and replay mode affect the candidate's
            # provenance even when two overlay manifests contain identical bytes.
            "official": inputs.get("official"),
            "fork": inputs.get("fork"),
            "overlay": inputs.get("overlay"),
            "replay": inputs.get("replay"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


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


def parse_fork_version(value: str) -> tuple[str, str, int]:
    match = FORK_VERSION_RE.fullmatch(value.strip())
    if not match:
        raise DetectionError(f"invalid Fork version: {value!r}")
    return match.group(1), match.group(2), int(match.group(3))


def canonical_json_sha256(value: Any) -> str:
    material = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def checked_commit(value: Any, label: str) -> str:
    commit = str(value).lower()
    if not COMMIT_RE.fullmatch(commit):
        raise DetectionError(f"approved replay has invalid {label}")
    return commit


def replay_file(root: Path, value: str) -> Path:
    relative = Path(value)
    if (
        relative.is_absolute()
        or value in {"", "."}
        or any(part in {"", ".", ".."} for part in relative.parts)
        or relative.parts[:2] != ("payload", "fork-replays")
    ):
        raise DetectionError(f"approved replay has unsafe patch path: {value!r}")
    root_path = root.resolve()
    path = (root_path / relative).resolve()
    if root_path not in path.parents or path.is_symlink() or not path.is_file():
        raise DetectionError(f"approved replay patch is missing or unsafe: {value!r}")
    if path.stat().st_size == 0:
        raise DetectionError(f"approved replay patch is empty: {value!r}")
    return path


def load_approved_replays(root: Path) -> tuple[str, list[dict[str, Any]]]:
    manifest_path = root / REPLAY_MANIFEST_PATH
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise DetectionError("approved replay manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DetectionError("approved replay manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise DetectionError("approved replay manifest has an unsupported schema")
    entries = manifest.get("replays")
    if not isinstance(entries, list):
        raise DetectionError("approved replay manifest has no replay list")

    validated: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in entries:
        if not isinstance(raw, dict):
            raise DetectionError("approved replay manifest contains a non-object entry")
        replay_id = raw.get("id")
        target = raw.get("target")
        source = raw.get("source")
        patch = raw.get("patch")
        revision = raw.get("overdraft_revision")
        if (
            not isinstance(replay_id, str)
            or not replay_id
            or replay_id in seen_ids
            or not isinstance(target, dict)
            or not isinstance(source, dict)
            or not isinstance(patch, dict)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
        ):
            raise DetectionError("approved replay entry is malformed or duplicated")
        seen_ids.add(replay_id)

        target_version = normalize_version(str(target.get("version", "")))
        source_version = str(source.get("version", "")).strip()
        # A custom release label is not necessarily its Git merge base. The
        # independently audited base below is the value used for replay.
        _, source_flavor, _ = parse_fork_version(source_version)
        if source_flavor not in {"custom", "codexrip"}:
            raise DetectionError(
                f"approved replay {replay_id} must lock a custom or codexrip source"
            )
        source_base_version = normalize_version(str(source.get("base_version", "")))
        patch_value = str(patch.get("path", ""))
        patch_path = replay_file(root, patch_value)
        patch_sha256 = str(patch.get("sha256", "")).lower()
        feature_diff_sha256 = str(source.get("feature_diff_sha256", "")).lower()
        excluded_raw = source.get("excluded_paths", [])
        if not isinstance(excluded_raw, list) or any(
            not isinstance(value, str) or value not in ALLOWED_REPLAY_EXCLUDED_PATHS
            for value in excluded_raw
        ):
            raise DetectionError(f"approved replay {replay_id} has unsupported excluded paths")
        excluded_paths = sorted(set(excluded_raw))
        checked_commit(target.get("commit"), "target commit")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", patch_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", feature_diff_sha256)
            or sha256_file(patch_path) != patch_sha256
        ):
            raise DetectionError(f"approved replay {replay_id} checksum validation failed")
        patch_bytes = patch_path.read_bytes()
        if excluded_paths and any(
            f"diff --git a/{path} b/{path}".encode("utf-8") in patch_bytes
            for path in excluded_paths
        ):
            raise DetectionError(f"approved replay {replay_id} still contains an excluded path")
        target_repository = str(target.get("repository", "")).strip()
        source_repository = str(source.get("repository", "")).strip()
        source_branch = str(source.get("branch", "")).strip()
        if not target_repository or not source_repository or not source_branch:
            raise DetectionError(f"approved replay {replay_id} has incomplete source identity")
        validated.append(
            {
                "id": replay_id,
                "target": {
                    "repository": target_repository,
                    "version": target_version,
                    "commit": checked_commit(target.get("commit"), "target commit"),
                },
                "source": {
                    "repository": source_repository,
                    "branch": source_branch,
                    "version": source_version,
                    "commit": checked_commit(source.get("commit"), "source commit"),
                    "base_version": source_base_version,
                    "base_commit": checked_commit(source.get("base_commit"), "base commit"),
                    "feature_diff_sha256": feature_diff_sha256,
                    **({"excluded_paths": excluded_paths} if excluded_paths else {}),
                },
                "patch": {"path": patch_value, "sha256": patch_sha256},
                "overdraft_revision": revision,
            }
        )
    return sha256_file(manifest_path), validated


def resolve_approved_replay(
    root: Path,
    official: dict[str, str],
    repository: str,
    branch: str,
    version: str,
    commit: str,
) -> dict[str, Any]:
    manifest_sha256, entries = load_approved_replays(root)
    matches = [
        entry
        for entry in entries
        if entry["target"] == {
            "repository": official["repository"],
            "version": official["version"],
            "commit": official["commit"],
        }
        and entry["source"]["repository"] == repository
        and entry["source"]["branch"] == branch
        and entry["source"]["version"] == version
        and entry["source"]["commit"] == commit
    ]
    if len(matches) != 1:
        raise DetectionError(
            "custom Fork change requires an approved resolved replay for the exact "
            "official release and Fork commit; no candidate was built"
        )
    entry = matches[0]
    return {
        "mode": "approved-resolved-patch",
        "id": entry["id"],
        "manifest_sha256": manifest_sha256,
        "entry_sha256": canonical_json_sha256(entry),
        "target": entry["target"],
        "source": entry["source"],
        "patch": entry["patch"],
        "overdraft_revision": entry["overdraft_revision"],
    }


def read_fork_version(client: GitHubClient, commit: str) -> str:
    try:
        payload = client.get(
            f"/repos/{FORK_REPOSITORY}/contents/FORK_VERSION?ref={urllib.parse.quote(commit, safe='')}"
        )
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise DetectionError("Fork returned an unsupported FORK_VERSION payload")
        encoded = "".join(str(payload.get("content", "")).split())
        value = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
        parse_fork_version(value)
        return value
    except (DetectionError, ValueError, UnicodeDecodeError):
        # HTExplicit publishes codexrip versions as Releases and does not carry
        # the legacy FORK_VERSION file. The Release is still validated below
        # and the source commit remains pinned independently.
        release = client.get(f"/repos/{FORK_REPOSITORY}/releases/latest")
        if not isinstance(release, dict):
            raise DetectionError("Fork latest Release payload is invalid")
        tag = str(release.get("tag_name", "")).strip()
        value = tag[1:] if tag.startswith("v") else tag
        parse_fork_version(value)
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
    fork_label_version, fork_flavor, fork_revision = parse_fork_version(fork_version)
    official = {
        "repository": OFFICIAL_REPOSITORY,
        "version": official_version,
        "tag": tag,
        "commit": official_commit,
        "published_at": str(release.get("published_at", "")),
        "url": str(release.get("html_url", "")),
    }
    replay: dict[str, Any]
    if fork_flavor in {"custom", "codexrip"}:
        replay = resolve_approved_replay(
            repository_root,
            {
                "repository": official["repository"],
                "version": official["version"],
                "commit": official["commit"],
            },
            FORK_REPOSITORY,
            FORK_BRANCH,
            fork_version,
            fork_commit,
        )
        fork_base_version = str(replay["source"]["base_version"])
        fork_base_commit = str(replay["source"]["base_commit"])
        release_flavor = "overdraft" if fork_flavor == "custom" else fork_flavor
        release_version = (
            f"{official_version}-{release_flavor}.{replay['overdraft_revision']}"
        )
    else:
        fork_base_version = fork_label_version
        fork_base_commit = ""
        replay = {
            "mode": "live-3way-replay",
            "flavor": fork_flavor,
            "overdraft_revision": fork_revision,
        }
        release_version = (
            fork_version
            if fork_base_version == official_version
            else f"{official_version}-overdraft.{fork_revision}"
        )
    if version_key(fork_base_version) > version_key(official_version):
        raise DetectionError(
            f"Fork base {fork_base_version} is newer than official latest {official_version}"
        )

    base_tag = f"v{fork_base_version}"
    fork_base_data = client.get(
        f"/repos/{OFFICIAL_REPOSITORY}/commits/{urllib.parse.quote(base_tag, safe='')}"
    )
    resolved_base_commit = str(fork_base_data.get("sha", "")).lower()
    if not COMMIT_RE.fullmatch(resolved_base_commit):
        raise DetectionError("Fork base version did not resolve to an official commit")
    if fork_base_commit and resolved_base_commit != fork_base_commit:
        raise DetectionError(
            "approved replay base tag no longer resolves to its locked official commit"
        )
    fork_base_commit = resolved_base_commit

    overlay = resolve_overlay(repository_root / "payload" / "ui", official_version)
    builder = {"definition_sha256": build_definition_sha256(repository_root)}
    inputs = {
        "official": official,
        "fork": {
            "repository": FORK_REPOSITORY,
            "branch": FORK_BRANCH,
            "version": fork_version,
            "flavor": fork_flavor,
            "base_version": fork_base_version,
            "base_commit": fork_base_commit,
            "commit": fork_commit,
            "url": f"https://github.com/{FORK_REPOSITORY}/commit/{fork_commit}",
        },
        "replay": replay,
        "overlay": overlay,
        "builder": builder,
    }
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    release_identity = release_identity_sha256(
        inputs,
        builder["definition_sha256"],
    )
    release_tag = (
        f"fusion-v{release_version}-{official_commit[:8]}-"
        f"{fork_commit[:8]}-u{release_identity[:8]}"
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
