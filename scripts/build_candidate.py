#!/usr/bin/env python3
"""Prepare, test, and package a fused native Sub2API candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import manager  # noqa: E402
from scripts import detect_updates  # noqa: E402


COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
TEXT_SOURCE_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ts",
    ".tsx",
    ".vue",
    ".yaml",
    ".yml",
}


class BuildError(RuntimeError):
    pass


def adapt_remote_skill_seed_for_go_embed(source: Path) -> dict[str, Any]:
    """Give Go embed a portable physical tree while retaining logical paths.

    Go toolchains used by the builder reject some Unicode filenames while
    walking ``all:`` embed patterns.  The bundle manifest is the public
    logical namespace, so only the temporary embedded filename is changed and
    the manifest records the mapping for the loader.
    """
    seed_root = source / "backend" / "internal" / "service" / "remote_skill_seed"
    tree_root = seed_root / "tree"
    manifest_path = seed_root / "manifest.json"
    registry_path = source / "backend" / "internal" / "service" / "remote_skill_registry_manifest.go"
    if not tree_root.is_dir() or not manifest_path.is_file() or not registry_path.is_file():
        return {"remote_skill_embed_adaptation": "not-needed"}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"remote skill manifest cannot be adapted: {exc}") from exc
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise BuildError("remote skill manifest cannot be adapted: files is not a list")

    adapted = 0
    renamed: set[str] = set()
    assigned: set[Path] = set()
    physical_files = [path for path in tree_root.rglob("*") if path.is_file()]
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("source_kind") != "upstream":
            continue
        logical = str(entry.get("path", ""))
        if not logical:
            raise BuildError("remote skill manifest contains an empty upstream path")
        physical = tree_root.joinpath(*Path(logical).parts)
        if not physical.is_file():
            expected_length = entry.get("byte_length")
            expected_hash = str(entry.get("sha256", "")).lower()
            candidates = []
            for candidate in physical_files:
                if candidate in assigned or not candidate.is_file():
                    continue
                if isinstance(expected_length, int) and candidate.stat().st_size != expected_length:
                    continue
                if hashlib.sha256(candidate.read_bytes()).hexdigest() == expected_hash:
                    candidates.append(candidate)
            if len(candidates) != 1:
                raise BuildError(
                    f"remote skill seed file is missing or ambiguous: {logical} "
                    f"(content matches={len(candidates)})"
                )
            physical = candidates[0]
        assigned.add(physical)
        relative = physical.relative_to(tree_root).as_posix()
        if all(ord(char) < 128 for char in relative):
            entry.pop("embedded_path", None)
            continue

        # Hash the logical path so the generated ASCII name is deterministic
        # and cannot collide with another Unicode filename.
        digest = hashlib.sha256(unicodedata.normalize("NFC", logical).encode("utf-8")).hexdigest()[:16]
        suffix = physical.suffix if physical.suffix.isascii() else ".bin"
        portable_name = f"__unicode_{digest}{suffix}"
        destination = physical.with_name(portable_name)
        if destination.exists() and destination != physical:
            raise BuildError(f"remote skill portable filename collision: {logical}")
        if destination != physical:
            physical.rename(destination)
            adapted += 1
            renamed.add(logical)
        entry["embedded_path"] = f"tree/{destination.relative_to(tree_root).as_posix()}"

    if not adapted:
        # A previous adaptation may already have supplied mappings; retain a
        # clean manifest when the source is already portable.
        return {"remote_skill_embed_adaptation": "not-needed"}

    registry = registry_path.read_text(encoding="utf-8")
    old_validation = """\t\tcase \"upstream\":
\t\t\tupstreamCount++
\t\t\tif entry.EmbeddedPath != \"\" || entry.Provenance != nil {
\t\t\t\treturn fmt.Errorf(\"%w: upstream manifest entry has pinned metadata\", ErrBusinessSystemPromptBundleInvalid)
\t\t\t}"""
    new_validation = """\t\tcase \"upstream\":
