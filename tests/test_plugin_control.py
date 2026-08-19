import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL_PATH = ROOT / "integration" / "sub2api-plugin-control"
APPLY_UNIT_PATH = ROOT / "systemd" / "sub2api-overdraft-apply.service"


def shell_function(source: str, name: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index("\n}\n", start) + len("\n}")
    return source[start:end]


class PluginControlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.control = CONTROL_PATH.read_text(encoding="utf-8")
        cls.apply_unit = APPLY_UNIT_PATH.read_text(encoding="utf-8")

    def run_bash(self, script: str) -> subprocess.CompletedProcess[str]:
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not available on this platform")
        return subprocess.run(
            [bash, "-c", script],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_restart_health_failure_stages_rollback_before_retry(self):
        restart_is_healthy = shell_function(self.control, "restart_is_healthy")
        restart_with_fallback = shell_function(self.control, "restart_with_fallback")
        script = textwrap.dedent(
            f"""
            set -Eeuo pipefail
            calls=()
            active_checks=0
            systemctl() {{
              calls+=("systemctl $*")
              if [[ "$1" == "restart" ]]; then
                return 0
              fi
              active_checks=$((active_checks + 1))
              [[ "$active_checks" -ge 2 ]]
            }}
            run_manager() {{ calls+=("manager $*"); }}
            journalctl() {{ calls+=("journalctl $*"); }}
            {restart_is_healthy}
            {restart_with_fallback}
            restart_with_fallback yes
            printf '%s\n' "${{calls[@]}}"
            """
        )

        result = self.run_bash(script)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "systemctl restart sub2api.service",
                "systemctl is-active --quiet sub2api.service",
                "manager rollback previous --reason start_failure",
                "systemctl restart sub2api.service",
                "systemctl is-active --quiet sub2api.service",
            ],
        )

    def test_control_script_has_valid_bash_syntax(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not available on this platform")
        result = subprocess.run(
            [bash, "-n", str(CONTROL_PATH)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_control_lock_is_exclusive_across_processes(self):
        acquire_control_lock = shell_function(self.control, "acquire_control_lock")
        with tempfile.TemporaryDirectory() as temporary:
            lock_path = Path(temporary)
            holder_script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                CONTROL_LOCK_WAIT_SECONDS=1
                exec 9<'{lock_path.as_posix()}'
                {acquire_control_lock}
                acquire_control_lock
                printf 'locked\n'
                sleep 2
                """
            )
            contender_script = textwrap.dedent(
                f"""
                set -Eeuo pipefail
                CONTROL_LOCK_WAIT_SECONDS=1
                exec 9<'{lock_path.as_posix()}'
                {acquire_control_lock}
                acquire_control_lock
                """
            )
            bash = shutil.which("bash")
            if bash is None:
                self.skipTest("bash is not available on this platform")
            holder = subprocess.Popen(
                [bash, "-c", holder_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            contender = subprocess.run(
                [bash, "-c", contender_script],
                check=False,
                capture_output=True,
                text=True,
            )
            holder_stdout, holder_stderr = holder.communicate(timeout=5)

        self.assertEqual(holder.returncode, 0, holder_stderr)
        self.assertEqual(holder_stdout, "")
        self.assertNotEqual(contender.returncode, 0)
        self.assertIn("another plugin control operation is running", contender.stderr)

    def test_apply_workflow_and_monitor_share_control_lock(self):
        auto_run = self.control.index("  auto-run)")
        apply_start = self.control.index("  apply-start)")
        apply_queued = self.control.index("  apply-queued)")
        worker_failed = self.control.index("  apply-worker-failed)")
        legacy_apply = self.control.index("  apply-prepared)")
        rollback = self.control.index("  rollback)")
        auto_run_block = self.control[auto_run:apply_start]
        apply_start_block = self.control[apply_start:apply_queued]
        apply_queued_block = self.control[apply_queued:worker_failed]
        legacy_apply_block = self.control[legacy_apply:rollback]
        start_apply_unit = shell_function(self.control, "start_apply_unit")

        self.assertLess(auto_run_block.index("try_control_lock"), auto_run_block.index("run_manager auto-run"))
        self.assertLess(apply_start_block.index("acquire_control_lock"), apply_start_block.index("start_apply_unit"))
        self.assertLess(start_apply_unit.index("run_manager queue-apply"), start_apply_unit.index("systemctl --no-block start"))
        self.assertLess(apply_queued_block.index("acquire_control_lock"), apply_queued_block.index("run_manager apply-prepared"))
        self.assertLess(apply_queued_block.index("run_manager apply-prepared"), apply_queued_block.index("restart_with_fallback yes"))
        self.assertLess(apply_queued_block.index("restart_with_fallback yes"), apply_queued_block.index("run_manager auto-finish"))
        self.assertNotIn("run_manager apply-prepared", legacy_apply_block)
        self.assertIn("start_apply_unit", legacy_apply_block)

    def test_apply_unit_has_unbounded_start_and_failure_convergence(self):
        self.assertIn("TimeoutStartSec=infinity", self.apply_unit)
        self.assertIn("TimeoutStopSec=180", self.apply_unit)
        self.assertIn("OnFailure=sub2api-overdraft-apply-failed.service", self.apply_unit)
        self.assertIn(
            "ExecStopPost=/usr/local/sbin/sub2api-plugin-control apply-worker-failed ${SERVICE_RESULT}",
            self.apply_unit,
        )
        worker_block = self.control[
            self.control.index("  apply-worker-failed)") : self.control.index("  apply-prepared)")
        ]
        self.assertIn(
            'if [[ "$action" == "apply-worker-failed" && "${2:-}" == "success" ]]',
            self.control,
        )
        self.assertLess(
            self.control.index('"$action" == "apply-worker-failed"'),
            self.control.index('[[ ! -x "$MANAGER" || ! -r "$ENV_FILE" ]]'),
        )
        self.assertIn("run_manager apply-worker-failed", worker_block)


if __name__ == "__main__":
    unittest.main()
