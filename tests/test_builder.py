import base64
import json
import tempfile
import unittest
from pathlib import Path

from scripts import build_candidate, detect_updates


class FakeGitHubClient:
    def __init__(
        self,
        fork_version: str = "0.1.178-overdraft.1",
        *,
        official_version: str = "0.1.178",
        official_commit: str = "e" * 40,
        fork_commit: str = "f" * 40,
        base_commits: dict[str, str] | None = None,
    ) -> None:
        self.fork_version = fork_version
        self.official_version = official_version
        self.official_commit = official_commit
        self.fork_commit = fork_commit
        self.base_commits = base_commits or {}

    def get(self, path: str):
        if path.endswith("/releases/latest"):
            return {
                "tag_name": f"v{self.official_version}",
                "published_at": "2026-08-18T00:00:00Z",
                "html_url": "https://example.invalid/official-release",
            }
        if "/contents/FORK_VERSION?" in path:
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.fork_version.encode()).decode(),
            }
        if path.endswith(f"/commits/{detect_updates.FORK_BRANCH}"):
            return {"sha": self.fork_commit}
        for version, commit in self.base_commits.items():
            if path.endswith(f"/commits/v{version}"):
                return {"sha": commit}
        if path.endswith(f"/commits/v{self.official_version}"):
            return {"sha": self.official_commit}
        raise AssertionError(f"unexpected API path: {path}")


