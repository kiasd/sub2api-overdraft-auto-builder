import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "integration" / "install-backup-panel.sh"
PANEL_DROPIN = ROOT / "integration" / "backup-panel" / "weekly-overdraft-control.conf"


class BackupPanelInstallAssetTests(unittest.TestCase):
    def test_panel_cannot_write_the_sub2api_install_tree(self):
        writable_paths = []
        read_only_paths = []
        for raw_line in PANEL_DROPIN.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("ReadWritePaths="):
                writable_paths.extend(line.partition("=")[2].split())
            if line.startswith("ReadOnlyPaths="):
                read_only_paths.extend(line.partition("=")[2].split())

        self.assertEqual(writable_paths, ["/var/lib/sub2api-weekly-overdraft"])
        self.assertNotIn("/opt/sub2api", writable_paths)
        self.assertIn("/opt/sub2api", read_only_paths)
        self.assertIn("/etc/sub2api", read_only_paths)

    def test_installer_snapshots_every_replaced_live_file(self):
        script = INSTALLER.read_text(encoding="utf-8")
        expected_targets = (
            'PANEL=/opt/sub2api-backup-web/backup_web.py',
            'CONTROL=/usr/local/sbin/sub2api-plugin-control',
            'SUDOERS=/etc/sudoers.d/sub2api-plugin-control',
            'DROPIN="$DROPIN_DIR/weekly-overdraft-control.conf"',
            'APPLY_UNIT=/etc/systemd/system/sub2api-overdraft-apply.service',
            'APPLY_FAILED_UNIT=/etc/systemd/system/sub2api-overdraft-apply-failed.service',
        )
        for target in expected_targets:
            self.assertIn(target, script)

        transaction_targets = script.split("TRANSACTION_TARGETS=(", 1)[1].split(")", 1)[0]
        for variable in (
            "PANEL",
            "CONTROL",
            "SUDOERS",
            "DROPIN",
            "APPLY_UNIT",
            "APPLY_FAILED_UNIT",
        ):
            self.assertIn(f'"${variable}"', transaction_targets)

    def test_installer_uses_exit_rollback_for_command_and_health_failures(self):
        script = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("trap on_exit EXIT", script)
        self.assertIn("flock -n 9", script)
        self.assertIn("restore_transaction", script)
        self.assertIn("snapshot_targets\ntransaction_started=1", script)
        self.assertIn("systemctl daemon-reload", script)
        self.assertIn('systemctl restart "$PANEL_SERVICE"', script)
        self.assertIn("did not become healthy within 30 seconds", script)
        unhealthy_branch = script.split('if [[ "$panel_ready" -ne 1 ]]', 1)[1]
        self.assertIn("exit 1", unhealthy_branch)

    def test_installer_has_valid_bash_syntax(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is not available")
        result = subprocess.run(
            [bash, "-n", str(INSTALLER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