\t\t\tupstreamCount++
\t\t\tif entry.Provenance != nil {
\t\t\t\treturn fmt.Errorf(\"%w: upstream manifest entry has pinned metadata\", ErrBusinessSystemPromptBundleInvalid)
\t\t\t}
\t\t\tif entry.EmbeddedPath != \"\" {
\t\t\t\tportable, portableErr := normalizeBundleRelativePath(strings.TrimPrefix(entry.EmbeddedPath, \"tree/\"))
\t\t\t\tif portableErr != nil || !strings.HasPrefix(entry.EmbeddedPath, \"tree/\") || portable != strings.TrimPrefix(entry.EmbeddedPath, \"tree/\") {
\t\t\t\t\treturn fmt.Errorf(\"%w: upstream embedded path invalid\", ErrBusinessSystemPromptBundleInvalid)
\t\t\t\t}
\t\t\t}"""
    old_lookup = "\t\t\tbody, ok = upstreamFiles[entry.Path]"
    new_lookup = """\t\t\tlookupPath := entry.Path
\t\t\tif entry.EmbeddedPath != \"\" {
\t\t\t\tlookupPath = strings.TrimPrefix(entry.EmbeddedPath, \"tree/\")
\t\t\t}
\t\t\tbody, ok = upstreamFiles[lookupPath]"""
    old_undeclared = """\tfor name := range upstreamFiles {
\t\tif _, ok := files[name]; !ok {
\t\t\treturn fmt.Errorf(\"%w: undeclared embedded upstream file\", ErrBusinessSystemPromptBundleInvalid)
\t\t}
\t}"""
    new_undeclared = """\tdeclaredEmbedded := make(map[string]struct{}, len(manifest.Files))
\tfor _, entry := range manifest.Files {
\t\tif entry.SourceKind != \"upstream\" {
\t\t\tcontinue
\t\t}
\t\tlookupPath := entry.Path
\t\tif entry.EmbeddedPath != \"\" {
\t\t\tlookupPath = strings.TrimPrefix(entry.EmbeddedPath, \"tree/\")
\t\t}
\t\tdeclaredEmbedded[lookupPath] = struct{}{}
\t}
\tfor name := range upstreamFiles {
\t\tif _, ok := declaredEmbedded[name]; !ok {
\t\t\treturn fmt.Errorf(\"%w: undeclared embedded upstream file\", ErrBusinessSystemPromptBundleInvalid)
\t\t}
\t}"""
    if old_validation not in registry or old_lookup not in registry or old_undeclared not in registry:
        raise BuildError("remote skill loader shape changed; portability adaptation needs review")
    registry = (
        registry.replace(old_validation, new_validation, 1)
        .replace(old_lookup, new_lookup, 1)
        .replace(old_undeclared, new_undeclared, 1)
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    registry_path.write_text(registry, encoding="utf-8", newline="\n")
    return {
        "remote_skill_embed_adaptation": "ascii-physical-names",
        "remote_skill_embed_renamed_files": str(adapted),
        "remote_skill_embed_renamed_paths_sha256": hashlib.sha256(
            "\n".join(sorted(renamed)).encode("utf-8")
        ).hexdigest(),
    }


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


def source_sha256(path: Path) -> str:
    """Hash source text canonically so Windows CRLF cannot cause false drift."""
    data = path.read_bytes()
    if path.suffix.lower() in TEXT_SOURCE_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


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
        actual = source_sha256(destination)
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


def fetch_replay_base(source: Path, base_commit: str) -> None:
    """Fetch the fork baseline into the full shallow source clone."""
    run(
        ["git", "fetch", "--depth=1", "origin", base_commit],
        cwd=source,
    )


def approved_replay_from_detection(detection: dict[str, Any]) -> dict[str, Any] | None:
    """Re-validate the exact local replay selected by immutable detection."""
    replay = detection.get("replay")
    if not isinstance(replay, dict):
        return None
    if replay.get("mode") != "approved-resolved-patch":
        return None
    expected_manifest_sha256 = str(replay.get("manifest_sha256", "")).lower()
    expected_entry_sha256 = str(replay.get("entry_sha256", "")).lower()
    replay_id = replay.get("id")
    if (
        not isinstance(replay_id, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_entry_sha256)
    ):
        raise BuildError("approved replay metadata is malformed")
    try:
        manifest_sha256, entries = detect_updates.load_approved_replays(ROOT)
    except detect_updates.DetectionError as exc:
        raise BuildError(f"approved replay manifest validation failed: {exc}") from exc
    if manifest_sha256 != expected_manifest_sha256:
        raise BuildError("approved replay manifest changed after detection")
    matches = [entry for entry in entries if entry["id"] == replay_id]
    if len(matches) != 1:
        raise BuildError("approved replay entry is missing or duplicated")
    entry = matches[0]
    if detect_updates.canonical_json_sha256(entry) != expected_entry_sha256:
        raise BuildError("approved replay entry changed after detection")

    official = detection.get("official")
    fork = detection.get("fork")
    if not isinstance(official, dict) or not isinstance(fork, dict):
        raise BuildError("approved replay detection is missing upstream provenance")
    target_matches = entry["target"] == {
        "repository": str(official.get("repository", "")),
        "version": str(official.get("version", "")),
        "commit": str(official.get("commit", "")).lower(),
    }
    source_matches = (
        entry["source"]["repository"] == str(fork.get("repository", ""))
        and entry["source"]["branch"] == str(fork.get("branch", ""))
        and entry["source"]["version"] == str(fork.get("version", ""))
        and entry["source"]["commit"] == str(fork.get("commit", "")).lower()
        and entry["source"]["base_version"] == str(fork.get("base_version", ""))
        and entry["source"]["base_commit"] == str(fork.get("base_commit", "")).lower()
    )
    replay_matches = (
        replay.get("target") == entry["target"]
        and replay.get("source") == entry["source"]
        and replay.get("patch") == entry["patch"]
        and replay.get("overdraft_revision") == entry["overdraft_revision"]
    )
    if not target_matches or not source_matches or not replay_matches:
        raise BuildError("approved replay no longer matches the immutable detection inputs")
    return entry


def prepare_approved_replay(work: Path, detection: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    official = detection["official"]
    replay = approved_replay_from_detection(detection)
    if replay is None:
        raise BuildError("no approved replay is available for this custom Fork source")
    if str(official.get("repository", "")) != manager.OFFICIAL_REPO:
        raise BuildError("approved replay official repository does not match the build source")
    source, source_tree = manager.clone_official_source(
        str(official["version"]),
        str(official["commit"]),
        work,
        partial=False,
    )
    patch_path = ROOT / str(replay["patch"]["path"])
    run(["git", "apply", "--check", "--whitespace=nowarn", patch_path], cwd=source)
    run(["git", "apply", "--whitespace=nowarn", patch_path], cwd=source)
    unresolved = run(
        ["git", "diff", "--name-only", "--diff-filter=U"], cwd=source, capture=True
    ).strip()
    if unresolved:
        raise BuildError(f"approved replay left unresolved files:\n{unresolved}")
    return source, {
        "integration_mode": "official-plus-approved-resolved-replay",
        "official_source_tree": source_tree,
        "fork_diff_sha256": str(replay["source"]["feature_diff_sha256"]),
        "fork_base_commit": str(replay["source"]["base_commit"]),
        "fork_replay_patch": str(replay["patch"]["path"]),
        "fork_replay_patch_sha256": str(replay["patch"]["sha256"]),
        "fork_replay_id": str(replay["id"]),
        "fork_replay_source_mode": "approved-resolved-patch",
        "fork_replay_excluded_paths": list(replay["source"].get("excluded_paths", [])),
    }


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
        partial=False,
    )
    fetch_replay_base(source, str(fork["base_commit"]))
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
        "fork_replay_base_hydrated": str(fork["base_commit"]),
        "fork_replay_source_mode": "shallow-full",
    }


def prepare_source(work: Path, detection: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    official_version = str(detection["official"]["version"])
    fork_base_version = str(detection["fork"]["base_version"])
    approved_replay = approved_replay_from_detection(detection)
    if approved_replay is not None:
        source, provenance = prepare_approved_replay(work, detection)
    elif str(detection["fork"].get("flavor", "")) in {"custom", "codexrip"}:
        raise BuildError("custom and codexrip Fork sources require an approved resolved replay")
    elif fork_base_version == official_version:
        source, provenance = prepare_aligned_fork(work, detection)
    else:
        source, provenance = prepare_replayed_fork(work, detection)
    provenance.update(adapt_remote_skill_seed_for_go_embed(source))
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
