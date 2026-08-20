import base64
import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_candidate, detect_updates


class FakeGitHubClient:
    def __init__(self, fork_version: str = "0.1.178-overdraft.1") -> None:
        self.fork_version = fork_version

    def get(self, path: str):
        if path.endswith("/releases/latest"):
            return {
                "tag_name": "v0.1.178",
                "published_at": "2026-08-18T00:00:00Z",
                "html_url": "https://example.invalid/official-release",
            }
        if "/contents/FORK_VERSION?" in path:
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.fork_version.encode()).decode(),
            }
        if path.endswith("/commits/v0.1.178"):
            return {"sha": "e" * 40}
        if path.endswith("/commits/codex-overdraft"):
            return {"sha": "f" * 40}
        raise AssertionError(f"unexpected API path: {path}")


class BuilderTests(unittest.TestCase):
    def make_overlay(self, root: Path, version: str) -> Path:
        directory = root / "payload" / "ui" / version
        directory.mkdir(parents=True)
        manifest = directory / "manifest.json"
        manifest.write_text(
            json.dumps({"overlay_id": "test", "files": [{"path": "frontend/a", "sha256": "0" * 64}]}),
            encoding="utf-8",
        )
        return manifest

    def test_overlay_selection_prefers_exact_then_forwards_latest_older(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exact_manifest = self.make_overlay(root, "0.1.178")
            exact = detect_updates.resolve_overlay(root / "payload" / "ui", "0.1.178")
            self.assertEqual(exact["mode"], "exact")
            self.assertEqual(exact["source_version"], "0.1.178")
            self.assertEqual(exact["manifest_sha256"], detect_updates.sha256_file(exact_manifest))

            forward = detect_updates.resolve_overlay(root / "payload" / "ui", "0.1.179")
            self.assertEqual(forward["mode"], "forward-replay")
            self.assertEqual(forward["source_version"], "0.1.178")

    def test_overlay_selection_never_uses_a_newer_overlay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.179")
            result = detect_updates.resolve_overlay(root / "payload" / "ui", "0.1.178")
            self.assertFalse(result["available"])
            self.assertEqual(result["mode"], "missing")

    def test_0179_overlay_keeps_the_catalog_and_records_source_state(self):
        root = Path(__file__).resolve().parents[1]
        overlay = root / "payload" / "ui" / "0.1.179"
        manifest = json.loads((overlay / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["target_version"], "0.1.179")
        self.assertTrue(
            all(
                ("source_sha256" in entry) != (entry.get("source_missing") is True)
                for entry in manifest["files"]
            )
        )
        filters = (
            overlay
            / "frontend"
            / "src"
            / "components"
            / "admin"
            / "account"
            / "AccountTableFilters.vue"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "import { CONCRETE_PLATFORM_OPTIONS } from '@/constants/platforms'",
            filters,
        )
        self.assertIn("...CONCRETE_PLATFORM_OPTIONS", filters)
        self.assertIn("xl:flex-nowrap", filters)

    def test_overlay_source_state_rejects_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "frontend" / "source.ts"
            destination.parent.mkdir(parents=True)
            destination.write_text("original", encoding="utf-8")
            entry = {"source_sha256": build_candidate.manager.sha256_file(destination)}
            relative = Path("frontend/source.ts")
            build_candidate.verify_overlay_source_state(destination, entry, relative)

            destination.write_text("updated", encoding="utf-8")
            with self.assertRaises(build_candidate.BuildError):
                build_candidate.verify_overlay_source_state(destination, entry, relative)

            missing = Path(temporary) / "frontend" / "new.ts"
            build_candidate.verify_overlay_source_state(
                missing, {"source_missing": True}, Path("frontend/new.ts")
            )
            missing.write_text("now exists", encoding="utf-8")
            with self.assertRaises(build_candidate.BuildError):
                build_candidate.verify_overlay_source_state(
                    missing, {"source_missing": True}, Path("frontend/new.ts")
                )

    def test_path_traversal_is_rejected(self):
        for value in ("", ".", "../secret", "frontend/../../secret"):
            with self.subTest(value=value):
                with self.assertRaises(build_candidate.BuildError):
                    build_candidate.safe_relative_path(value)

    def test_fingerprint_state_controls_builds(self):
        snapshot = {"fingerprint": "a" * 64}
        state = {"schema": 1, "last_success": {"fingerprint": "a" * 64}}
        self.assertEqual(detect_updates.build_decision(snapshot, state), (False, False))
        self.assertEqual(detect_updates.build_decision(snapshot, state, True), (False, True))
        state["last_success"]["fingerprint"] = "b" * 64
        self.assertEqual(detect_updates.build_decision(snapshot, state), (True, True))

    def test_snapshot_generates_input_derived_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.make_overlay(root, "0.1.178")
            result = detect_updates.resolve_snapshot(root, FakeGitHubClient())
            self.assertEqual(result["release_version"], "0.1.178-overdraft.1")
            self.assertEqual(
                result["release_tag"],
                f"fusion-v0.1.178-overdraft.1-eeeeeeee-ffffffff-u{detect_updates.sha256_file(manifest)[:8]}",
            )
            self.assertRegex(result["fingerprint"], r"^[0-9a-f]{64}$")

    def test_snapshot_rejects_fork_based_on_newer_official(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.178")
            with self.assertRaises(detect_updates.DetectionError):
                detect_updates.resolve_snapshot(
                    root, FakeGitHubClient("0.1.179-overdraft.1")
                )


if __name__ == "__main__":
    unittest.main()