class BuilderTests(unittest.TestCase):
    def make_build_definition(self, root: Path) -> str:
        for relative in detect_updates.BUILD_DEFINITION_PATHS:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(f"definition: {relative}\n", encoding="utf-8", newline="\n")
        return detect_updates.build_definition_sha256(root)

    def make_overlay(self, root: Path, version: str) -> Path:
        directory = root / "payload" / "ui" / version
        directory.mkdir(parents=True)
        manifest = directory / "manifest.json"
        manifest.write_text(
            json.dumps({"overlay_id": "test", "files": [{"path": "frontend/a", "sha256": "0" * 64}]}),
            encoding="utf-8",
        )
        return manifest

    def make_custom_replay(
        self,
        root: Path,
        *,
        target_version: str = "0.1.185",
        target_commit: str = "a" * 40,
        source_commit: str = "b" * 40,
        base_commit: str = "c" * 40,
        revision: int = 1,
    ) -> dict:
        patch = root / "payload" / "fork-replays" / "resolved.patch"
        patch.parent.mkdir(parents=True, exist_ok=True)
        patch.write_text("diff --git a/a b/a\n", encoding="utf-8", newline="\n")
        entry = {
            "id": "custom-test-replay",
            "target": {
                "repository": detect_updates.OFFICIAL_REPOSITORY,
                "version": target_version,
                "commit": target_commit,
            },
            "source": {
                "repository": detect_updates.FORK_REPOSITORY,
                "branch": detect_updates.FORK_BRANCH,
                "version": "0.1.184-custom.1",
                "commit": source_commit,
                "base_version": "0.1.183",
                "base_commit": base_commit,
                "feature_diff_sha256": "d" * 64,
            },
            "patch": {
                "path": "payload/fork-replays/resolved.patch",
                "sha256": detect_updates.sha256_file(patch),
            },
            "overdraft_revision": revision,
        }
        manifest = patch.parent / "manifest.json"
        manifest.write_text(
            json.dumps({"schema": 1, "replays": [entry]}),
            encoding="utf-8",
            newline="\n",
        )
        return entry

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

    def test_021_replay_and_overlay_are_locked_to_verified_sources(self):
        root = Path(__file__).resolve().parents[1]
        replay_manifest = json.loads(
            (root / "payload" / "fork-replays" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        replay = next(
            entry
            for entry in replay_manifest["replays"]
            if entry["id"] == "codexrip-0.2.1.4"
        )
        self.assertEqual(replay["target"]["version"], "0.2.1")
        self.assertEqual(
            replay["target"]["commit"],
            "578785ee7fb35030b094b69624efe25670a36f5f",
        )
        self.assertEqual(replay["source"]["version"], "0.2.1-codexrip.1")
        self.assertEqual(
            replay["source"]["commit"],
            "57a697e7872e27a71a89606f1e599c89839c34b8",
        )
        self.assertEqual(
            replay["source"]["base_commit"],
            "578785ee7fb35030b094b69624efe25670a36f5f",
        )
        self.assertEqual(replay["overdraft_revision"], 4)
        patch_path = root / replay["patch"]["path"]
        self.assertEqual(
            detect_updates.sha256_file(patch_path), replay["patch"]["sha256"]
        )
        self.assertEqual(
            replay["source"]["feature_diff_sha256"], replay["patch"]["sha256"]
        )
        patch_bytes = patch_path.read_bytes()
        for excluded in replay["source"]["excluded_paths"]:
            self.assertNotIn(
                f"diff --git a/{excluded} b/{excluded}".encode("utf-8"),
                patch_bytes,
            )
        patch_headers = b"\n".join(
            line for line in patch_bytes.splitlines() if line.startswith(b"diff --git ")
        )
        self.assertNotIn(b"downstream-verify.yml", patch_headers)
        self.assertNotIn(b"downstream-release.yml", patch_headers)
        for required in (
            "backend/internal/service/remote_skill_seed/tree/scripts/env_probe.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/artifact_inventory.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/har_summary.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/new-experience-entry.ps1",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/new_experience_entry.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/pack_cloud_handoff.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/pe_entropy_triage.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/route_task.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/reusable/scaffold_project.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/validate_result.py",
            "backend/internal/service/remote_skill_seed/tree/scripts/validate_skill.py",
        ):
            self.assertIn(f"diff --git a/{required} b/{required}".encode("utf-8"), patch_bytes)

        # The remote-skill seed is a content-addressed bundle.  Keep the
        # complete pinned tree in the replay so a fresh official checkout
        # cannot fail later when Go embed validates the manifest.
        remote_headers = [
            line
            for line in patch_bytes.splitlines()
            if line.startswith(b"diff --git ")
            and b"backend/internal/service/remote_skill_seed/" in line
        ]
        self.assertEqual(len(remote_headers), 459)
        self.assertEqual(
            sum(any(byte >= 0x80 for byte in line) for line in remote_headers),
            47,
        )

        overlay = json.loads(
            (root / "payload" / "ui" / "0.2.1" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(overlay["target_version"], "0.2.1")
        sidebar = next(
            entry
            for entry in overlay["files"]
            if entry["path"] == "frontend/src/components/layout/AppSidebar.vue"
        )
        self.assertEqual(
            sidebar["source_sha256"],
            "38ed7dc6433d4ba15bb95c4b686b54189cdff00764540fa3b36e1d855e357c80",
        )

        replay5 = next(
            entry
            for entry in replay_manifest["replays"]
            if entry["id"] == "codexrip-0.2.1.5"
        )
        self.assertEqual(replay5["source"]["repository"], "kiasd/sub2api-overdraft-auto-builder")
        self.assertEqual(replay5["source"]["branch"], "fusion-proof-v0.2.1-codexrip.5")
        self.assertEqual(replay5["source"]["version"], "0.2.1-codexrip.5")
        self.assertEqual(replay5["source"]["commit"], "b53ea7ffc4faf11ed06fedafbb97fa538d8accb1")
        self.assertEqual(replay5["overdraft_revision"], 5)
        replay5_path = root / replay5["patch"]["path"]
        self.assertEqual(
            detect_updates.sha256_file(replay5_path), replay5["patch"]["sha256"]
        )

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

    def test_overlay_source_state_ignores_windows_line_endings_for_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "frontend" / "style.css"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b".panel {\n  color: red;\n}\n")
            entry = {"source_sha256": build_candidate.source_sha256(destination)}

            destination.write_bytes(b".panel {\r\n  color: red;\r\n}\r\n")
            build_candidate.verify_overlay_source_state(
                destination, entry, Path("frontend/style.css")
            )

    def test_path_traversal_is_rejected(self):
        for value in ("", ".", "../secret", "frontend/../../secret"):
            with self.subTest(value=value):
                with self.assertRaises(build_candidate.BuildError):
                    build_candidate.safe_relative_path(value)

    def test_remote_skill_seed_adapts_unicode_embed_names_without_changing_logical_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "backend" / "internal" / "service" / "remote_skill_seed" / "tree" / "docs"
            tree.mkdir(parents=True)
            (tree / "u-认证.md").write_text("ok", encoding="utf-8")
            seed = tree.parent.parent
            (seed / "manifest.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "docs/认证.md",
                                "source_kind": "upstream",
                                "byte_length": 2,
                                "sha256": "2689367b205c16ce32ed4200942b8b8b1e262dfc70d9bc9fbc77c49699a4f1df",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            registry = root / "backend" / "internal" / "service" / "remote_skill_registry_manifest.go"
            registry.write_text(
                "\t\tcase \"upstream\":\n"
                "\t\t\tupstreamCount++\n"
                "\t\t\tif entry.EmbeddedPath != \"\" || entry.Provenance != nil {\n"
                "\t\t\t\treturn fmt.Errorf(\"%w: upstream manifest entry has pinned metadata\", ErrBusinessSystemPromptBundleInvalid)\n"
                "\t\t\t}\n"
                "\t\t\tbody, ok = upstreamFiles[entry.Path]\n"
                "\t\t}\n"
                "\tfor name := range upstreamFiles {\n"
                "\t\tif _, ok := files[name]; !ok {\n"
                "\t\t\treturn fmt.Errorf(\"%w: undeclared embedded upstream file\", ErrBusinessSystemPromptBundleInvalid)\n"
                "\t\t}\n"
                "\t}",
                encoding="utf-8",
            )

            result = build_candidate.adapt_remote_skill_seed_for_go_embed(root)
            self.assertEqual(result["remote_skill_embed_adaptation"], "ascii-physical-names")
            manifest = json.loads((seed / "manifest.json").read_text(encoding="utf-8"))
            entry = manifest["files"][0]
            self.assertEqual(entry["path"], "docs/认证.md")
            self.assertTrue(entry["embedded_path"].startswith("tree/docs/__unicode_"))
            self.assertTrue((seed / entry["embedded_path"]).is_file())
            rewritten = registry.read_text(encoding="utf-8")
            self.assertIn("lookupPath := entry.Path", rewritten)
            self.assertIn("declaredEmbedded := make(map[string]struct{}", rewritten)
            self.assertIn("if _, ok := declaredEmbedded[name]; !ok", rewritten)

    def test_remote_skill_seed_adapts_the_021_tuple_loader_shape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "backend" / "internal" / "service" / "remote_skill_seed" / "tree" / "docs"
            tree.mkdir(parents=True)
            (tree / "u-认证.md").write_text("ok", encoding="utf-8")
            seed = tree.parent.parent
            (seed / "manifest.json").write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "path": "docs/认证.md",
                                "source_kind": "upstream",
                                "byte_length": 2,
                                "sha256": "2689367b205c16ce32ed4200942b8b8b1e262dfc70d9bc9fbc77c49699a4f1df",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            registry = root / "backend" / "internal" / "service" / "remote_skill_registry_manifest.go"
            registry.write_text(
                "\t\tcase \"upstream\":\n"
                "\t\t\tupstreamCount++\n"
                "\t\t\tif entry.EmbeddedPath != \"\" || entry.Provenance != nil {\n"
                "\t\t\t\treturn fmt.Errorf(\"%w: upstream manifest entry has pinned metadata\", ErrBusinessSystemPromptBundleInvalid)\n"
                "\t\t\t}\n"
                "\t\t\tbody, ok = upstreamFiles[entry.Path]\n"
                "\t}\n"
                "\tfor name := range upstreamFiles {\n"
                "\t\tif _, ok := files[name]; !ok {\n"
                "\t\t\treturn remoteSkillManifest{}, nil, fmt.Errorf(\"%w: undeclared embedded upstream file\", ErrBusinessSystemPromptBundleInvalid)\n"
                "\t\t}\n"
                "\t}",
                encoding="utf-8",
            )

            result = build_candidate.adapt_remote_skill_seed_for_go_embed(root)
            self.assertEqual(result["remote_skill_embed_adaptation"], "ascii-physical-names")
            rewritten = registry.read_text(encoding="utf-8")
            self.assertIn("return remoteSkillManifest{}, nil, fmt.Errorf", rewritten)
            self.assertIn("declaredEmbedded := make(map[string]struct{}", rewritten)

    def test_replay_base_is_fetched_from_the_full_shallow_clone(self):
        commands: list[list[str]] = []

        def fake_run(command, **_kwargs):
            commands.append(list(command))
            return None

        original_run = build_candidate.run
        build_candidate.run = fake_run
        try:
            build_candidate.fetch_replay_base(Path("/tmp/source"), "a" * 40)
        finally:
            build_candidate.run = original_run

        self.assertEqual(
            commands,
            [["git", "fetch", "--depth=1", "origin", "a" * 40]],
        )

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
            definition = self.make_build_definition(root)
            result = detect_updates.resolve_snapshot(root, FakeGitHubClient())
            identity = detect_updates.release_identity_sha256(
                result, definition
            )
            self.assertEqual(result["release_version"], "0.1.178-overdraft.1")
            self.assertEqual(
                result["release_tag"],
                f"fusion-v0.1.178-overdraft.1-eeeeeeee-ffffffff-u{identity[:8]}",
            )
            self.assertEqual(result["builder"]["definition_sha256"], definition)
            self.assertRegex(result["fingerprint"], r"^[0-9a-f]{64}$")

    def test_builder_definition_change_generates_a_distinct_release_tag(self):
        for relative in detect_updates.BUILD_DEFINITION_PATHS:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.make_overlay(root, "0.1.178")
                self.make_build_definition(root)
                first = detect_updates.resolve_snapshot(root, FakeGitHubClient())

                (root / relative).write_text("definition: changed\n", encoding="utf-8")
                second = detect_updates.resolve_snapshot(root, FakeGitHubClient())

                self.assertNotEqual(first["builder"], second["builder"])
                self.assertNotEqual(first["release_tag"], second["release_tag"])
                self.assertNotEqual(first["fingerprint"], second["fingerprint"])

    def test_overlay_provenance_changes_release_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.178")
            definition = self.make_build_definition(root)
            snapshot = detect_updates.resolve_snapshot(root, FakeGitHubClient())
            replayed_inputs = dict(snapshot)
            replayed_overlay = dict(snapshot["overlay"])
            replayed_overlay.update({"mode": "forward-replay", "source_version": "0.1.177"})
            replayed_inputs["overlay"] = replayed_overlay

            self.assertNotEqual(
                detect_updates.release_identity_sha256(snapshot, definition),
                detect_updates.release_identity_sha256(replayed_inputs, definition),
            )

    def test_snapshot_rejects_fork_based_on_newer_official(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.178")
            self.make_build_definition(root)
            with self.assertRaises(detect_updates.DetectionError):
                detect_updates.resolve_snapshot(
                    root, FakeGitHubClient("0.1.179-overdraft.1")
                )

    def test_approved_custom_replay_uses_the_official_target_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            entry = self.make_custom_replay(root)
            definition = self.make_build_definition(root)
            snapshot = detect_updates.resolve_snapshot(
                root,
                FakeGitHubClient(
                    "0.1.184-custom.1",
                    official_version="0.1.185",
                    official_commit=entry["target"]["commit"],
                    fork_commit=entry["source"]["commit"],
                    base_commits={"0.1.183": entry["source"]["base_commit"]},
                ),
            )

            self.assertEqual(snapshot["release_version"], "0.1.185-overdraft.1")
            self.assertEqual(snapshot["fork"]["base_version"], "0.1.183")
            self.assertEqual(snapshot["replay"]["mode"], "approved-resolved-patch")
            self.assertEqual(snapshot["replay"]["id"], entry["id"])
            self.assertEqual(
                snapshot["replay"]["entry_sha256"],
                detect_updates.canonical_json_sha256(entry),
            )
            self.assertNotEqual(
                snapshot["release_tag"],
                f"fusion-v0.1.185-overdraft.1-aaaaaaaa-bbbbbbbb-u{definition[:8]}",
            )

    def test_custom_replay_without_an_exact_approval_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            replay_manifest = root / "payload" / "fork-replays" / "manifest.json"
            replay_manifest.parent.mkdir(parents=True, exist_ok=True)
            replay_manifest.write_text('{"schema": 1, "replays": []}', encoding="utf-8")
            self.make_build_definition(root)
            with self.assertRaisesRegex(
                detect_updates.DetectionError, "approved resolved replay"
            ):
                detect_updates.resolve_snapshot(
                    root,
                    FakeGitHubClient(
                        "0.1.184-custom.1",
                        official_version="0.1.185",
                        official_commit="a" * 40,
                        fork_commit="b" * 40,
                        base_commits={"0.1.183": "c" * 40},
                    ),
                )

    def test_custom_replay_rejects_a_changed_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            entry = self.make_custom_replay(root)
            self.make_build_definition(root)
            (root / "payload" / "fork-replays" / "resolved.patch").write_text(
                "changed", encoding="utf-8"
            )
            with self.assertRaisesRegex(detect_updates.DetectionError, "checksum"):
                detect_updates.resolve_snapshot(
                    root,
                    FakeGitHubClient(
                        "0.1.184-custom.1",
                        official_version="0.1.185",
                        official_commit=entry["target"]["commit"],
                        fork_commit=entry["source"]["commit"],
                        base_commits={"0.1.183": entry["source"]["base_commit"]},
                    ),
                )

    def test_custom_replay_requires_the_exact_official_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            entry = self.make_custom_replay(root, target_commit="a" * 40)
            self.make_build_definition(root)
            with self.assertRaisesRegex(
                detect_updates.DetectionError, "approved resolved replay"
            ):
                detect_updates.resolve_snapshot(
                    root,
                    FakeGitHubClient(
                        "0.1.184-custom.1",
                        official_version="0.1.185",
                        official_commit="e" * 40,
                        fork_commit=entry["source"]["commit"],
                        base_commits={"0.1.183": entry["source"]["base_commit"]},
                    ),
                )

    def test_custom_replay_rejects_a_retagged_base_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            entry = self.make_custom_replay(root)
            self.make_build_definition(root)
            with self.assertRaisesRegex(detect_updates.DetectionError, "base tag"):
                detect_updates.resolve_snapshot(
                    root,
                    FakeGitHubClient(
                        "0.1.184-custom.1",
                        official_version="0.1.185",
                        official_commit=entry["target"]["commit"],
                        fork_commit=entry["source"]["commit"],
                        base_commits={"0.1.183": "e" * 40},
                    ),
                )

    def test_replay_manifest_change_generates_a_distinct_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            entry = self.make_custom_replay(root)
            self.make_build_definition(root)
            client = FakeGitHubClient(
                "0.1.184-custom.1",
                official_version="0.1.185",
                official_commit=entry["target"]["commit"],
                fork_commit=entry["source"]["commit"],
                base_commits={"0.1.183": entry["source"]["base_commit"]},
            )
            first = detect_updates.resolve_snapshot(root, client)
            manifest_path = root / "payload" / "fork-replays" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["replays"][0]["overdraft_revision"] = 2
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            second = detect_updates.resolve_snapshot(root, client)

            self.assertNotEqual(first["fingerprint"], second["fingerprint"])
            self.assertNotEqual(first["release_tag"], second["release_tag"])
            self.assertEqual(second["release_version"], "0.1.185-overdraft.2")

    def test_builder_rejects_custom_detection_without_a_local_approval(self):
        detection = {
            "official": {"version": "0.1.185", "commit": "a" * 40},
            "fork": {
                "flavor": "custom",
                "base_version": "0.1.183",
                "base_commit": "c" * 40,
            },
        }
        with self.assertRaisesRegex(build_candidate.BuildError, "approved resolved replay"):
            build_candidate.prepare_source(Path("/tmp"), detection)

    def test_builder_applies_only_the_locked_resolved_patch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_overlay(root, "0.1.185")
            entry = self.make_custom_replay(root)
            self.make_build_definition(root)
            detection = detect_updates.resolve_snapshot(
                root,
                FakeGitHubClient(
                    "0.1.184-custom.1",
                    official_version="0.1.185",
                    official_commit=entry["target"]["commit"],
                    fork_commit=entry["source"]["commit"],
                    base_commits={"0.1.183": entry["source"]["base_commit"]},
                ),
            )
            source = root / "source"
            source.mkdir()
            commands: list[list[str]] = []
            original_root = build_candidate.ROOT
            original_clone = build_candidate.manager.clone_official_source
            original_run = build_candidate.run

            def fake_clone(*_args, **_kwargs):
                return source, "official-tree"

            def fake_run(command, **_kwargs):
                commands.append([str(part) for part in command])
                return ""

            build_candidate.ROOT = root
            build_candidate.manager.clone_official_source = fake_clone
            build_candidate.run = fake_run
            try:
                _, provenance = build_candidate.prepare_approved_replay(root, detection)
            finally:
                build_candidate.ROOT = original_root
                build_candidate.manager.clone_official_source = original_clone
                build_candidate.run = original_run

            self.assertEqual(provenance["fork_replay_id"], entry["id"])
            self.assertIn(
                [
                    "git",
                    "apply",
                    "--check",
                    "--whitespace=nowarn",
                    str(root / "payload" / "fork-replays" / "resolved.patch"),
                ],
                commands,
            )
            self.assertFalse(
                any(command[:3] == ["git", "diff", "--binary"] for command in commands)
            )


if __name__ == "__main__":
    unittest.main()
