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
    @staticmethod
    def release_check(
        *,
        has_update=True,
        official_ahead=False,
        workflow_status="completed",
        workflow_conclusion="success",
        payload=b"candidate",
    ):
        digest = manager.hashlib.sha256(payload).hexdigest()
        return {
            "current_version": "0.1.178-overdraft.1",
            "current_channel": "overdraft",
            "has_update": has_update,
            "official_ahead": official_ahead,
            "official": {
                "version": "0.1.179" if official_ahead else "0.1.178",
                "html_url": "https://github.com/Wei-Shaw/sub2api/releases/latest",
            },
            "workflow": {
                "status": workflow_status,
                "conclusion": workflow_conclusion,
                "failed": workflow_status == "completed" and workflow_conclusion not in {"success", "skipped"},
                "html_url": "https://github.com/kiasd/sub2api-overdraft-auto-builder/actions/runs/1",
            },
            "release": {
                "version": "0.1.178-overdraft.1",
                "tag": "fusion-v0.1.178-overdraft.1-e0c48a19-45e40b4f-u0bbd2518",
                "source_commit": "a" * 40,
                "official_version": "0.1.178",
                "binary_sha256": digest,
                "binary_size": len(payload),
                "binary_url": "https://github.com/kiasd/sub2api-overdraft-auto-builder/releases/download/test/sub2api",
                "html_url": "https://github.com/kiasd/sub2api-overdraft-auto-builder/releases/tag/test",
            },
        }

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
            self.assertEqual(result["ui_overlay_files"], "17")
            self.assertTrue((source / "frontend/src/style.css").is_file())
            self.assertTrue(
                (
                    source
                    / "frontend/src/components/admin/account/AccountTableFilters.vue"
                ).is_file()
            )
            self.assertTrue(
                (
                    source
                    / "frontend/src/components/user/dashboard/UserDashboardStats.vue"
                ).is_file()
            )
            self.assertTrue((source / "frontend/src/components/layout/AppSidebar.vue").is_file())
            self.assertTrue((source / "frontend/src/views/admin/DashboardView.vue").is_file())
            with self.assertRaises(manager.ManagerError):
                manager.select_ui_overlay("0.1.180")

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

    def test_ui_version_badge_is_official_and_read_only(self):
        sidebar = (
            ROOT
            / "payload/ui/0.1.178/frontend/src/components/layout/AppSidebar.vue"
        ).read_text(encoding="utf-8")
        component = (
            ROOT
            / "payload/ui/0.1.178/frontend/src/components/common/OfficialVersionBadge.vue"
        ).read_text(encoding="utf-8")
        helper = (
            ROOT / "payload/ui/0.1.178/frontend/src/components/common/officialVersion.ts"
        ).read_text(encoding="utf-8")

        self.assertIn("<OfficialVersionBadge", sidebar)
        self.assertNotIn("@/components/common/VersionBadge.vue", sidebar)
        self.assertIn("Wei-Shaw/sub2api", helper)
        self.assertIn("releases/latest", helper)
        self.assertIn("officialBaseVersion", component)
        self.assertIn("version.refresh", component)
        self.assertIn("version.viewRelease", component)
        self.assertNotIn("version.viewUpdate", component)
        for write_action in (
            "updateAPI",
            "performUpdate",
            "restartService",
            "getRollbackVersions",
            "rollbackAPI",
            "copyToClipboard",
        ):
            self.assertNotIn(write_action, component)

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

    def test_program_backup_excludes_only_cache_and_atomic_temporary_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            config = root / "config"
            state = root / "state"
            plugin = install / "plugins" / "weekly-overdraft"
            cache = plugin / "__pycache__"
            cache.mkdir(parents=True)
            config.mkdir()
            state.mkdir()
            (install / "sub2api").write_bytes(b"binary")
            (plugin / "manager.py").write_text("important", encoding="utf-8")
            (plugin / "module.pyc").write_bytes(b"cache")
            (cache / ".manager.py.3005eea.cpython-313.pyc").write_bytes(b"cache")
            (install / ".sub2api.150909.new").write_bytes(b"partial")
            (plugin / ".manager.py.deploy.tmp").write_bytes(b"partial")
            (config / "manager.env").write_text("important=true\n", encoding="utf-8")
            (config / ".manager.env.deploy.download").write_bytes(b"partial")
            archive = state / "program.tar.gz"

            with mock.patch.object(manager, "install_root", return_value=install), \
                mock.patch.object(manager, "config_root", return_value=config), \
                mock.patch.object(manager, "state_root", return_value=state):
                manager.backup_program(archive)

            with tarfile.open(archive, "r:gz") as bundle:
                names = set(bundle.getnames())
            self.assertIn("opt/sub2api/sub2api", names)
            self.assertIn("opt/sub2api/plugins/weekly-overdraft/manager.py", names)
            self.assertIn("etc/sub2api/manager.env", names)
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertFalse(any(name.endswith((".pyc", ".pyo")) for name in names))
            self.assertNotIn("opt/sub2api/.sub2api.150909.new", names)
            self.assertNotIn(
                "opt/sub2api/plugins/weekly-overdraft/.manager.py.deploy.tmp",
                names,
            )
            self.assertNotIn("etc/sub2api/.manager.env.deploy.download", names)

    def test_program_backup_does_not_hide_important_permission_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            config = root / "config"
            state = root / "state"
            install.mkdir()
            config.mkdir()
            state.mkdir()
            bundle = mock.MagicMock()
            bundle.__enter__.return_value = bundle
            bundle.add.side_effect = PermissionError("important file is unreadable")

            with mock.patch.object(manager, "install_root", return_value=install), \
                mock.patch.object(manager, "config_root", return_value=config), \
                mock.patch.object(manager, "state_root", return_value=state), \
                mock.patch.object(manager.tarfile, "open", return_value=bundle):
                with self.assertRaisesRegex(PermissionError, "important file is unreadable"):
                    manager.backup_program(state / "program.tar.gz")

            self.assertIs(bundle.add.call_args.kwargs["filter"], manager.program_backup_filter)

    def test_create_backup_marks_partial_archive_as_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            binary = Path(temporary) / "sub2api"
            binary.write_bytes(b"current-binary")

            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": str(state)}), \
                mock.patch.object(manager, "binary_path", return_value=binary), \
                mock.patch.object(
                    manager,
                    "backup_program",
                    side_effect=PermissionError("important file is unreadable"),
                ), mock.patch.object(manager, "dump_database") as dump:
                with self.assertRaisesRegex(PermissionError, "important file is unreadable"):
                    manager.create_backup("0.1.178", "0.1.179")

            dump.assert_not_called()
            backup_directories = list((state / "backups").iterdir())
            self.assertEqual(len(backup_directories), 1)
            metadata = manager.read_json(backup_directories[0] / "metadata.json", {})
            self.assertEqual(metadata["status"], "failed")
            self.assertIs(metadata["recoverable"], False)
            self.assertIn("important file is unreadable", metadata["error"])
            self.assertTrue((backup_directories[0] / "sub2api").is_file())

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

    def test_stage_rollback_persists_intent_before_binary_restore(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            backup = state / "backups" / "backup"
            backup.mkdir(parents=True)
            binary = backup / "sub2api"
            binary.write_bytes(b"old-binary")
            (backup / "database.dump").write_bytes(b"database")
            metadata = {
                "backup_id": "backup",
                "from_version": "0.1.178",
                "from_commit": "c" * 40,
                "from_channel": "official",
                "binary_sha256": manager.sha256_file(binary),
                "status": "ready",
            }
            manager.write_json_atomic(backup / "metadata.json", metadata)
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}), \
                mock.patch.object(manager, "atomic_replace_binary") as replace:
                result = manager.stage_rollback("previous", reason="health_check_failed")

            self.assertEqual(result["status"], "rollback_staged")
            replace.assert_not_called()
            pending = manager.read_json(state / "pending-rollback.json", {})
            self.assertEqual(pending["phase"], "rollback_pending")
            self.assertEqual(Path(pending["binary_backup"]), binary.resolve())
            self.assertEqual(pending["reason"], "health_check_failed")

    def test_auto_update_disabled_does_not_check_or_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.set_auto_update(False)
                with mock.patch.object(manager, "check_update") as check, mock.patch.object(manager, "upgrade") as upgrade:
                    result = manager.auto_run()
                self.assertEqual(result["status"], "disabled")
                check.assert_not_called()
                upgrade.assert_not_called()

    def test_auto_update_no_update_does_not_download_or_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = self.release_check(has_update=False)
                with mock.patch.object(manager, "check_update", return_value=result), \
                    mock.patch.object(manager, "prepare_release_candidate") as download, \
                    mock.patch.object(manager, "prepare_candidate") as local_build:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "no_update")
                download.assert_not_called()
                local_build.assert_not_called()

    def test_auto_update_waits_when_official_release_is_ahead(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = self.release_check(official_ahead=True)
                with mock.patch.object(manager, "check_update", return_value=result), \
                    mock.patch.object(manager, "prepare_release_candidate") as download, \
                    mock.patch.object(manager, "prepare_candidate") as local_build:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "waiting_builder")
                self.assertIn("官方 0.1.179", output["stage"])
                download.assert_not_called()
                local_build.assert_not_called()

    def test_auto_update_reports_builder_in_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = self.release_check(workflow_status="in_progress", workflow_conclusion="")
                with mock.patch.object(manager, "check_update", return_value=result), \
                    mock.patch.object(manager, "prepare_release_candidate") as download, \
                    mock.patch.object(manager, "prepare_candidate") as local_build:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "building")
                download.assert_not_called()
                local_build.assert_not_called()

    def test_auto_update_reports_builder_failure_and_preserves_server(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                result = self.release_check(workflow_conclusion="failure")
                with mock.patch.object(manager, "check_update", return_value=result), \
                    mock.patch.object(manager, "prepare_release_candidate") as download, \
                    mock.patch.object(manager, "prepare_candidate") as local_build, \
                    mock.patch.object(manager, "atomic_replace_binary") as replace:
                    output = manager.auto_run()
                self.assertEqual(output["status"], "failed")
                self.assertEqual(output["last_result"], "build_failed")
                download.assert_not_called()
                local_build.assert_not_called()
                replace.assert_not_called()

    def test_auto_update_downloads_verified_release_without_local_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            payload = b"candidate"
            result = self.release_check(payload=payload)

            def fake_download(_url, destination):
                destination.write_bytes(payload)

            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}), \
                mock.patch.object(manager, "check_update", return_value=result), \
                mock.patch.object(manager, "download", side_effect=fake_download) as download, \
                mock.patch.object(manager, "prepare_candidate") as local_build:
                output = manager.auto_run()
            self.assertEqual(output["status"], "ready")
            self.assertEqual(output["prepared"]["source_mode"], "verified-github-release")
            download.assert_called_once()
            local_build.assert_not_called()

    def test_prepare_release_candidate_rejects_size_and_checksum_mismatch(self):
        for mismatch in ("size", "checksum"):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary:
                result = self.release_check(payload=b"candidate")
                downloaded = b"short" if mismatch == "size" else b"different"

                def fake_download(_url, destination):
                    destination.write_bytes(downloaded)

                with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}), \
                    mock.patch.object(manager, "download", side_effect=fake_download):
                    with self.assertRaisesRegex(manager.ManagerError, f"{mismatch} mismatch"):
                        manager.prepare_release_candidate(result)

    def test_check_update_detects_official_release_ahead(self):
        with mock.patch.object(manager, "current_build", return_value={"version": "0.1.178", "commit": "c" * 40}), \
            mock.patch.object(manager, "builder_workflow_status", return_value={"status": "completed", "conclusion": "success"}), \
            mock.patch.object(manager, "latest_verified_release", return_value=self.release_check()["release"]), \
            mock.patch.object(manager, "official_release_notice", return_value={"version": "0.1.179", "html_url": "https://github.com/Wei-Shaw/sub2api/releases/latest"}):
            result = manager.check_update()
        self.assertIs(result["official_ahead"], True)

    def test_check_update_matches_installed_binary_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "sub2api"
            binary.write_bytes(b"candidate")
            release = self.release_check(payload=b"candidate")["release"]
            with mock.patch.object(manager, "binary_path", return_value=binary), \
                mock.patch.object(manager, "current_build", return_value={"version": release["version"], "commit": "c" * 40}), \
                mock.patch.object(manager, "builder_workflow_status", return_value={"status": "completed", "conclusion": "success"}), \
                mock.patch.object(manager, "latest_verified_release", return_value=release), \
                mock.patch.object(manager, "official_release_notice", return_value={"version": "0.1.178", "html_url": "https://github.com/Wei-Shaw/sub2api/releases/latest"}):
                result = manager.check_update()
        self.assertIs(result["has_update"], False)

    def test_latest_verified_release_rejects_untrusted_author_metadata_and_checksum(self):
        tag = "fusion-v0.1.178-overdraft.1-e0c48a19-45e40b4f-u0bbd2518"
        digest = manager.hashlib.sha256(b"candidate").hexdigest()
        release = {
            "tag_name": tag,
            "author": {"login": "github-actions[bot]"},
            "assets": [
                {
                    "name": "build-metadata.json",
                    "browser_download_url": "https://github.com/kiasd/repo/releases/download/test/build-metadata.json",
                    "size": 500,
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": "https://github.com/kiasd/repo/releases/download/test/SHA256SUMS",
                    "size": 200,
                },
                {
                    "name": "sub2api",
                    "browser_download_url": "https://github.com/kiasd/repo/releases/download/test/sub2api",
                    "size": len(b"candidate"),
                },
            ],
        }
        metadata = {
            "status": "verified",
            "release_tag": tag,
            "release_version": "0.1.178-overdraft.1",
            "build": {"tests": "passed", "binary_sha256": digest},
            "inputs": {
                "fork": {"commit": "a" * 40},
                "official": {"version": "0.1.178", "commit": "b" * 40},
            },
        }
        metadata_text = json.dumps(metadata, sort_keys=True)

        untrusted = dict(release)
        untrusted["author"] = {"login": "someone-else"}
        with mock.patch.object(manager, "fetch_json", return_value=untrusted):
            with self.assertRaisesRegex(manager.ManagerError, "GitHub Actions"):
                manager.latest_verified_release()

        pending_metadata = json.dumps(dict(metadata, status="pending"), sort_keys=True)
        with mock.patch.object(manager, "fetch_json", return_value=release), \
            mock.patch.object(manager, "fetch_small_text", return_value=pending_metadata):
            with self.assertRaisesRegex(manager.ManagerError, "not verified"):
                manager.latest_verified_release()

        sums = f"{'0' * 64}  build-metadata.json\n{digest}  sub2api\n"
        with mock.patch.object(manager, "fetch_json", return_value=release), \
            mock.patch.object(manager, "fetch_small_text", side_effect=[metadata_text, sums]):
            with self.assertRaisesRegex(manager.ManagerError, "metadata checksum"):
                manager.latest_verified_release()

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
                    def assert_switch_intent(_artifact):
                        pending = manager.read_json(state / "pending-upgrade.json", {})
                        self.assertEqual(pending["phase"], "switch_pending")
                        self.assertEqual(pending["target_binary_sha256"], prepared["binary_sha256"])

                    replace.side_effect = assert_switch_intent
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

    def test_apply_prepared_preserves_ready_candidate_when_backup_fails_before_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            artifact = state / "releases" / "candidate" / "sub2api"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"candidate")
            prepared = {
                "status": "ready",
                "version": "0.1.179-overdraft.1",
                "channel": "overdraft",
                "source_commit": "a" * 40,
                "artifact": str(artifact),
                "binary_sha256": manager.sha256_file(artifact),
            }
            backup_dir = state / "backups" / "backup"
            backup_dir.mkdir(parents=True)
            backup_meta = {"backup_id": "backup", "status": "preflight"}
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                with mock.patch.object(
                    manager,
                    "current_build",
                    return_value={"version": "0.1.178", "commit": "c" * 40},
                ), mock.patch.object(
                    manager,
                    "create_backup",
                    return_value=("backup", backup_dir, backup_meta),
                ), mock.patch.object(
                    manager,
                    "finalize_database_backup",
                    side_effect=manager.ManagerError("database backup failed"),
                ), mock.patch.object(manager, "atomic_replace_binary") as replace:
                    with self.assertRaisesRegex(manager.ManagerError, "database backup failed"):
                        manager.apply_prepared()

                replace.assert_not_called()
                self.assertEqual(manager.prepared_update(), prepared)
                status = manager.auto_update_status()
                self.assertEqual(status["status"], "apply_failed")
                self.assertEqual(status["last_result"], "apply_failed")
                self.assertEqual(status["progress"], 45)
                self.assertEqual(status["prepared"]["status"], "ready")
                self.assertIn("database backup failed", status["last_error"])
                self.assertFalse((state / "pending-upgrade.json").exists())

    def test_queue_apply_binds_the_confirmed_release_and_blocks_monitor(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            artifact = state / "releases" / "candidate" / "sub2api"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"candidate")
            digest = manager.sha256_file(artifact)
            tag = "fusion-v0.1.179-overdraft.1-12345678-abcdef01-u87654321"
            prepared = {
                "status": "ready",
                "version": "0.1.179-overdraft.1",
                "channel": "overdraft",
                "release_tag": tag,
                "source_commit": "a" * 40,
                "artifact": str(artifact),
                "binary_sha256": digest,
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                queued = manager.queue_prepared_apply(tag, digest)
                with mock.patch.object(manager, "check_update") as check:
                    busy = manager.auto_run()

                self.assertEqual(queued["status"], "apply_queued")
                self.assertEqual(queued["apply_request"]["release_tag"], tag)
                self.assertEqual(queued["apply_request"]["binary_sha256"], digest)
                self.assertEqual(busy["status"], "apply_queued")
                self.assertIs(busy["busy"], True)
                check.assert_not_called()

                with self.assertRaisesRegex(manager.ManagerError, "已变化"):
                    manager.validate_prepared_identity(prepared, tag, "0" * 64)

    def test_queue_apply_resumes_only_the_matching_pending_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            artifact = state / "releases" / "candidate" / "sub2api"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"candidate")
            digest = manager.sha256_file(artifact)
            tag = "fusion-v0.1.179-overdraft.1-12345678-abcdef01-u87654321"
            prepared = {
                "status": "staged",
                "version": "0.1.179-overdraft.1",
                "channel": "overdraft",
                "release_tag": tag,
                "source_commit": "a" * 40,
                "artifact": str(artifact),
                "binary_sha256": digest,
            }
            pending = {
                "backup_id": "backup",
                "from_version": "0.1.178",
                "from_channel": "official",
                "to_version": prepared["version"],
                "channel": "overdraft",
                "release_tag": tag,
                "old_binary_sha256": "1" * 64,
                "target_binary_sha256": digest,
                "phase": "switched",
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                manager.write_json_atomic(state / "pending-upgrade.json", pending)
                manager.write_auto_update_status(
                    status="apply_failed",
                    prepared=prepared,
                )

                queued = manager.queue_prepared_apply(tag, digest)
                self.assertIs(queued["apply_request"]["resume_pending"], True)
                self.assertEqual(manager.auto_update_status()["status"], "apply_queued")
                self.assertEqual(
                    manager.read_json(state / "pending-upgrade.json", {}), pending
                )

                manager.write_auto_update_status(status="apply_failed")
                pending["target_binary_sha256"] = "0" * 64
                manager.write_json_atomic(state / "pending-upgrade.json", pending)
                with self.assertRaisesRegex(manager.ManagerError, "其他升级"):
                    manager.queue_prepared_apply(tag, digest)

    def test_apply_prepared_resumes_when_switch_intent_matches_disk_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            binary = state / "installed" / "sub2api"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"candidate")
            artifact = state / "releases" / "candidate" / "sub2api"
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"candidate")
            digest = manager.sha256_file(artifact)
            tag = "fusion-v0.1.179-overdraft.1-12345678-abcdef01-u87654321"
            prepared = {
                "status": "ready",
                "version": "0.1.179-overdraft.1",
                "channel": "overdraft",
                "release_tag": tag,
                "source_commit": "a" * 40,
                "artifact": str(artifact),
                "binary_sha256": digest,
            }
            pending = {
                "operation_id": "operation",
                "backup_id": "backup",
                "from_version": "0.1.178",
                "from_channel": "official",
                "to_version": prepared["version"],
                "channel": "overdraft",
                "source_commit": prepared["source_commit"],
                "release_tag": tag,
                "old_binary_sha256": "1" * 64,
                "target_binary_sha256": digest,
                "phase": "switch_pending",
                "staged_at": "2026-08-19T00:00:00+00:00",
            }
            with mock.patch.dict(
                os.environ,
                {"SUB2API_STATE_ROOT": temporary, "SUB2API_BINARY": str(binary)},
            ):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                manager.write_json_atomic(state / "pending-upgrade.json", pending)
                with mock.patch.object(manager, "create_backup") as backup, \
                    mock.patch.object(manager, "atomic_replace_binary") as replace:
                    result = manager.apply_prepared(tag, digest)

                self.assertEqual(result["status"], "staged")
                backup.assert_not_called()
                replace.assert_not_called()
                resumed = manager.read_json(state / "pending-upgrade.json", {})
                self.assertEqual(resumed["phase"], "switched")
                self.assertEqual(manager.auto_update_status()["progress"], 75)

    def test_post_start_check_records_updated_at_full_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            pending = {
                "backup_id": "backup",
                "from_version": "0.1.178",
                "to_version": "0.1.179-overdraft.1",
                "channel": "overdraft",
                "source_commit": "a" * 40,
                "staged_at": "2026-08-19T00:00:00+00:00",
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(state / "pending-upgrade.json", pending)
                manager.write_json_atomic(
                    manager.patch_state_path(pending["to_version"], pending["channel"]),
                    {"status": "staged"},
                )
                with mock.patch.object(manager, "health_check", return_value=True):
                    result = manager.post_start_check()

                self.assertEqual(result, {"status": "verified", "version": pending["to_version"]})
                status = manager.auto_update_status()
                self.assertEqual(status["status"], "updated")
                self.assertEqual(status["last_result"], "updated")
                self.assertEqual(status["progress"], 100)
                self.assertEqual(status["prepared"], {})
                self.assertFalse((state / "pending-upgrade.json").exists())
                self.assertEqual(
                    manager.read_json(state / "current.json", {})["status"],
                    "verified",
                )
                self.assertEqual(
                    manager.read_json(
                        manager.patch_state_path(pending["to_version"], pending["channel"]),
                        {},
                    )["status"],
                    "verified",
                )

    def test_post_start_old_hash_after_switch_stages_paired_database_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            installed = state / "installed" / "sub2api"
            installed.parent.mkdir(parents=True)
            installed.write_bytes(b"old-binary")
            old_hash = manager.sha256_file(installed)
            backup = state / "backups" / "backup"
            backup.mkdir(parents=True)
            (backup / "sub2api").write_bytes(b"old-binary")
            (backup / "database.dump").write_bytes(b"database")
            manager.write_json_atomic(
                backup / "metadata.json",
                {
                    "backup_id": "backup",
                    "from_version": "0.1.178",
                    "from_commit": "c" * 40,
                    "from_channel": "official",
                    "binary_sha256": old_hash,
                    "status": "ready",
                },
            )
            pending = {
                "backup_id": "backup",
                "from_version": "0.1.178",
                "from_channel": "official",
                "to_version": "0.1.179-overdraft.1",
                "channel": "overdraft",
                "source_commit": "a" * 40,
                "old_binary_sha256": old_hash,
                "target_binary_sha256": manager.hashlib.sha256(b"candidate").hexdigest(),
                "phase": "switched",
                "staged_at": "2026-08-19T00:00:00+00:00",
            }
            with mock.patch.dict(
                os.environ,
                {"SUB2API_STATE_ROOT": temporary, "SUB2API_BINARY": str(installed)},
            ):
                manager.write_json_atomic(state / "pending-upgrade.json", pending)
                with mock.patch.object(manager, "health_check") as health:
                    with self.assertRaisesRegex(manager.ManagerError, "automatic rollback staged"):
                        manager.post_start_check()

                health.assert_not_called()
                self.assertFalse((state / "pending-upgrade.json").exists())
                rollback = manager.read_json(state / "pending-rollback.json", {})
                self.assertEqual(rollback["backup_id"], "backup")
                self.assertEqual(rollback["reason"], "binary_mismatch")
                self.assertEqual(Path(rollback["database_dump"]), (backup / "database.dump").resolve())

    def test_apply_pending_records_rolled_back_at_full_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            dump = state / "backups" / "backup" / "database.dump"
            backup_binary = state / "backups" / "backup" / "sub2api"
            backup_binary.parent.mkdir(parents=True)
            backup_binary.write_bytes(b"old-binary")
            pending = {
                "backup_id": "backup",
                "target_version": "0.1.178",
                "target_commit": "c" * 40,
                "target_channel": "official",
                "binary_backup": str(backup_binary),
                "binary_sha256": manager.sha256_file(backup_binary),
                "database_dump": str(dump),
                "reason": "health_check_failed",
                "staged_at": "2026-08-19T00:00:00+00:00",
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(state / "pending-rollback.json", pending)
                with mock.patch.object(manager, "restore_database") as restore, \
                    mock.patch.object(manager, "atomic_replace_binary") as replace:
                    result = manager.apply_pending()

                self.assertEqual(result, {"status": "rollback_applied", "version": "0.1.178"})
                restore.assert_called_once_with(dump)
                replace.assert_called_once_with(backup_binary)
                status = manager.auto_update_status()
                self.assertEqual(status["status"], "rolled_back")
                self.assertEqual(status["last_result"], "rolled_back")
                self.assertEqual(status["progress"], 100)
                self.assertEqual(status["prepared"], {})
                self.assertFalse((state / "pending-rollback.json").exists())
                self.assertEqual(
                    manager.read_json(state / "current.json", {})["status"],
                    "rolled_back",
                )
                self.assertEqual(
                    manager.read_json(state / "last-rollback.json", {})["status"],
                    "applied",
                )

    def test_reconcile_marks_stale_inactive_apply_as_retryable_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_auto_update_status(
                    status="applying",
                    last_result="applying",
                    progress=45,
                    stage="正在生成切换前数据库恢复点",
                    last_started_at="2026-08-19T00:00:00+00:00",
                )
                result = manager.reconcile_apply_status()

                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["auto_update"]["status"], "apply_failed")
                self.assertEqual(result["auto_update"]["progress"], 45)
                self.assertIn("候选包仍可重新应用", result["auto_update"]["stage"])

    def test_worker_failure_with_pending_rollback_only_offers_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            prepared = {
                "status": "staged",
                "version": "0.1.179-overdraft.1",
                "binary_sha256": "f" * 64,
            }
            with mock.patch.dict(os.environ, {"SUB2API_STATE_ROOT": temporary}):
                manager.write_json_atomic(manager.prepared_update_path(), prepared)
                manager.write_json_atomic(
                    state / "pending-rollback.json",
                    {"backup_id": "backup", "phase": "rollback_pending"},
                )
                manager.write_auto_update_status(
                    status="rollback_pending",
                    progress=95,
                    prepared=prepared,
                )

                failed = manager.apply_worker_failed("worker stopped")

                self.assertEqual(failed["status"], "apply_failed")
                self.assertEqual(failed["prepared"], {})
                self.assertIn("重新执行回退", failed["stage"])


if __name__ == "__main__":
    unittest.main()
