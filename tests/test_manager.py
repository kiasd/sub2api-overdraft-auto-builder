import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("overdraft_manager", ROOT / "manager.py")
manager = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(manager)


class ManagerTests(unittest.TestCase):
    def test_normalize_version(self):
        self.assertEqual(manager.normalize_version("v0.1.177"), "0.1.177")
        self.assertEqual(
            manager.normalize_version("0.1.177-overdraft.6"),
            "0.1.177-overdraft.6",
        )
        with self.assertRaises(manager.ManagerError):
            manager.normalize_version("latest; rm -rf /")

    def test_validate_fork_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "FORK_VERSION").write_text("0.1.177-overdraft.6\n", encoding="utf-8")
            version_file = source / "backend" / "cmd" / "server" / "VERSION"
            version_file.parent.mkdir(parents=True)
            version_file.write_text("0.1.177\n", encoding="utf-8")
            result = manager.validate_fork_source(
                source,
                "0.1.177-overdraft.6",
                "a" * 64,
                "d" * 40,
            )
            self.assertEqual(result["source_commit"], "d" * 40)
            self.assertEqual(result["patch_mode"], "fork-native")
            with self.assertRaises(manager.ManagerError):
                manager.validate_fork_source(
                    source,
                    "0.1.177-overdraft.5",
                    "a" * 64,
                    "d" * 40,
                )

    def test_latest_fork_locks_version_to_commit(self):
        responses = [
            {"sha": "d" * 40},
            {
                "encoding": "base64",
                "content": "MC4xLjE3Ny1vdmVyZHJhZnQuNgo=",
            },
        ]
        with mock.patch.object(manager, "fetch_json", side_effect=responses):
            result = manager.latest_fork()
        self.assertEqual(result["version"], "0.1.177-overdraft.6")
        self.assertEqual(result["commit"], "d" * 40)

    def test_official_release_resolves_tag_commit(self):
        responses = [
            {
                "tag_name": "v0.1.178",
                "html_url": "https://github.com/Wei-Shaw/sub2api/releases/tag/v0.1.178",
                "name": "0.1.178",
            },
            {"sha": "e" * 40},
        ]
        with mock.patch.object(manager, "fetch_json", side_effect=responses):
            result = manager.latest_official()
        self.assertEqual(result["channel"], "official")
        self.assertEqual(result["version"], "0.1.178")
        self.assertEqual(result["commit"], "e" * 40)

    def test_patch_selection_uses_exact_official_baseline(self):
        base_version, patch_path, base_commit = manager.select_overdraft_patch("0.1.178")
        self.assertEqual(base_version, "0.1.178")
        self.assertEqual(patch_path.name, "sub2api-overdraft-v0.1.178-e0c48a19e.patch")
        self.assertEqual(base_commit, "e0c48a19ed794a565e3858662520afe0a1f9f0ba")

    def test_channel_for_version_distinguishes_legacy_fork(self):
        self.assertEqual(manager.channel_for_version("0.1.178"), "official")
        self.assertEqual(manager.channel_for_version("0.1.177-overdraft.6"), "overdraft")

    def test_validate_official_source_accepts_lagging_embedded_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            version_file = source / "backend" / "cmd" / "server" / "VERSION"
            version_file.parent.mkdir(parents=True)
            version_file.write_text("0.1.177\n", encoding="utf-8")
            result = manager.validate_official_source(
                source,
                "0.1.178",
                "a" * 40,
                "e" * 40,
            )
            self.assertEqual(result["source_mode"], "official")
            with self.assertRaises(manager.ManagerError):
                manager.validate_official_source(source, "0.1.176", "a" * 40, "e" * 40)

    def test_ui_overlay_is_exact_and_checksum_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            (source / "frontend").mkdir(parents=True)
            result = manager.apply_ui_overlay(source, "0.1.178")
            self.assertEqual(result["ui_overlay_id"], "anime-control-room-v1")
            self.assertEqual(result["ui_overlay_files"], "10")
            self.assertTrue((source / "frontend/src/style.css").is_file())
            self.assertTrue((source / "frontend/src/components/layout/AppSidebar.vue").is_file())
            self.assertTrue((source / "frontend/src/views/admin/DashboardView.vue").is_file())
            with self.assertRaises(manager.ManagerError):
                manager.select_ui_overlay("0.1.179")

    def test_ui_overlay_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            overlay_root = Path(temporary)
            version_root = overlay_root / "0.1.178"
            version_root.mkdir()
            (version_root / "manifest.json").write_text(
                json.dumps(
                    {
                        "target_version": "0.1.178",
                        "files": [{"path": "frontend/src/style.css", "sha256": "0" * 64}],
                    }
                ),
                encoding="utf-8",
            )
            (version_root / "frontend/src").mkdir(parents=True)
            (version_root / "frontend/src/style.css").write_text("tampered", encoding="utf-8")
            with mock.patch.object(manager, "UI_OVERLAY_DIR", overlay_root):
                with self.assertRaises(manager.ManagerError):
                    manager.select_ui_overlay("0.1.178")

    def test_frontend_test_command_only_excludes_known_upstream_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            frontend = Path(temporary)
            test_path = frontend / "src/components/account/__tests__/CreateAccountModal.grok.spec.ts"
            implementation = frontend / "src/components/account/CreateAccountModal.vue"
            test_path.parent.mkdir(parents=True)
            implementation.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_text("expect(source).toContain(\"? 'xai-...'\")", encoding="utf-8")
            implementation.write_text("return 'xai-...'", encoding="utf-8")
            command, exclusions = manager.frontend_test_command("pnpm", frontend)
            self.assertEqual(exclusions, ["src/components/account/__tests__/CreateAccountModal.grok.spec.ts"])
            self.assertEqual(command[-2:], ["--exclude", exclusions[0]])

    def test_sync_patch_catalog_keeps_branch_commit_and_hash(self):
        responses = [
            [
                {
                    "name": "sub2api-overdraft-v0.1.178-e0c48a19e.patch",
                    "url": "https://api.github.com/repos/DeanZFC/sub2api-overdraft/contents/patches/example",
                }
            ],
            {
                "encoding": "base64",
                "content": "cGF0Y2g=",
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                with mock.patch.object(manager, "fetch_json", side_effect=responses):
                    result = manager.sync_patch_catalog({"commit": "f" * 40})
                self.assertEqual(result["source_commit"], "f" * 40)
                self.assertEqual(result["count"], 1)
                self.assertEqual((Path(temporary) / "patches" / "sub2api-overdraft-v0.1.178-e0c48a19e.patch").read_bytes(), b"patch")
                catalog = manager.read_json(Path(temporary) / "patch-catalog.json", {})
                self.assertEqual(catalog["source_commit"], "f" * 40)

    def test_sync_patch_catalog_prunes_stale_cached_patches(self):
        responses = [
            [
                {
                    "name": "sub2api-overdraft-v0.1.178-e0c48a19e.patch",
                    "url": "https://api.github.com/repos/DeanZFC/sub2api-overdraft/contents/patches/example",
                }
            ],
            {"encoding": "base64", "content": "cGF0Y2g="},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            cache = state / "patches"
            cache.mkdir(parents=True)
            stale = cache / "sub2api-overdraft-v0.1.177-baeac1f3d.patch"
            stale.write_text("stale", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                with mock.patch.object(manager, "fetch_json", side_effect=responses):
                    manager.sync_patch_catalog({"commit": "f" * 40})
            self.assertFalse(stale.exists())
            self.assertTrue((cache / "sub2api-overdraft-v0.1.178-e0c48a19e.patch").exists())

    def test_current_build_reads_full_fork_version_and_commit(self):
        completed = mock.Mock(
            stdout=(
                "Sub2API 0.1.177-overdraft.6 "
                "(commit: d7716b3082f8e773b8f780e5cd8e9c11df51af1b, built: now)\n"
            )
        )
        with mock.patch.object(manager, "binary_path", return_value=ROOT / "manager.py"):
            with mock.patch.object(manager, "run", return_value=completed):
                result = manager.current_build()
        self.assertEqual(result["version"], "0.1.177-overdraft.6")
        self.assertEqual(result["commit"], "d7716b3082f8e773b8f780e5cd8e9c11df51af1b")

    def test_safe_extract_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as bundle:
                item = tarfile.TarInfo("../escape")
                payload = b"bad"
                item.size = len(payload)
                bundle.addfile(item, io.BytesIO(payload))
            with self.assertRaises(manager.ManagerError):
                manager.safe_extract_tar(archive, root / "extract")

    def test_temporary_work_directory_uses_platform_cleanup_policy(self):
        temporary = mock.MagicMock()
        temporary.__enter__.return_value = "temporary-path"
        with mock.patch.object(manager.tempfile, "TemporaryDirectory", return_value=temporary) as constructor:
            with manager.temporary_work_directory("verify-") as work:
                self.assertEqual(work, Path("temporary-path"))
        constructor.assert_called_once_with(
            prefix="verify-",
            ignore_cleanup_errors=os.name == "nt",
        )

    def test_overdraft_setting_parses_database_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                completed = mock.Mock(stdout="true\n")
                with mock.patch.object(manager, "run", return_value=completed):
                    self.assertIs(manager.overdraft_setting(), True)
                completed.stdout = "false\n"
                with mock.patch.object(manager, "run", return_value=completed):
                    self.assertIs(manager.overdraft_setting(), False)
                with mock.patch.object(
                    manager,
                    "run",
                    side_effect=manager.ManagerError("database unavailable"),
                ):
                    self.assertIsNone(manager.overdraft_setting())

    def test_overdraft_setting_prefers_runtime_env(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "runtime.env").write_text(
                "GATEWAY_CODEX_QUOTA_OVERDRAFT_ENABLED=true\n",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": str(state)}):
                self.assertIs(manager.overdraft_setting(), True)

    def test_atomic_replace_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "candidate"
            target = root / "install" / "sub2api"
            source.write_bytes(b"new-binary")
            target.parent.mkdir()
            target.write_bytes(b"old-binary")
            with mock.patch.dict(os.environ, {"SUB2API_BINARY": str(target)}):
                manager.atomic_replace_binary(source)
            self.assertEqual(target.read_bytes(), b"new-binary")

    def test_choose_backup_requires_ready_database_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            good = state / "backups" / "20260815T010000-0.1.176-to-0.1.177"
            bad = state / "backups" / "20260815T020000-0.1.175-to-0.1.176"
            good.mkdir(parents=True)
            bad.mkdir(parents=True)
            (good / "metadata.json").write_text(
                json.dumps({"backup_id": good.name, "from_version": "0.1.176", "status": "ready"}),
                encoding="utf-8",
            )
            (bad / "metadata.json").write_text(
                json.dumps({"backup_id": bad.name, "from_version": "0.1.175", "status": "preflight"}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": str(state)}):
                selected = manager.choose_backup("previous")
                self.assertEqual(selected["from_version"], "0.1.176")
                with self.assertRaises(manager.ManagerError):
                    manager.choose_backup("0.1.175")

    def test_auto_update_disabled_does_not_check_or_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.set_auto_update(False)
                with mock.patch.object(manager, "check_update") as check, mock.patch.object(manager, "upgrade") as upgrade:
                    result = manager.auto_run()
                self.assertEqual(result["status"], "disabled")
                check.assert_not_called()
                upgrade.assert_not_called()

    def test_auto_update_no_update_does_not_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = {
                    "current_version": "0.1.178",
                    "current_channel": "overdraft",
                    "channels": {
                        "official": {"version": "0.1.178", "commit": "a" * 40, "has_update": False},
                        "overdraft": {
                            "version": "0.1.178",
                            "commit": "a" * 40,
                            "has_update": False,
                            "patch_available": True,
                            "patch_sha256": "b" * 64,
                            "fork_source": {"commit": "c" * 40},
                        },
                    },
                }
                with mock.patch.object(manager, "check_update", return_value=result), mock.patch.object(manager, "upgrade") as upgrade:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "no_update")
                upgrade.assert_not_called()

    def test_auto_update_blocks_when_patch_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = {
                    "channels": {
                        "overdraft": {
                            "version": "0.1.179",
                            "commit": "a" * 40,
                            "has_update": True,
                            "patch_available": False,
                            "patch_error": "no compatible patch",
                        }
                    }
                }
                with mock.patch.object(manager, "check_update", return_value=result), mock.patch.object(manager, "upgrade") as upgrade:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "blocked")
                self.assertEqual(output["reason"], "patch_unavailable")
                upgrade.assert_not_called()

    def test_auto_update_prepares_build_when_patch_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = {
                    "channels": {
                        "official": {"version": "0.1.179", "commit": "a" * 40, "has_update": True},
                        "overdraft": {
                            "version": "0.1.179",
                            "commit": "a" * 40,
                            "has_update": True,
                            "patch_available": True,
                            "patch_sha256": "b" * 64,
                            "fork_source": {"commit": "c" * 40},
                        },
                    }
                }
                artifact = Path(temporary) / "prepared" / "sub2api"
                artifact.parent.mkdir(parents=True)
                artifact.write_bytes(b"candidate")
                prepared = {
                    "status": "ready",
                    "version": "0.1.179",
                    "channel": "overdraft",
                    "source_commit": "a" * 40,
                    "fork_commit": "c" * 40,
                    "patch_sha256": "b" * 64,
                    "artifact": str(artifact),
                    "binary_sha256": manager.sha256_file(artifact),
                }
                with mock.patch.object(manager, "check_update", return_value=result), mock.patch.object(manager, "prepare_candidate", return_value=prepared) as prepare:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "ready")
                prepare.assert_called_once_with(None, None, "overdraft", progress=mock.ANY)
                self.assertEqual(manager.auto_update_status()["prepared"], prepared)

    def test_apply_prepared_is_the_only_path_that_replaces_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            artifact = state / "releases" / "candidate" / "sub2api"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"candidate")
            prepared = {
                "status": "ready",
                "version": "0.1.179",
                "channel": "overdraft",
                "source_commit": "a" * 40,
                "artifact": str(artifact),
                "binary_sha256": manager.sha256_file(artifact),
                "patch_sha256": "b" * 64,
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                backup_dir = state / "backups" / "backup"
                backup_dir.mkdir(parents=True)
                metadata = {"backup_id": "backup", "status": "preflight"}
                with mock.patch.object(manager, "current_build", return_value={"version": "0.1.178", "commit": "c" * 40}), \
                    mock.patch.object(manager, "create_backup", return_value=("backup", backup_dir, metadata)), \
                    mock.patch.object(manager, "finalize_database_backup"), \
                    mock.patch.object(manager, "atomic_replace_binary") as replace:
                    result = manager.apply_prepared()
                self.assertEqual(result["status"], "staged")
                replace.assert_called_once_with(artifact)
                self.assertTrue((state / "pending-upgrade.json").is_file())

    def test_apply_prepared_rejects_a_candidate_older_than_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            artifact = state / "releases" / "candidate" / "sub2api"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"candidate")
            prepared = {
                "status": "ready",
                "version": "0.1.179",
                "channel": "overdraft",
                "source_commit": "a" * 40,
                "artifact": str(artifact),
                "binary_sha256": manager.sha256_file(artifact),
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                with mock.patch.object(
                    manager,
                    "current_build",
                    return_value={"version": "0.1.180", "commit": "c" * 40},
                ), mock.patch.object(manager, "create_backup") as backup:
                    with self.assertRaisesRegex(manager.ManagerError, "低于当前运行版本"):
                        manager.apply_prepared()
                backup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
