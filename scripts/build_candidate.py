#!/usr/bin/env python3
"""Prepare, test, and package a fused native Sub2API candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import manager  # noqa: E402


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class BuildError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise BuildError(f"{path} must contain a JSON object")
    return value


def run(command: list[str | Path], *, cwd: Path, capture: bool = False) -> str:
    argv = [str(part) for part in command]
    print("+ " + " ".join(argv), file=sys.stderr, flush=True)
    result = subprocess.run(
        argv,
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    if result.returncode != 0:
        output = (result.stdout or "").strip()
        raise BuildError(f"command failed ({result.returncode}): {' '.join(argv)}\n{output[-8192:]}")
    return result.stdout or ""


def safe_relative_path(value: str) -> Path:
    relative = Path(value)
    if (
        relative.is_absolute()
        or value in {"", "."}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise BuildError(f"unsafe overlay path: {value!r}")
    return relative


def verify_overlay_source_state(destination: Path, entry: dict[str, Any], relative: Path) -> None:
    has_source_hash = "source_sha256" in entry
    source_missing = entry.get("source_missing")
    if has_source_hash and source_missing:
        raise BuildError(
            f"UI overlay source state is ambiguous: {relative.as_posix()}"
        )
    if has_source_hash:
        expected = str(entry.get("source_sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise BuildError(
                f"UI overlay source checksum is invalid: {relative.as_posix()}"
            )
        if destination.is_symlink() or not destination.is_file():
            raise BuildError(
                f"UI overlay source file is missing: {relative.as_posix()}"
            )
        actual = manager.sha256_file(destination)
        if actual != expected:
            raise BuildError(
                "UI overlay source changed and needs adaptation: "
                f"{relative.as_posix()}"
            )
    elif source_missing is not None:
        if source_missing is not True:
            raise BuildError(
                f"UI overlay source_missing must be true: {relative.as_posix()}"
            )
        if destination.exists() or destination.is_symlink():
            raise BuildError(
                "UI overlay source unexpectedly exists and needs adaptation: "
                f"{relative.as_posix()}"
            )


def apply_compatible_overlay(source: Path, detection: dict[str, Any]) -> dict[str, Any]:
    overlay = detection.get("overlay")
    if not isinstance(overlay, dict) or not overlay.get("available"):
        raise BuildError("no compatible UI overlay is available for this official version")
    source_version = str(overlay.get("source_version", ""))
    directory = ROOT / "payload" / "ui" / source_version
    manifest_path = directory / "manifest.json"
    expected_manifest_hash = str(overlay.get("manifest_sha256", "")).lower()
    if not manifest_path.is_file() or manager.sha256_file(manifest_path) != expected_manifest_hash:
        raise BuildError("UI overlay manifest hash changed after detection")
    manifest = load_json(manifest_path)
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BuildError("UI overlay manifest has no files")

    source_root = source.resolve()
    candidates: list[tuple[Path, Path, Path]] = []
    for entry in files:
        if not isinstance(entry, dict):
            raise BuildError("UI overlay manifest contains a non-object entry")
        relative = safe_relative_path(str(entry.get("path", "")))
        payload = directory / relative
        expected = str(entry.get("sha256", "")).lower()
        if (
            not payload.is_file()
            or payload.is_symlink()
            or manager.sha256_file(payload) != expected
        ):
            raise BuildError(f"UI overlay checksum failed: {relative.as_posix()}")
        destination = (source_root / relative).resolve()
        if destination != source_root and source_root not in destination.parents:
            raise BuildError(f"UI overlay escapes source tree: {relative.as_posix()}")
        if destination.exists() and destination.is_symlink():
            raise BuildError(f"refusing to replace a symlink: {relative.as_posix()}")
        verify_overlay_source_state(destination, entry, relative)
        candidates.append((relative, payload, destination))

    applied: list[str] = []
    for relative, payload, destination in candidates:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(payload, destination)
        applied.append(relative.as_posix())
    return {
        "overlay_id": str(manifest.get("overlay_id", "unknown")),
        "overlay_mode": str(overlay.get("mode", "unknown")),
        "overlay_source_version": source_version,
        "overlay_target_version": str(overlay.get("target_version", "")),
        "overlay_manifest_sha256": expected_manifest_hash,
        "overlay_files": applied,
    }


def clone_at(repository: str, commit: str, destination: Path) -> Path:
    if not COMMIT_RE.fullmatch(commit):
        raise BuildError(f"invalid commit for {repository}: {commit!r}")
    url = f"https://github.com/{repository}.git"
    try:
        run(["git", "clone", "--filter=blob:none", "--no-checkout", url, destination], cwd=destination.parent)
    except BuildError:
        shutil.rmtree(destination, ignore_errors=True)
        run(["git", "clone", "--no-checkout", url, destination], cwd=destination.parent)
    run(["git", "fetch", "--depth=1", "origin", commit], cwd=destination)
    run(["git", "checkout", "--detach", commit], cwd=destination)
    return destination


def prepare_aligned_fork(work: Path, detection: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    fork = detection["fork"]
    archive = work / "fork-source.tar.gz"
    manager.download(
        f"https://codeload.github.com/{fork['repository']}/tar.gz/{fork['commit']}",
        archive,
    )
    archive_hash = manager.sha256_file(archive)
    source = manager.safe_extract_tar(archive, work / "source")
    provenance = manager.validate_fork_source(
        source,
        str(fork["version"]),
        archive_hash,
        str(fork["commit"]),
    )
    provenance.update(
        {
            "integration_mode": "fork-native-aligned",
            "source_archive_sha256": archive_hash,
        }
    )
    return source, provenance


def prepare_replayed_fork(work: Path, detection: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    official = detection["official"]
    fork = detection["fork"]
    source, source_tree = manager.clone_official_source(
        str(official["version"]),
        str(official["commit"]),
        work,
    )
    fork_source = clone_at(str(fork["repository"]), str(fork["commit"]), work / "fork-source")
    official_url = f"https://github.com/{official['repository']}.git"
    run(["git", "remote", "add", "official-upstream", official_url], cwd=fork_source)
    run(["git", "fetch", "--filter=blob:none", "official-upstream", str(fork["base_commit"])], cwd=fork_source)
    patch_path = work / "fork-feature.patch"
    patch_text = run(
        ["git", "diff", "--binary", str(fork["base_commit"]), str(fork["commit"])],
        cwd=fork_source,
        capture=True,
    )
    if not patch_text.strip():
        raise BuildError("Fork feature diff is empty")
    patch_path.write_text(patch_text, encoding="utf-8", newline="\n")
    run(["git", "apply", "--3way", "--ignore-whitespace", patch_path], cwd=source)
    unresolved = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=source, capture=True).strip()
    if unresolved:
        raise BuildError(f"Fork replay left unresolved files:\n{unresolved}")
    return source, {
        "integration_mode": "official-plus-live-fork-replay",
        "official_source_tree": source_tree,
        "fork_diff_sha256": manager.sha256_file(patch_path),
        "fork_base_commit": str(fork["base_commit"]),
    }


def prepare_source(work: Path, detection: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    official_version = str(detection["official"]["version"])
    fork_base_version = str(detection["fork"]["base_version"])
    if fork_base_version == official_version:
        source, provenance = prepare_aligned_fork(work, detection)
    else:
        source, provenance = prepare_replayed_fork(work, detection)
    provenance.update(apply_compatible_overlay(source, detection))
    return source, provenance


def package_candidate(
    output: Path,
    binary: Path,
    metadata: dict[str, Any],
    release_tag: str,
) -> dict[str, str]:
    output.mkdir(parents=True, exist_ok=True)
    binary_target = output / "sub2api"
    shutil.copy2(binary, binary_target)
    binary_target.chmod(0o750)
    metadata_path = output / "build-metadata.json"
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    archive = output / f"{release_tag}-linux-amd64.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(binary_target, arcname="sub2api")
        bundle.add(metadata_path, arcname="build-metadata.json")
        bundle.add(ROOT / "LICENSE", arcname="LICENSE")
        bundle.add(ROOT / "NOTICE", arcname="NOTICE")

    hashes = {
        binary_target.name: manager.sha256_file(binary_target),
        metadata_path.name: manager.sha256_file(metadata_path),
        archive.name: manager.sha256_file(archive),
    }
    checksum_path = output / "SHA256SUMS"
    checksum_path.write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(hashes.items())),
        encoding="ascii",
        newline="\n",
    )
    return {"archive": str(archive), "checksums": str(checksum_path), **hashes}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detection", type=Path, default=Path("build/detection.json"))
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    detection = load_json(args.detection)
    if not detection.get("overlay", {}).get("available"):
        raise BuildError("build is blocked because no UI overlay can be replayed")
    release_version = str(detection["release_version"])
    release_tag = str(detection["release_tag"])
    source_commit = str(detection["fork"]["commit"])
    with tempfile.TemporaryDirectory(prefix="sub2api-fusion-") as temporary:
        work = Path(temporary)
        source, provenance = prepare_source(work, detection)
        metadata: dict[str, Any] = {
            "schema": 1,
            "release_tag": release_tag,
            "release_version": release_version,
            "inputs": detection,
            "provenance": provenance,
        }
        args.output.mkdir(parents=True, exist_ok=True)
        if args.prepare_only:
            metadata["status"] = "prepared"
            (args.output / "build-metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            print(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        candidate = work / "artifacts" / "sub2api"
        build_info = manager.build_and_test(
            source,
            release_version,
            source_commit,
            candidate,
            "overdraft",
        )
        metadata["status"] = "verified"
        metadata["build"] = build_info
        package_info = package_candidate(args.output, candidate, metadata, release_tag)
        print(json.dumps({"metadata": metadata, "package": package_info}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, manager.ManagerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
