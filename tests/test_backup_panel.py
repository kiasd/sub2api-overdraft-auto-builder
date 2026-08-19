import base64
import importlib.util
import io
import json
import os
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PANEL_PATH = ROOT / "integration" / "backup-panel" / "backup_web.py"
with mock.patch.dict(os.environ, {"BACKUP_WEB_PASSWORD": "test-password"}):
    SPEC = importlib.util.spec_from_file_location("backup_web", PANEL_PATH)
    panel = importlib.util.module_from_spec(SPEC)
    assert SPEC.loader is not None
    SPEC.loader.exec_module(panel)


class PluginRequest:
    def __init__(self, form):
        self.form = form
        self.responses = []
        self.errors = []

    def csrf_form(self):
        return self.form

    def respond_operation_result(self, status, message, title=""):
        self.responses.append((status, message, title))

    def send_error(self, status):
        self.errors.append(status)


class BackupPanelTests(unittest.TestCase):
    def setUp(self):
        panel.PLUGIN_AUTO_STATUS = {
            "enabled": True,
            "status": "ready",
            "last_result": "ready",
            "progress": 100,
            "prepared": {"status": "ready"},
        }

    def test_apply_request_starts_background_unit_and_returns_202(self):
        tag = "fusion-v0.1.178-overdraft.1-e0c48a19-45e40b4f-u0ca66d07"
        digest = "f" * 64
        request = PluginRequest(
            {
                "action": ["apply-prepared"],
                "expected_tag": [tag],
                "expected_sha256": [digest],
            }
        )

        def control(action, *arguments, timeout=30):
            if action == "auto-status":
                return {
                    "ok": True,
                    "auto_update": {
                        "status": "ready",
                        "last_result": "ready",
                        "prepared": {"status": "ready"},
                    },
                }
            if action == "apply-start":
                self.assertEqual(arguments, (tag, digest))
                self.assertEqual(timeout, 30)
                return {"ok": True, "status": "accepted"}
            self.fail(f"unexpected control action: {action}")

        with mock.patch.object(panel, "plugin_control", side_effect=control), \
            mock.patch.object(panel, "set_plugin_action_status"):
            panel.BackupHandler.run_plugin_request(request)

        self.assertEqual(request.errors, [])
        self.assertEqual(request.responses[0][0], 202)
        self.assertIn("后台应用任务已启动", request.responses[0][1])

    def test_collect_status_derives_operation_from_persistent_manager_state(self):
        auto = {
            "status": "applying",
            "progress": 45,
            "stage": "正在生成切换前数据库恢复点",
            "last_error": "",
        }
        with mock.patch.object(panel, "refresh_plugin_auto_status", return_value=auto), \
            mock.patch.object(panel, "traffic_snapshot", return_value={}), \
            mock.patch.object(panel, "traffic_totals", return_value={"rx": 0, "tx": 0}), \
            mock.patch.object(panel, "traffic_delta", return_value={}), \
            mock.patch.object(panel, "system_memory", return_value=(1, 1)), \
            mock.patch.object(panel, "service_state", return_value="active"), \
            mock.patch.object(panel.shutil, "disk_usage", return_value=mock.Mock(total=1, used=1)):
            status = panel.collect_server_status()

        self.assertIs(status["plugin_operation"]["active"], True)
        self.assertEqual(status["plugin_operation"]["progress"], 45)
        self.assertEqual(status["plugin_operation"]["stage"], auto["stage"])

    def test_apply_http_request_uses_urlencoded_form_and_returns_json(self):
        tag = "fusion-v0.1.178-overdraft.1-e0c48a19-45e40b4f-u0ca66d07"
        digest = "f" * 64

        def control(action, *arguments, timeout=30):
            if action == "apply-start":
                self.assertEqual(arguments, (tag, digest))
                return {"ok": True, "status": "accepted"}
            if action == "auto-status":
                return {
                    "ok": True,
                    "auto_update": {
                        "status": "ready",
                        "prepared": {"status": "ready"},
                    },
                }
            self.fail(f"unexpected control action: {action}")

        server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.BackupHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fields = {
                "csrf_token": panel.CSRF_TOKEN,
                "action": "apply-prepared",
                "expected_tag": tag,
                "expected_sha256": digest,
            }
            body = urllib.parse.urlencode(fields).encode("ascii")
            credentials = base64.b64encode(
                f"{panel.WEB_USER}:{panel.WEB_PASSWORD}".encode("utf-8")
            ).decode("ascii")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/plugin",
                data=body,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with mock.patch.object(panel, "plugin_control", side_effect=control), \
                urllib.request.urlopen(request, timeout=5) as response:
                payload = json.load(response)

            self.assertEqual(response.status, 202)
            self.assertTrue(response.headers.get_content_type() == "application/json")
            self.assertIs(payload["ok"], True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_json_apply_errors_never_return_an_html_error_page(self):
        server = panel.ThreadingHTTPServer(("127.0.0.1", 0), panel.BackupHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            credentials = base64.b64encode(
                f"{panel.WEB_USER}:{panel.WEB_PASSWORD}".encode("utf-8")
            ).decode("ascii")
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/plugin",
                data=b"csrf_token=stale&action=apply-prepared",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(request, timeout=5)
            error = raised.exception
            payload = json.loads(error.read().decode("utf-8"))

            self.assertEqual(error.code, 403)
            self.assertEqual(error.headers.get_content_type(), "application/json")
            self.assertIs(payload["ok"], False)
            self.assertNotIn("<!DOCTYPE", json.dumps(payload))

            multipart_request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/plugin",
                data=b"--boundary--\r\n",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "multipart/form-data; boundary=boundary",
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as raised:
                urllib.request.urlopen(multipart_request, timeout=5)
            error = raised.exception
            payload = json.loads(error.read().decode("utf-8"))

            self.assertEqual(error.code, 415)
            self.assertEqual(error.headers.get_content_type(), "application/json")
            self.assertIs(payload["ok"], False)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_rendered_panel_contains_async_apply_and_fast_progress_polling(self):
        tag = "fusion-v0.1.178-overdraft.1-e0c48a19-45e40b4f-u0ca66d07"
        digest = "f" * 64
        auto = {
            "enabled": True,
            "status": "apply_failed",
            "last_result": "apply_failed",
            "progress": 75,
            "stage": "后台任务在切换后中断，完整恢复点已保留，请执行回退",
            "prepared": {
                "status": "staged",
                "version": "0.1.178-overdraft.1",
                "release_tag": tag,
                "binary_sha256": digest,
            },
        }
        server = {
            "checked_at": "2026-08-19 13:00:00",
            "uptime": "1 天",
            "load": "0.1",
            "memory_total": 2,
            "memory_used": 1,
            "disk_total": 2,
            "disk_used": 1,
            "traffic_total": {"rx": 0, "tx": 0},
            "services": {"Sub2API": "active"},
        }
        update = {
            "current_version": "0.1.178-overdraft.1",
            "current_channel": "overdraft",
            "workflow": {"status": "completed", "conclusion": "success"},
            "release": {
                "version": "0.1.178-overdraft.1",
                "official_version": "0.1.178",
                "tag": tag,
                "binary_sha256": digest,
            },
            "official": {"version": "0.1.178"},
            "official_ahead": False,
            "checked_at": "2026-08-19 13:00:00",
        }
        ip = {
            "ip": "192.0.2.1",
            "country": "US",
            "region": "",
            "city": "",
            "org": "test",
            "is_us": True,
            "checked_at": "2026-08-19 13:00:00",
        }

        class CaptureHandler:
            def __init__(self):
                self.wfile = io.BytesIO()

            def send_response(self, _status):
                pass

            def send_common_headers(self):
                pass

            def send_header(self, _name, _value):
                pass

            def end_headers(self):
                pass

        handler = CaptureHandler()
        with mock.patch.object(panel, "backup_rows", return_value=[]), \
            mock.patch.object(panel, "public_ip_status", return_value=ip), \
            mock.patch.object(panel, "collect_server_status", return_value=server), \
            mock.patch.object(panel, "plugin_status", return_value={
                "current_version": "0.1.178-overdraft.1",
                "current_state": {"channel": "overdraft", "status": "verified"},
                "weekly_overdraft_enabled": True,
            }), \
            mock.patch.object(panel, "plugin_update_status", return_value=update), \
            mock.patch.object(panel, "plugin_auto_status", return_value=auto):
            panel.BackupHandler.render_index(handler)

        page = handler.wfile.getvalue().decode("utf-8")
        self.assertIn('headers: { Accept: \'application/json\' }', page)
        self.assertIn("body: new URLSearchParams(new FormData(applyForm))", page)
        self.assertNotIn("body: new FormData(applyForm)", page)
        self.assertIn("response.headers.get('content-type')", page)
        self.assertIn("setTimeout(refreshServerStatus, pluginOperationActive ? 500 : 30000)", page)
        self.assertIn(f'name="expected_tag" value="{tag}"', page)
        self.assertIn(f'name="expected_sha256" value="{digest}"', page)
        self.assertIn('id="pluginApplyButton" class="plugin-button primary" type="submit">重新应用已验证版本</button>', page)
        self.assertNotIn('id="pluginApplyForm" method="post" action="/plugin" onsubmit=', page)
        node = shutil.which("node")
        if node:
            script = page.split("<script>", 1)[1].split("</script>", 1)[0]
            checked = subprocess.run(
                [node, "--check", "-"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)


if __name__ == "__main__":
    unittest.main()
