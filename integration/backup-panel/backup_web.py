#!/usr/bin/env python3
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import smtplib
import ssl
import subprocess
import threading
import tempfile
import time
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/var/backups/sub2api")).resolve()
WEB_USER = os.environ.get("BACKUP_WEB_USER", "backupadmin")
WEB_PASSWORD = os.environ["BACKUP_WEB_PASSWORD"]
WEB_HOST = os.environ.get("BACKUP_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("BACKUP_WEB_PORT", "2222"))
IP_CHECK_INTERVAL = max(300, int(os.environ.get("IP_CHECK_INTERVAL", "1800")))
IP_INFO_URL = os.environ.get("IP_INFO_URL", "https://ipinfo.io/json")
STATE_DIR = Path(os.environ.get("BACKUP_WEB_STATE_DIR", "/var/lib/sub2api-backup-web")).resolve()
EMAIL_CONFIG_PATH = Path(os.environ.get("EMAIL_CONFIG_PATH", str(STATE_DIR / "email-config.json"))).resolve()
MONITOR_STATE_PATH = Path(os.environ.get("MONITOR_STATE_PATH", str(STATE_DIR / "monitor-state.json"))).resolve()
PLUGIN_CONTROL = Path(os.environ.get("SUB2API_PLUGIN_CONTROL", "/usr/local/sbin/sub2api-plugin-control")).resolve()
BACKUP_NAME = re.compile(r"^sub2api-\d{8}-\d{6}\.dump$")
EMAIL_ADDRESS = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
IP_STATUS_LOCK = threading.Lock()
EMAIL_STATUS_LOCK = threading.Lock()
EMAIL_CONFIG_LOCK = threading.Lock()
EMAIL_SEND_LOCK = threading.Lock()
MONITOR_STATE_LOCK = threading.Lock()
RESTORE_LOCK = threading.Lock()
PLUGIN_LOCK = threading.Lock()
PLUGIN_UPDATE_STATUS_LOCK = threading.Lock()
EMAIL_WAKE = threading.Event()
PLUGIN_ACTIVE_STATUSES = {"apply_queued", "applying", "restart_pending", "rollback_pending"}
CSRF_TOKEN = secrets.token_urlsafe(32)
EMAIL_CONFIG_DEFAULTS = {
    "enabled": False,
    "sender_email": "",
    "smtp_host": "smtp.qq.com",
    "smtp_port": 465,
    "smtp_security": "ssl",
    "smtp_password": "",
    "interval_minutes": 60,
    "recipients": [],
}
IP_STATUS = {
    "ip": "",
    "country": "",
    "region": "",
    "city": "",
    "org": "",
    "timezone": "",
    "is_us": None,
    "previous_ip": "",
    "ip_changed": False,
    "checked_at": "",
    "error": "",
}
RESTORE_STATUS = {"kind": "", "message": "", "finished_at": ""}
EMAIL_STATUS = {"kind": "", "message": "", "finished_at": ""}
PLUGIN_ACTION_STATUS = {"kind": "", "message": "", "finished_at": ""}
PLUGIN_UPDATE_STATUS = {
    "current_version": "未知",
    "current_channel": "未知",
    "version": "检测中",
    "has_update": None,
    "repository": "kiasd/sub2api-overdraft-auto-builder",
    "workflow": {},
    "release": {},
    "official": {},
    "official_ahead": None,
    "checked_at": "",
    "error": "",
}
PLUGIN_AUTO_STATUS = {
    "enabled": True,
    "status": "检测中",
    "last_result": "",
    "last_started_at": "",
    "last_checked_at": "",
    "finished_at": "",
    "last_error": "",
    "last_check": {},
    "last_upgrade": {},
    "min_interval_hours": 3,
    "max_interval_hours": 5,
}
def read_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as source:
            value = json.load(source)
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


MONITOR_STATE = read_json(MONITOR_STATE_PATH, {})


def load_email_config():
    config = dict(EMAIL_CONFIG_DEFAULTS)
    stored = read_json(EMAIL_CONFIG_PATH, {})
    for key in config:
        if key in stored:
            config[key] = stored[key]
    config["enabled"] = bool(config["enabled"])
    config["smtp_port"] = int(config["smtp_port"]) if str(config["smtp_port"]).isdigit() else 465
    config["smtp_security"] = config["smtp_security"] if config["smtp_security"] in {"ssl", "starttls"} else "ssl"
    config["interval_minutes"] = int(config["interval_minutes"]) if str(config["interval_minutes"]).isdigit() else 60
    if config["interval_minutes"] not in {15, 30, 60, 360, 1440}:
        config["interval_minutes"] = 60
    config["recipients"] = [item for item in config["recipients"] if isinstance(item, str)] if isinstance(config["recipients"], list) else []
    return config


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def save_email_config(config):
    with EMAIL_CONFIG_LOCK:
        save_json(EMAIL_CONFIG_PATH, config)
    EMAIL_WAKE.set()


def parse_recipients(value):
    recipients = [item.strip() for item in re.split(r"[,;\s]+", value) if item.strip()]
    invalid = [item for item in recipients if not EMAIL_ADDRESS.fullmatch(item)]
    if invalid:
        raise ValueError(f"收件邮箱格式不正确：{', '.join(invalid[:3])}")
    return recipients


def email_status():
    with EMAIL_STATUS_LOCK:
        return dict(EMAIL_STATUS)


def set_email_status(kind, message):
    global EMAIL_STATUS
    with EMAIL_STATUS_LOCK:
        EMAIL_STATUS = {
            "kind": kind,
            "message": message,
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }


def save_monitor_state(updates):
    with MONITOR_STATE_LOCK:
        MONITOR_STATE.update(updates)
        try:
            save_json(MONITOR_STATE_PATH, MONITOR_STATE)
        except OSError:
            pass


def human_size(value):
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{amount:.1f} TB"


def format_delta(value):
    if value is None:
        return "-"
    prefix = "+" if value > 0 else ""
    return f"{prefix}{human_size(value)}"


def backup_rows():
    files = sorted(BACKUP_DIR.glob("sub2api-*.dump"), key=lambda item: item.stat().st_mtime, reverse=True)
    rows = []
    for index, path in enumerate(files):
        stat = path.stat()
        older_size = files[index + 1].stat().st_size if index + 1 < len(files) else None
        checksum_path = path.with_name(path.name + ".sha256")
        checksum = checksum_path.read_text(encoding="ascii").strip() if checksum_path.exists() else ""
        rows.append(
            {
                "name": path.name,
                "size": stat.st_size,
                "delta": stat.st_size - older_size if older_size is not None else None,
                "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "checksum": checksum,
            }
        )
    return rows


def check_public_ip():
    checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        request = urllib.request.Request(IP_INFO_URL, headers={"User-Agent": "Sub2API-Backup-Monitor/1.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.load(response)
        country = str(payload.get("country", "")).upper()
        previous_ip = str(MONITOR_STATE.get("last_ip", ""))
        current_ip = str(payload.get("ip", ""))
        status = {
            "ip": current_ip,
            "country": country,
            "region": str(payload.get("region", "")),
            "city": str(payload.get("city", "")),
            "org": str(payload.get("org", "")),
            "timezone": str(payload.get("timezone", "")),
            "is_us": country == "US" if country else None,
            "previous_ip": previous_ip,
            "ip_changed": bool(previous_ip and current_ip and previous_ip != current_ip),
            "checked_at": checked_at,
            "error": "",
        }
        save_monitor_state({"last_ip": current_ip})
    except Exception as exc:
        with IP_STATUS_LOCK:
            status = dict(IP_STATUS)
        status["checked_at"] = checked_at
        status["error"] = str(exc)[:160]
    with IP_STATUS_LOCK:
        IP_STATUS.update(status)


def public_ip_loop():
    while True:
        check_public_ip()
        time.sleep(IP_CHECK_INTERVAL)


def public_ip_status():
    with IP_STATUS_LOCK:
        return dict(IP_STATUS)


def traffic_snapshot():
    snapshot = {}
    try:
        lines = Path("/proc/net/dev").read_text(encoding="ascii").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return snapshot
    for line in lines:
        if ":" not in line:
            continue
        interface, values = line.split(":", 1)
        interface = interface.strip()
        fields = values.split()
        if interface == "lo" or len(fields) < 9:
            continue
        try:
            snapshot[interface] = {"rx": int(fields[0]), "tx": int(fields[8])}
        except ValueError:
            continue
    return snapshot


def traffic_totals(snapshot):
    return {
        "rx": sum(item["rx"] for item in snapshot.values()),
        "tx": sum(item["tx"] for item in snapshot.values()),
    }


def traffic_delta(current, previous):
    previous = previous if isinstance(previous, dict) else {}
    result = {}
    for interface, values in current.items():
        old = previous.get(interface, {}) if isinstance(previous.get(interface, {}), dict) else {}
        result[interface] = {
            "rx": max(0, values["rx"] - int(old.get("rx", 0))),
            "tx": max(0, values["tx"] - int(old.get("tx", 0))),
        }
    return result


def format_uptime():
    try:
        seconds = int(float(Path("/proc/uptime").read_text(encoding="ascii").split()[0]))
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return "未知"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes = seconds // 60
    return f"{days}天 {hours}小时 {minutes}分钟"


def system_memory():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0]) * 1024
    except (FileNotFoundError, OSError, ValueError):
        return None, None
    total = values.get("MemTotal")
    available = values.get("MemAvailable", values.get("MemFree"))
    return total, max(0, total - available) if total is not None and available is not None else None


def service_state(unit):
    try:
        result = subprocess.run(
            ["systemctl", "is-active", unit],
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
        return (result.stdout or "unknown").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def plugin_control(action, *arguments, timeout=30):
    command = ["/usr/bin/sudo", str(PLUGIN_CONTROL), action]
    command.extend(str(value) for value in arguments if value is not None)
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)[:500]}
    output = (result.stdout or "").strip()
    error = (result.stderr or "").strip()
    payload = {}
    for line in reversed(output.splitlines()):
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            payload = value
            break
    payload["ok"] = result.returncode == 0
    if result.returncode != 0:
        payload["error"] = (error or output or "插件控制命令失败")[-1000:]
    return payload


def plugin_status():
    return plugin_control("status", timeout=20)


def set_plugin_action_status(kind, message):
    global PLUGIN_ACTION_STATUS
    PLUGIN_ACTION_STATUS = {
        "kind": kind,
        "message": message,
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_plugin_update_result(result):
    global PLUGIN_UPDATE_STATUS
    summary = result.get("check", {}) if isinstance(result, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    release = result.get("release", {}) if isinstance(result, dict) else {}
    if not isinstance(release, dict):
        release = {}
    workflow = result.get("workflow", {}) if isinstance(result, dict) else {}
    if not isinstance(workflow, dict):
        workflow = {}
    official = result.get("official", {}) if isinstance(result, dict) else {}
    if not isinstance(official, dict):
        official = {}
    if not release and summary:
        release = {
            "version": summary.get("release_version", "未知"),
            "tag": summary.get("release_tag", ""),
            "binary_sha256": summary.get("release_sha256", ""),
            "html_url": summary.get("release_url", ""),
            "official_version": summary.get("release_official_version", "未知"),
        }
    if not workflow and summary:
        workflow = {
            "status": summary.get("workflow_status", "unknown"),
            "conclusion": summary.get("workflow_conclusion", ""),
            "html_url": summary.get("workflow_url", ""),
            "failed": summary.get("workflow_status") == "completed"
            and summary.get("workflow_conclusion") not in {"success", "skipped"},
        }
    if not official and summary:
        official = {
            "version": summary.get("official_version", "未知"),
            "html_url": summary.get("official_url", ""),
        }
    status = {
        "current_version": str(result.get("current_version", summary.get("current_version", "未知"))),
        "current_channel": str(result.get("current_channel", summary.get("current_channel", "未知"))),
        "current_commit": str(result.get("current_commit", "unknown")),
        "version": str(result.get("version", release.get("version", "未知"))),
        "commit": str(result.get("commit", release.get("source_commit", "unknown"))),
        "has_update": result.get("has_update")
        if isinstance(result.get("has_update"), bool)
        else summary.get("has_update")
        if isinstance(summary.get("has_update"), bool)
        else None,
        "repository": str(result.get("repository", release.get("repository", "kiasd/sub2api-overdraft-auto-builder"))),
        "workflow": workflow,
        "release": release,
        "official": official,
        "official_ahead": result.get("official_ahead")
        if isinstance(result.get("official_ahead"), bool)
        else summary.get("official_ahead")
        if isinstance(summary.get("official_ahead"), bool)
        else None,
        "html_url": str(result.get("html_url", release.get("html_url", ""))),
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": "" if result.get("ok") else str(result.get("error", "更新检测失败"))[:500],
    }
    with PLUGIN_UPDATE_STATUS_LOCK:
        PLUGIN_UPDATE_STATUS = status


def plugin_update_status():
    with PLUGIN_UPDATE_STATUS_LOCK:
        return dict(PLUGIN_UPDATE_STATUS)


def save_plugin_auto_status(result):
    global PLUGIN_AUTO_STATUS
    value = result.get("auto_update", result) if isinstance(result, dict) else {}
    if not isinstance(value, dict):
        value = {}
    with PLUGIN_UPDATE_STATUS_LOCK:
        PLUGIN_AUTO_STATUS = dict(PLUGIN_AUTO_STATUS) | value
    last_check = value.get("last_check", {})
    if isinstance(last_check, dict) and last_check:
        save_plugin_update_result({"ok": True, "check": last_check})


def plugin_auto_status():
    with PLUGIN_UPDATE_STATUS_LOCK:
        return dict(PLUGIN_AUTO_STATUS)


def refresh_plugin_auto_status():
    save_plugin_auto_status(plugin_control("auto-status", timeout=20))
    return plugin_auto_status()


def collect_server_status():
    current_auto_status = refresh_plugin_auto_status()
    operation_state = str(current_auto_status.get("status", ""))
    operation_status = {
        "active": operation_state in PLUGIN_ACTIVE_STATUSES,
        "status": operation_state or "idle",
        "progress": current_auto_status.get("progress", 0),
        "stage": current_auto_status.get("stage", ""),
        "error": current_auto_status.get("last_error", ""),
        "started_at": current_auto_status.get("last_started_at", ""),
        "finished_at": current_auto_status.get("finished_at", ""),
    }
    current_traffic = traffic_snapshot()
    previous_traffic = MONITOR_STATE.get("last_email_traffic", {})
    total = traffic_totals(current_traffic)
    delta = traffic_totals(traffic_delta(current_traffic, previous_traffic))
    total_memory, used_memory = system_memory()
    try:
        load = Path("/proc/loadavg").read_text(encoding="ascii").split()[0]
    except (FileNotFoundError, OSError, IndexError):
        load = "未知"
    disk = shutil.disk_usage("/")
    return {
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "uptime": format_uptime(),
        "load": load,
        "memory_total": total_memory,
        "memory_used": used_memory,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "traffic": current_traffic,
        "traffic_total": total,
        "traffic_delta": delta,
        "plugin_update": plugin_update_status(),
        "plugin_auto_update": current_auto_status,
        "plugin_operation": operation_status,
        "services": {
            "Sub2API": service_state("sub2api.service"),
            "PostgreSQL": service_state("postgresql.service"),
            "Redis": service_state("redis-server.service"),
            "备份定时器": service_state("sub2api-backup.timer"),
            "自动更新定时器": service_state("sub2api-overdraft-auto-update.timer"),
            "备份页面": service_state("sub2api-backup-web.service"),
        },
    }


def send_email(config, subject, body):
    if not config["sender_email"] or not EMAIL_ADDRESS.fullmatch(config["sender_email"]):
        raise ValueError("请先填写有效的发信邮箱")
    if not config["smtp_password"]:
        raise ValueError("请先填写 QQ 邮箱授权码")
    if not config["recipients"]:
        raise ValueError("请先填写管理员收件邮箱")
    message = EmailMessage()
    message["From"] = config["sender_email"]
    message["To"] = ", ".join(config["recipients"])
    message["Subject"] = subject
    message.set_content(body)
    if config["smtp_security"] == "ssl":
        with smtplib.SMTP_SSL(config["smtp_host"], config["smtp_port"], timeout=20) as smtp:
            smtp.login(config["sender_email"], config["smtp_password"])
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(config["sender_email"], config["smtp_password"])
            smtp.send_message(message)


def render_status_line(label, value):
    return f"{label}: {value}"


def format_server_email(status, ip_status, test=False):
    memory = "未知"
    if status["memory_total"] is not None and status["memory_used"] is not None:
        memory = f"{human_size(status['memory_used'])} / {human_size(status['memory_total'])}"
    disk = f"{human_size(status['disk_used'])} / {human_size(status['disk_total'])}"
    traffic_lines = [
        f"累计下载: {human_size(status['traffic_total']['rx'])}",
        f"累计上传: {human_size(status['traffic_total']['tx'])}",
        f"本周期下载: {human_size(status['traffic_delta']['rx'])}",
        f"本周期上传: {human_size(status['traffic_delta']['tx'])}",
    ]
    interface_lines = []
    for interface, values in sorted(status["traffic"].items()):
        interface_lines.append(f"  {interface}: 下载 {human_size(values['rx'])} / 上传 {human_size(values['tx'])}")
    service_lines = [f"  {name}: {state}" for name, state in status["services"].items()]
    ip_change = "是" if ip_status.get("ip_changed") else "否"
    title = "Sub2API 服务器状态测试邮件" if test else "Sub2API 服务器定时状态报告"
    return "\n".join(
        [
            title,
            "=" * 32,
            render_status_line("检测时间", status["checked_at"]),
            render_status_line("运行时间", status["uptime"]),
            render_status_line("负载", status["load"]),
            render_status_line("内存使用", memory),
            render_status_line("根分区使用", disk),
            "",
            "服务状态:",
            *service_lines,
            "",
            "流量信息:",
            *traffic_lines,
            *interface_lines,
            "",
            "出口 IP:",
            render_status_line("当前 IP", ip_status.get("ip") or "未知"),
            render_status_line("上次 IP", ip_status.get("previous_ip") or "未知"),
            render_status_line("IP 是否变动", ip_change),
            render_status_line("地区", " / ".join(filter(None, (ip_status.get("city"), ip_status.get("region"), ip_status.get("country")))) or "未知"),
            render_status_line("运营商", ip_status.get("org") or "未知"),
        ]
    )


def email_loop():
    while True:
        config = load_email_config()
        wait_seconds = config["interval_minutes"] * 60 if config["enabled"] else 300
        if EMAIL_WAKE.wait(wait_seconds):
            EMAIL_WAKE.clear()
            continue
        if not config["enabled"]:
            continue
        try:
            status = collect_server_status()
            ip_status = public_ip_status()
            body = format_server_email(status, ip_status)
            with EMAIL_SEND_LOCK:
                send_email(config, f"Sub2API 服务器状态报告 {status['checked_at']}", body)
            save_monitor_state({"last_email_traffic": status["traffic"], "last_email_at": status["checked_at"]})
            set_email_status("success", "定时状态邮件发送成功。")
        except Exception as exc:
            set_email_status("error", f"定时状态邮件发送失败：{str(exc)[:240]}")


class BackupHandler(BaseHTTPRequestHandler):
    server_version = "Sub2APIBackup/1.0"

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} - {fmt % args}", flush=True)

    def send_common_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def authorized(self):
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        return hmac.compare_digest(username, WEB_USER) and hmac.compare_digest(password, WEB_PASSWORD)

    def require_auth(self):
        self.send_response(401)
        self.send_common_headers()
        self.send_header("WWW-Authenticate", 'Basic realm="Sub2API Backups", charset="UTF-8"')
        self.end_headers()

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/healthz":
            self.send_response(200)
            self.send_common_headers()
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return

        if not self.authorized():
            self.require_auth()
            return

        if route == "/api/ip-status":
            self.render_ip_status()
            return
        if route == "/api/server-status":
            self.render_server_status()
            return
        if route == "/":
            self.render_index()
            return
        if route.startswith("/download/"):
            self.download(route.removeprefix("/download/"))
            return
        self.send_error(404)

    def do_POST(self):
        route = urlparse(self.path).path
        if not self.authorized():
            self.require_auth()
            return
        if route == "/email-config":
            self.save_email_config_request()
            return
        if route == "/backup":
            self.run_backup_request()
            return
        if route == "/plugin":
            self.run_plugin_request()
            return
        if not route.startswith("/restore/"):
            self.send_error(404)
            return

        name = unquote(route.removeprefix("/restore/"))
        if not BACKUP_NAME.fullmatch(name):
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if content_length < 1 or content_length > 4096:
            self.send_error(400)
            return

        form = parse_qs(self.rfile.read(content_length).decode("utf-8", errors="strict"))
        token = form.get("csrf_token", [""])[0]
        if not hmac.compare_digest(token, CSRF_TOKEN):
            self.send_error(403)
            return

        if not RESTORE_LOCK.acquire(blocking=False):
            self.respond_restore_result(409, "已有备份或恢复任务正在执行。")
            return

        try:
            self.run_restore(name)
        finally:
            RESTORE_LOCK.release()

    def read_form(self, maximum=16384):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return None
        if content_length < 1 or content_length > maximum:
            self.send_error(400)
            return None
        try:
            return parse_qs(self.rfile.read(content_length).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError):
            self.send_error(400)
            return None

    def csrf_form(self, maximum=16384):
        form = self.read_form(maximum)
        if form is None:
            return None
        token = form.get("csrf_token", [""])[0]
        if not hmac.compare_digest(token, CSRF_TOKEN):
            self.send_error(403)
            return None
        return form

    def save_email_config_request(self):
        form = self.csrf_form()
        if form is None:
            return
        current = load_email_config()
        try:
            smtp_port = int(form.get("smtp_port", ["465"])[0])
            interval_minutes = int(form.get("email_interval", ["60"])[0])
            if not 1 <= smtp_port <= 65535:
                raise ValueError("SMTP 端口必须在 1-65535 之间")
            if interval_minutes not in {15, 30, 60, 360, 1440}:
                raise ValueError("发送频率不受支持")
            sender_email = form.get("sender_email", [""])[0].strip()
            smtp_host = form.get("smtp_host", [""])[0].strip()
            smtp_security = form.get("smtp_security", ["ssl"])[0]
            if sender_email and not EMAIL_ADDRESS.fullmatch(sender_email):
                raise ValueError("发信邮箱格式不正确")
            if not smtp_host or len(smtp_host) > 253:
                raise ValueError("SMTP 服务器地址不正确")
            if smtp_security not in {"ssl", "starttls"}:
                raise ValueError("SMTP 加密方式不正确")
            password = form.get("smtp_password", [""])[0]
            config = {
                "enabled": form.get("email_enabled", [""])[0] == "on",
                "sender_email": sender_email,
                "smtp_host": smtp_host,
                "smtp_port": smtp_port,
                "smtp_security": smtp_security,
                "smtp_password": password if password else current["smtp_password"],
                "interval_minutes": interval_minutes,
                "recipients": parse_recipients(form.get("recipients", [""])[0]),
            }
            save_email_config(config)
            if form.get("action", ["save"])[0] == "test":
                status = collect_server_status()
                with EMAIL_SEND_LOCK:
                    send_email(config, "Sub2API QQ 发信测试", format_server_email(status, public_ip_status(), test=True))
                set_email_status("success", "测试邮件已发送，请检查管理员收件邮箱。")
                self.respond_operation_result(200, "测试邮件已发送，请检查管理员收件邮箱。", "邮件测试结果")
                return
            set_email_status("success", "邮件通知配置已保存。")
            self.respond_operation_result(200, "邮件通知配置已保存。", "邮件配置结果")
        except (ValueError, OSError, smtplib.SMTPException) as exc:
            message = str(exc)[:500] or "邮件配置保存失败"
            set_email_status("error", message)
            self.respond_operation_result(400, message, "邮件配置结果")

    def run_backup_request(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if content_length < 1 or content_length > 4096:
            self.send_error(400)
            return

        try:
            form = parse_qs(self.rfile.read(content_length).decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, ValueError):
            self.send_error(400)
            return
        token = form.get("csrf_token", [""])[0]
        if not hmac.compare_digest(token, CSRF_TOKEN):
            self.send_error(403)
            return

        if not RESTORE_LOCK.acquire(blocking=False):
            self.respond_operation_result(409, "已有备份或恢复任务正在执行。")
            return

        try:
            self.run_backup()
        finally:
            RESTORE_LOCK.release()

    def run_backup(self):
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "/usr/local/sbin/sub2api-backup"],
                check=False,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode == 0:
                self.respond_operation_result(200, "已完成一次数据库全量备份。")
                return
            detail = (result.stderr or result.stdout or "备份命令失败").strip()[-500:]
            self.respond_operation_result(500, f"备份失败：{detail}")
        except subprocess.TimeoutExpired:
            self.respond_operation_result(504, "备份执行超时，请检查服务日志。")

    def run_plugin_request(self):
        form = self.csrf_form()
        if form is None:
            return
        action = form.get("action", [""])[0]
        value = form.get("value", [""])[0]
        timeouts = {
            "check-update": 900,
            "set-auto-update": 30,
            "apply-prepared": 7200,
            "rollback": 1800,
            "enable-overdraft": 300,
        }
        if action not in timeouts:
            self.send_error(400)
            return
        if action == "enable-overdraft" and value not in {"on", "off"}:
            self.send_error(400)
            return
        if action == "set-auto-update" and value not in {"on", "off"}:
            self.send_error(400)
            return
        if action == "apply-prepared":
            expected_tag = form.get("expected_tag", [""])[0].strip()
            expected_hash = form.get("expected_sha256", [""])[0].strip().lower()
            if not expected_tag or len(expected_tag) > 160 or not re.fullmatch(r"[0-9a-z.-]+", expected_tag):
                self.respond_operation_result(400, "候选 Release 标签无效。", "插件操作结果")
                return
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                self.respond_operation_result(400, "候选程序 SHA-256 无效。", "插件操作结果")
                return
        else:
            expected_tag = ""
            expected_hash = ""
        current_auto = refresh_plugin_auto_status()
        if action != "apply-prepared" and str(current_auto.get("status", "")) in PLUGIN_ACTIVE_STATUSES:
            self.respond_operation_result(409, "版本应用或回退任务正在执行，请等待完成。", "版本操作结果")
            return
        if not PLUGIN_LOCK.acquire(blocking=False):
            self.respond_operation_result(409, "已有 Release 监控或更新操作正在执行。", "版本操作结果")
            return
        try:
            if action == "apply-prepared":
                result = plugin_control(
                    "apply-start", expected_tag, expected_hash, timeout=30
                )
            else:
                control_action = "auto-run" if action == "check-update" else action
                arguments = (
                    (value,)
                    if action in {"enable-overdraft", "set-auto-update"}
                    else ()
                )
                result = plugin_control(
                    control_action, *arguments, timeout=timeouts[action]
                )
        finally:
            PLUGIN_LOCK.release()
        if action == "apply-prepared":
            save_plugin_auto_status(plugin_control("auto-status", timeout=20))
        if action == "check-update":
            save_plugin_update_result(result)
            save_plugin_auto_status(result)
        if action == "set-auto-update":
            save_plugin_auto_status(plugin_control("auto-status", timeout=20))
        if not result.get("ok"):
            message = str(result.get("error", "插件操作失败"))[-1000:]
            set_plugin_action_status("error", message)
            self.respond_operation_result(500, message, "插件操作结果")
            return
        labels = {
            "check-update": "构建仓库监控已完成。",
            "set-auto-update": "Release 监控设置已更新。",
            "apply-prepared": "后台应用任务已启动，正在执行完整备份。",
            "rollback": "回退、重启和健康检查已完成。",
            "enable-overdraft": "透支设置已更新并完成重启。",
        }
        detail = labels[action]
        if action == "check-update":
            monitor_status = str(result.get("status", "unknown"))
            if monitor_status == "ready":
                detail = "已验证 Release 已下载，等待你点击应用。"
            elif monitor_status == "waiting_builder":
                detail = "官方已发布新版本，正在等待 GitHub 融合仓库完成编译。"
            elif monitor_status == "building":
                detail = "GitHub 正在融合编译，服务器不会本地编译。"
            elif monitor_status == "failed":
                detail = "GitHub 构建或仓库监控失败，服务器继续运行旧版。"
            else:
                detail = "构建仓库检查完成，当前没有需要应用的新 Release。"
        set_plugin_action_status("success", detail)
        self.respond_operation_result(202 if action == "apply-prepared" else 200, detail, "插件操作结果")

    def run_restore(self, name):
        global RESTORE_STATUS
        try:
            result = subprocess.run(
                ["/usr/bin/sudo", "/usr/local/sbin/sub2api-restore", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=900,
            )
            finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if result.returncode == 0:
                RESTORE_STATUS = {
                    "kind": "success",
                    "message": f"已从 {name} 完成全量恢复。",
                    "finished_at": finished_at,
                }
                self.respond_restore_result(200, RESTORE_STATUS["message"])
                return
            detail = (result.stderr or result.stdout or "恢复命令失败").strip()[-500:]
            RESTORE_STATUS = {
                "kind": "error",
                "message": f"恢复失败：{detail}",
                "finished_at": finished_at,
            }
            self.respond_restore_result(500, RESTORE_STATUS["message"])
        except subprocess.TimeoutExpired:
            RESTORE_STATUS = {
                "kind": "error",
                "message": "恢复执行超时，请检查服务日志。",
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.respond_restore_result(504, RESTORE_STATUS["message"])

    def respond_restore_result(self, status, message):
        self.respond_operation_result(status, message, "数据库恢复结果")

    def respond_operation_result(self, status, message, title="数据库备份结果"):
        if "application/json" in self.headers.get("Accept", "").lower():
            body = json.dumps(
                {"ok": 200 <= status < 300, "status": status, "message": message},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(status)
            self.send_common_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>body{{margin:0;background:#f3f5f7;color:#17202a;font:14px Segoe UI,Arial,"Microsoft YaHei",sans-serif}}main{{width:min(620px,calc(100% - 32px));margin:12vh auto;background:#fff;border:1px solid #d8dee6;border-radius:6px;padding:28px}}h1{{font-size:20px;margin:0 0 14px}}p{{line-height:1.7;overflow-wrap:anywhere}}a{{display:inline-block;margin-top:10px;color:#166534;font-weight:700;text-decoration:none}}</style></head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><a href="/">返回备份管理</a></main></body></html>""".encode("utf-8")
        self.send_response(status)
        self.send_common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_ip_status(self):
        body = json.dumps(public_ip_status(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_server_status(self):
        body = json.dumps(collect_server_status(), ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def render_index(self):
        rows = backup_rows()
        ip_status = public_ip_status()
        server_status = collect_server_status()
        email_config = load_email_config()
        current_email_status = email_status()
        current_plugin_status = plugin_status()
        total_size = sum(row["size"] for row in rows)
        latest = rows[0]["created"] if rows else "尚无备份"
        if ip_status["is_us"] is True:
            ip_badge = "美国出口"
            ip_badge_class = "is-us"
        elif ip_status["is_us"] is False:
            ip_badge = "非美国出口"
            ip_badge_class = "not-us"
        else:
            ip_badge = "检测中"
            ip_badge_class = "unknown"
        ip_location = " · ".join(filter(None, (ip_status["city"], ip_status["region"], ip_status["country"]))) or "正在获取位置"
        ip_address = ip_status["ip"] or "查询中"
        ip_org = ip_status["org"] or "运营商待确认"
        ip_checked_at = ip_status["checked_at"] or "尚未完成"
        state_labels = {
            "active": "运行中",
            "activating": "启动中",
            "deactivating": "停止中",
            "inactive": "已停止",
            "failed": "故障",
            "unknown": "未知",
        }
        services_healthy = all(state == "active" for state in server_status["services"].values())
        server_health_class = "healthy" if services_healthy else "unhealthy"
        server_health_label = "全部服务正常" if services_healthy else "存在异常服务"
        memory_label = (
            f'{human_size(server_status["memory_used"])} / {human_size(server_status["memory_total"])}'
            if server_status["memory_used"] is not None and server_status["memory_total"] is not None
            else "未知"
        )
        disk_label = f'{human_size(server_status["disk_used"])} / {human_size(server_status["disk_total"])}'
        traffic_label = (
            f'下载 {human_size(server_status["traffic_total"]["rx"])} / '
            f'上传 {human_size(server_status["traffic_total"]["tx"])}'
        )
        service_rows = []
        for name, state in server_status["services"].items():
            state_class = "active" if state == "active" else "pending" if state in {"activating", "deactivating"} else "failed"
            service_rows.append(
                f'<div class="service-row"><span class="service-dot {state_class}"></span>'
                f'<span class="service-name">{html.escape(name)}</span>'
                f'<strong>{html.escape(state_labels.get(state, state))}</strong></div>'
            )
        restore_notice = ""
        if RESTORE_STATUS["message"]:
            restore_notice = (
                f'<div class="notice {html.escape(RESTORE_STATUS["kind"])}">'
                f'{html.escape(RESTORE_STATUS["message"])} '
                f'<span>{html.escape(RESTORE_STATUS["finished_at"])}</span></div>'
            )
        email_notice = ""
        if current_email_status["message"]:
            email_notice = (
                f'<div class="notice {html.escape(current_email_status["kind"])}">'
                f'{html.escape(current_email_status["message"])} '
                f'<span>{html.escape(current_email_status["finished_at"])}</span></div>'
            )
        plugin_notice = ""
        if PLUGIN_ACTION_STATUS["message"]:
            plugin_notice = (
                f'<div class="notice {html.escape(PLUGIN_ACTION_STATUS["kind"])}">'
                f'{html.escape(PLUGIN_ACTION_STATUS["message"])} '
                f'<span>{html.escape(PLUGIN_ACTION_STATUS["finished_at"])}</span></div>'
            )
        plugin_version = html.escape(str(current_plugin_status.get("current_version", "未安装")))
        update_status = plugin_update_status()
        auto_status = plugin_auto_status()
        if isinstance(current_plugin_status.get("auto_update"), dict):
            auto_status.update(current_plugin_status["auto_update"])
        current_state_value = current_plugin_status.get("current_state", {})
        stored_channel = current_state_value.get("channel", "未知") if isinstance(current_state_value, dict) else "未知"
        current_channel_value = str(update_status.get("current_channel") or stored_channel)
        channel_label = "自用融合版" if current_channel_value == "overdraft" else "官方版" if current_channel_value == "official" else "未知"
        official_update = update_status.get("official", {}) if isinstance(update_status.get("official", {}), dict) else {}
        release_update = update_status.get("release", {}) if isinstance(update_status.get("release", {}), dict) else {}
        workflow_update = update_status.get("workflow", {}) if isinstance(update_status.get("workflow", {}), dict) else {}
        plugin_update_checked = html.escape(str(update_status.get("checked_at", "尚未检测")))
        auto_enabled = auto_status.get("enabled") is True
        auto_enabled_label = "已启用" if auto_enabled else "已停用"
        auto_toggle_value = "off" if auto_enabled else "on"
        auto_toggle_label = "停用自动更新" if auto_enabled else "启用自动更新"
        auto_run_status = str(auto_status.get("last_result") or auto_status.get("status") or "尚未运行")
        auto_status_labels = {
            "no_update": "无更新",
            "updated": "已完成更新",
            "apply_queued": "已进入后台应用队列",
            "applying": "正在应用",
            "apply_failed": "应用失败，可重试",
            "restart_pending": "等待健康检查",
            "rollback_pending": "正在恢复旧版",
            "rolled_back": "候选失败，已恢复旧版",
            "waiting_builder": "官方已更新，等待仓库编译",
            "building": "GitHub 正在编译",
            "build_failed": "GitHub 编译失败，保留旧版",
            "downloading": "正在下载已验证版本",
            "ready": "已下载，等待应用",
            "failed": "仓库监控失败，保留旧版",
            "disabled": "已停用",
        }
        auto_run_label = html.escape(auto_status_labels.get(auto_run_status, auto_run_status))
        auto_checked_at = html.escape(str(auto_status.get("last_checked_at") or "尚未运行"))
        auto_error = html.escape(str(auto_status.get("last_error") or ""))
        auto_progress = max(0, min(100, int(auto_status.get("progress", 0) or 0)))
        auto_stage = html.escape(str(auto_status.get("stage") or "尚未开始"))
        prepared = auto_status.get("prepared", {}) if isinstance(auto_status.get("prepared", {}), dict) else {}
        auto_state = str(auto_status.get("status", ""))
        auto_active = auto_state in PLUGIN_ACTIVE_STATUSES
        auto_ready = (
            auto_state == "ready" and prepared.get("status") == "ready"
        ) or (
            auto_state == "apply_failed" and prepared.get("status") in {"ready", "staged"}
        )
        auto_apply_label = ("重新应用已验证版本" if auto_state == "apply_failed" else "应用已验证版本") if auto_ready else {
            "apply_queued": "应用任务排队中",
            "applying": "正在备份并应用",
            "restart_pending": "正在重启并检查",
            "rollback_pending": "正在自动恢复旧版",
            "rolled_back": "已恢复旧版",
            "apply_failed": "请先执行回退",
            "waiting_builder": "等待仓库跟进官方版本",
            "building": "仓库编译中",
            "failed": "构建失败，保持旧版",
            "downloading": "正在下载候选包",
        }.get(auto_state, "暂无可应用版本")
        auto_apply_disabled = "" if auto_ready and not auto_active else " disabled"
        auto_apply_confirm = "确认应用已通过仓库测试和校验的版本？系统会先执行完整备份。"
        prepared_info = (
            f"已验证候选 {html.escape(str(prepared.get('version', '未知')))} · "
            f"SHA256 {html.escape(str(prepared.get('binary_sha256', ''))[:16])}..."
            if prepared.get("version") and prepared.get("binary_sha256")
            else "暂无已验证候选包"
        )
        plugin_state_value = current_state_value
        plugin_state = html.escape(str(plugin_state_value.get("status", "未接入")) if isinstance(plugin_state_value, dict) else "未知")
        overdraft_value = current_plugin_status.get("weekly_overdraft_enabled")
        overdraft_label = "已启用" if overdraft_value is True else "已停用" if overdraft_value is False else "未知"
        overdraft_next = "off" if overdraft_value is True else "on"
        overdraft_button = "停用透支" if overdraft_value is True else "启用透支"
        official_latest = html.escape(str(official_update.get("version", "检测中")))
        official_state = "等待融合仓库跟进" if update_status.get("official_ahead") is True else "融合基线已跟进" if official_update else "未检测"
        release_version = html.escape(str(release_update.get("version", "检测中")))
        release_base = html.escape(str(release_update.get("official_version", "检测中")))
        release_tag = html.escape(str(release_update.get("tag", "尚无已验证 Release")))
        release_hash = html.escape(str(release_update.get("binary_sha256", ""))[:16])
        workflow_status = str(workflow_update.get("status", "unknown"))
        workflow_conclusion = str(workflow_update.get("conclusion", ""))
        workflow_failed = workflow_update.get("failed") is True or (
            workflow_status == "completed" and workflow_conclusion not in {"success", "skipped", ""}
        )
        if workflow_failed:
            workflow_state = f"构建失败：{html.escape(workflow_conclusion or 'unknown')}"
        elif workflow_status != "completed":
            workflow_state = "正在编译" if workflow_status in {"queued", "in_progress", "pending"} else "等待检测"
        else:
            workflow_state = "构建通过" if workflow_conclusion == "success" else html.escape(workflow_conclusion or "已完成")
        workflow_url = str(workflow_update.get("html_url", ""))
        workflow_link = (
            f'<a id="builderWorkflowLink" href="{html.escape(workflow_url, quote=True)}" target="_blank" rel="noopener noreferrer">查看 Actions</a>'
            if workflow_url.startswith("https://github.com/")
            else '<a id="builderWorkflowLink" hidden>查看 Actions</a>'
        )
        workflow_error_class = " build-failure" if workflow_failed else ""
        recipient_text = ", ".join(email_config["recipients"])
        masked_password = "已保存授权码，留空保持不变" if email_config["smtp_password"] else "填写 QQ 邮箱授权码"
        table_rows = []
        for row in rows:
            checksum = html.escape(row["checksum"][:16] + "..." if row["checksum"] else "-")
            name = html.escape(row["name"])
            table_rows.append(
                f"""
                <tr>
                  <td><strong>{name}</strong><span class="checksum">SHA256 {checksum}</span></td>
                  <td>{html.escape(row['created'])}</td>
                  <td>{human_size(row['size'])}</td>
                  <td>{format_delta(row['delta'])}</td>
                  <td class="actions"><a class="download" href="/download/{name}" title="下载备份">&#8595; 下载</a><form class="restore-form" method="post" action="/restore/{name}" onsubmit="return confirm('确认使用 {name} 覆盖当前数据库？系统会先自动备份当前数据。')"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><button class="restore" type="submit" title="恢复此备份">&#8634; 恢复</button></form></td>
                </tr>
                """
            )
        if not table_rows:
            table_rows.append('<tr><td colspan="5" class="empty">暂无备份文件</td></tr>')

        page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Sub2API 运维面板</title>
  <style>
    :root {{ color-scheme: light; --ink:#17202a; --muted:#667085; --line:#d8dee6; --surface:#fff; --page:#f3f5f7; --accent:#166534; --accent-bg:#dcfce7; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--page); color:var(--ink); font-family:Inter,Segoe UI,Arial,"Microsoft YaHei",sans-serif; font-size:14px; }}
    header {{ background:#1f2937; color:#fff; border-bottom:4px solid #22c55e; }}
    .header-inner, main {{ width:min(1120px, calc(100% - 32px)); margin:0 auto; }}
    .header-inner {{ min-height:76px; display:flex; align-items:center; justify-content:space-between; gap:16px; }}
    h1 {{ margin:0; font-size:22px; font-weight:700; letter-spacing:0; }}
    .status {{ display:flex; align-items:center; gap:8px; color:#d1fae5; white-space:nowrap; }}
    .dot {{ width:9px; height:9px; border-radius:50%; background:#4ade80; box-shadow:0 0 0 3px rgba(74,222,128,.16); }}
    main {{ padding:24px 0 40px; }}
    .network {{ display:grid; grid-template-columns:minmax(0,1fr) auto; gap:16px; align-items:center; background:var(--surface); border:1px solid var(--line); border-left:4px solid #22c55e; border-radius:6px; padding:16px 18px; margin-bottom:12px; }}
    .network-main {{ display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }}
    .network-label {{ color:var(--muted); font-size:12px; width:100%; }}
    .network-ip {{ font-size:20px; }}
    .network-location {{ color:#475467; }}
    .network-meta {{ grid-column:1 / -1; color:var(--muted); font-size:12px; display:flex; gap:18px; flex-wrap:wrap; }}
    .country-badge {{ justify-self:end; padding:7px 10px; border-radius:5px; font-weight:700; white-space:nowrap; border:1px solid; }}
    .country-badge.is-us {{ color:#166534; background:#dcfce7; border-color:#86efac; }}
    .country-badge.not-us {{ color:#991b1b; background:#fee2e2; border-color:#fca5a5; }}
    .country-badge.unknown {{ color:#475467; background:#f2f4f7; border-color:#d0d5dd; }}
    .metrics {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:20px; }}
    .server-panel {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; margin-bottom:12px; overflow:hidden; }}
    .server-header {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:16px 18px; border-bottom:1px solid #e7ebf0; }}
    .server-title h2 {{ margin:0 0 5px; }}
    .server-title span {{ color:var(--muted); font-size:12px; }}
    .server-health {{ display:flex; align-items:center; gap:8px; font-weight:700; white-space:nowrap; }}
    .server-health.healthy {{ color:#166534; }}
    .server-health.unhealthy {{ color:#991b1b; }}
    .server-metrics {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); border-bottom:1px solid #e7ebf0; }}
    .server-metric {{ min-width:0; padding:15px 18px; border-right:1px solid #e7ebf0; }}
    .server-metric:last-child {{ border-right:0; }}
    .server-metric span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:7px; }}
    .server-metric strong {{ display:block; font-size:15px; line-height:1.4; overflow-wrap:anywhere; }}
    .service-list {{ display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); padding:4px 8px; }}
    .service-row {{ display:grid; grid-template-columns:9px minmax(0,1fr); gap:5px 8px; align-items:center; min-width:0; padding:11px 10px; }}
    .service-row strong {{ grid-column:2; color:var(--muted); font-size:12px; font-weight:600; }}
    .service-name {{ overflow-wrap:anywhere; }}
    .service-dot {{ width:8px; height:8px; border-radius:50%; background:#98a2b3; }}
    .service-dot.active {{ background:#22c55e; }}
    .service-dot.pending {{ background:#f59e0b; }}
    .service-dot.failed {{ background:#ef4444; }}
    .notice {{ border:1px solid; border-radius:6px; padding:11px 14px; margin-bottom:12px; font-weight:600; }}
    .notice span {{ margin-left:8px; color:var(--muted); font-size:12px; font-weight:400; }}
    .notice.success {{ color:#166534; background:#f0fdf4; border-color:#86efac; }}
    .notice.error {{ color:#991b1b; background:#fef2f2; border-color:#fca5a5; }}
    .metric {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:16px; min-height:92px; }}
    .metric span {{ display:block; color:var(--muted); font-size:12px; margin-bottom:10px; }}
    .metric strong {{ display:block; font-size:20px; line-height:1.25; overflow-wrap:anywhere; }}
    .mail-settings {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:18px; margin-bottom:20px; }}
    .plugin-settings {{ background:var(--surface); border:1px solid var(--line); border-radius:6px; padding:18px; margin-bottom:20px; }}
    .plugin-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-bottom:14px; }}
    .plugin-header h2 {{ margin:0; }}
    .plugin-summary {{ display:flex; gap:18px; flex-wrap:wrap; color:var(--muted); font-size:12px; }}
    .plugin-summary strong {{ color:var(--ink); margin-left:5px; }}
    .channel-grid {{ display:grid; gap:8px; margin-top:14px; }}
    .channel-row {{ display:grid; grid-template-columns:150px minmax(0,1fr) minmax(80px,auto); gap:12px; align-items:center; padding:10px 12px; border:1px solid #e7ebf0; border-radius:5px; color:var(--muted); font-size:12px; }}
    .channel-row strong, .channel-row b {{ color:var(--ink); }}
    .channel-row a {{ color:#175cd3; font-weight:700; text-decoration:none; }}
    .channel-row a:hover {{ text-decoration:underline; }}
    .build-failure, .build-failure a {{ color:#b42318 !important; }}
    .plugin-actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:16px; }}
    .plugin-actions form {{ margin:0; }}
    .plugin-button {{ border:1px solid #c7ced8; border-radius:5px; padding:8px 11px; background:#fff; color:#344054; font:inherit; font-weight:600; cursor:pointer; }}
    .plugin-button.primary {{ color:#fff; background:#166534; border-color:#166534; }}
    .plugin-button.danger {{ color:#9a3412; background:#fff7ed; border-color:#fdba74; }}
    .plugin-button:disabled {{ color:#98a2b3; background:#f2f4f7; border-color:#d0d5dd; cursor:not-allowed; }}
    .plugin-auto-status {{ display:flex; flex-wrap:wrap; gap:8px 16px; margin-top:12px; color:var(--muted); font-size:12px; }}
    .plugin-auto-status b {{ color:var(--ink); }}
    .plugin-auto-status .auto-error {{ color:#991b1b; overflow-wrap:anywhere; }}
    .plugin-progress {{ display:grid; grid-template-columns:minmax(120px,1fr) auto; gap:10px; align-items:center; margin-top:14px; }}
    .plugin-progress-track {{ height:9px; overflow:hidden; border-radius:5px; background:#e5e7eb; }}
    .plugin-progress-bar {{ height:100%; width:0; border-radius:5px; background:#166534; transition:width .35s ease; }}
    .plugin-progress-bar.failed {{ background:#dc2626; }}
    .plugin-progress-bar.rolled-back {{ background:#d97706; }}
    .plugin-progress-label {{ min-width:42px; color:var(--muted); text-align:right; font-variant-numeric:tabular-nums; }}
    .plugin-stage {{ margin-top:6px; color:var(--muted); font-size:12px; overflow-wrap:anywhere; }}
    .mail-header {{ display:flex; align-items:flex-start; justify-content:space-between; gap:18px; margin-bottom:18px; }}
    .mail-header h2 {{ margin:0; }}
    .mail-header p {{ margin:6px 0 0; color:var(--muted); font-size:12px; }}
    .switch {{ display:flex; align-items:center; gap:8px; color:#344054; font-weight:600; white-space:nowrap; cursor:pointer; }}
    .switch input {{ width:16px; height:16px; accent-color:#166534; }}
    .form-grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:14px; }}
    .field {{ display:flex; flex-direction:column; gap:6px; min-width:0; color:#475467; font-size:12px; font-weight:600; }}
    .field input, .field select, .field textarea {{ width:100%; border:1px solid #c7ced8; border-radius:5px; background:#fff; color:var(--ink); padding:9px 10px; font:inherit; font-weight:400; }}
    .field textarea {{ resize:vertical; min-height:58px; line-height:1.5; }}
    .field input:focus, .field select:focus, .field textarea:focus {{ outline:2px solid #bbf7d0; border-color:#22c55e; }}
    .recipient-settings {{ border-top:1px solid #e7ebf0; margin-top:18px; padding-top:16px; }}
    .recipient-settings h3 {{ margin:0 0 10px; font-size:14px; color:var(--ink); }}
    .mail-actions {{ display:flex; gap:8px; flex-wrap:wrap; margin-top:16px; }}
    .save-mail, .test-mail {{ border:1px solid; border-radius:5px; padding:8px 11px; font:inherit; font-weight:600; cursor:pointer; }}
    .save-mail {{ color:#fff; background:#166534; border-color:#166534; }}
    .save-mail:hover {{ background:#14532d; }}
    .test-mail {{ color:#344054; background:#fff; border-color:#c7ced8; }}
    .toolbar {{ display:flex; align-items:center; justify-content:space-between; gap:16px; margin:0 0 10px; }}
    h2 {{ margin:0; font-size:17px; letter-spacing:0; }}
    .toolbar-actions {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }}
    .refresh {{ color:#344054; text-decoration:none; border:1px solid #c7ced8; background:#fff; padding:8px 11px; border-radius:5px; }}
    .backup-now {{ color:#fff; background:#166534; border:1px solid #166534; padding:8px 11px; border-radius:5px; font:inherit; font-weight:600; cursor:pointer; }}
    .backup-now:hover {{ background:#14532d; }}
    .table-wrap {{ overflow-x:auto; background:var(--surface); border:1px solid var(--line); border-radius:6px; }}
    table {{ width:100%; border-collapse:collapse; min-width:780px; }}
    th, td {{ text-align:left; padding:13px 14px; border-bottom:1px solid #e7ebf0; vertical-align:middle; }}
    th {{ color:#475467; background:#f8fafb; font-size:12px; font-weight:600; }}
    tr:last-child td {{ border-bottom:0; }}
    .checksum {{ display:block; margin-top:5px; color:#84909e; font:11px Consolas,monospace; }}
    .actions {{ text-align:right; white-space:nowrap; }}
    .download {{ display:inline-flex; align-items:center; gap:6px; color:var(--accent); background:var(--accent-bg); border:1px solid #86efac; padding:7px 10px; border-radius:5px; text-decoration:none; font-weight:600; white-space:nowrap; }}
    .restore-form {{ display:inline-block; margin-left:6px; }}
    .restore {{ color:#9a3412; background:#fff7ed; border:1px solid #fdba74; padding:7px 10px; border-radius:5px; font:inherit; font-weight:600; cursor:pointer; }}
    .empty {{ text-align:center; color:var(--muted); padding:36px; }}
    footer {{ margin-top:14px; color:var(--muted); font-size:12px; }}
    @media (max-width:900px) {{ .server-metrics, .service-list {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} .server-metric:nth-child(3) {{ border-right:0; }} .form-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} }}
    @media (max-width:760px) {{ .metrics, .server-metrics, .service-list {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .server-header, .header-inner {{ align-items:flex-start; padding:18px 0; flex-direction:column; }} .server-header {{ padding:16px; }} .server-metric:nth-child(3) {{ border-right:1px solid #e7ebf0; }} .server-metric:nth-child(even) {{ border-right:0; }} .network {{ grid-template-columns:1fr; }} .country-badge {{ justify-self:start; }} .mail-header {{ flex-direction:column; }} .form-grid {{ grid-template-columns:1fr; }} .channel-row {{ grid-template-columns:1fr auto; }} }}
    @media (max-width:480px) {{ .server-metrics, .service-list {{ grid-template-columns:1fr; }} .server-metric {{ border-right:0; border-bottom:1px solid #e7ebf0; }} .server-metric:last-child {{ border-bottom:0; }} }}
  </style>
</head>
<body>
  <header><div class="header-inner"><h1>Sub2API 运维面板</h1><div class="status"><span class="dot"></span>备份服务正常</div></div></header>
  <main>
    {restore_notice}
    {email_notice}
    {plugin_notice}
    <section class="network" aria-label="公网出口状态">
      <div class="network-main"><span class="network-label">公网出口</span><strong class="network-ip" id="publicIp">{html.escape(ip_address)}</strong><span class="network-location" id="ipLocation">{html.escape(ip_location)}</span></div>
      <span class="country-badge {ip_badge_class}" id="countryBadge">{ip_badge}</span>
      <div class="network-meta"><span id="ipOrg">{html.escape(ip_org)}</span><span id="ipCheckedAt">最后检测：{html.escape(ip_checked_at)}</span><span>每 30 分钟检测</span></div>
    </section>
    <section class="server-panel" aria-label="服务器状态">
      <div class="server-header">
        <div class="server-title"><h2>服务器状态</h2><span id="serverCheckedAt">采集时间：{html.escape(server_status['checked_at'])}</span></div>
        <div class="server-health {server_health_class}" id="serverHealth"><span class="service-dot {'active' if services_healthy else 'failed'}"></span><span>{server_health_label}</span></div>
      </div>
      <div class="server-metrics">
        <div class="server-metric"><span>运行时间</span><strong id="serverUptime">{html.escape(server_status['uptime'])}</strong></div>
        <div class="server-metric"><span>1 分钟负载</span><strong id="serverLoad">{html.escape(str(server_status['load']))}</strong></div>
        <div class="server-metric"><span>内存</span><strong id="serverMemory">{html.escape(memory_label)}</strong></div>
        <div class="server-metric"><span>根分区</span><strong id="serverDisk">{html.escape(disk_label)}</strong></div>
        <div class="server-metric"><span>累计流量</span><strong id="serverTraffic">{html.escape(traffic_label)}</strong></div>
      </div>
      <div class="service-list" id="serviceList">{''.join(service_rows)}</div>
    </section>
    <section class="metrics" aria-label="备份摘要">
      <div class="metric"><span>当前备份</span><strong>{len(rows)} / 10</strong></div>
      <div class="metric"><span>占用空间</span><strong>{human_size(total_size)}</strong></div>
      <div class="metric"><span>最新备份</span><strong>{html.escape(latest)}</strong></div>
      <div class="metric"><span>下次执行</span><strong>每日 03:00</strong></div>
    </section>
    <section class="plugin-settings" aria-label="Sub2API 版本监控">
      <div class="plugin-header"><h2>Sub2API 版本监控</h2><span id="pluginUpdateChecked">检测时间：{plugin_update_checked}</span></div>
      <div class="plugin-summary"><span>当前版本类型<strong id="pluginCurrentChannel">{channel_label}</strong></span><span>当前版本<strong id="pluginCurrentVersion">{plugin_version}</strong></span><span>部署状态<strong>{plugin_state}</strong></span><span>透支功能<strong>{overdraft_label}</strong></span><span>Release 监控<strong id="pluginAutoEnabled">{auto_enabled_label}</strong></span></div>
      <div class="channel-grid">
        <div class="channel-row"><strong>官方最新 Release</strong><span>版本 <b id="officialLatestVersion">{official_latest}</b></span><span id="officialUpdateState">{official_state}</span></div>
        <div class="channel-row{workflow_error_class}"><strong>GitHub 融合构建</strong><span>{workflow_link}</span><span id="builderWorkflowState">{workflow_state}</span></div>
        <div class="channel-row"><strong>已验证融合 Release</strong><span><b id="verifiedReleaseVersion">{release_version}</b> · 官方基线 <b id="verifiedReleaseBase">{release_base}</b></span><span id="verifiedReleaseState">{release_tag}{f' · {release_hash}...' if release_hash else ''}</span></div>
      </div>
      <div class="plugin-actions">
        <form method="post" action="/plugin"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><input type="hidden" name="action" value="check-update"><button class="plugin-button" type="submit">检查仓库</button></form>
        <form id="pluginApplyForm" method="post" action="/plugin"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><input id="pluginApplyAction" type="hidden" name="action" value="apply-prepared"><input id="pluginApplyTag" type="hidden" name="expected_tag" value="{html.escape(str(prepared.get('release_tag', '')), quote=True)}"><input id="pluginApplySha" type="hidden" name="expected_sha256" value="{html.escape(str(prepared.get('binary_sha256', '')), quote=True)}"><button id="pluginApplyButton" class="plugin-button primary" type="submit"{auto_apply_disabled}>{auto_apply_label}</button></form>
        <form method="post" action="/plugin" onsubmit="return confirm('确认{auto_toggle_label}？')"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><input type="hidden" name="action" value="set-auto-update"><input type="hidden" name="value" value="{auto_toggle_value}"><button class="plugin-button" type="submit">{auto_toggle_label.replace('自动更新', 'Release 监控')}</button></form>
        <form method="post" action="/plugin" onsubmit="return confirm('确认回退到最近一份完整配对备份？')"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><input type="hidden" name="action" value="rollback"><button class="plugin-button danger" type="submit">回退</button></form>
        <form method="post" action="/plugin" onsubmit="return confirm('确认{overdraft_button}并重启 Sub2API？')"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><input type="hidden" name="action" value="enable-overdraft"><input type="hidden" name="value" value="{overdraft_next}"><button class="plugin-button" type="submit">{overdraft_button}</button></form>
      </div>
      <div class="plugin-auto-status"><span>自动监控间隔：3–5 小时</span><span>上次检查：<b id="pluginAutoChecked">{auto_checked_at}</b></span><span>上次结果：<b id="pluginAutoResult">{auto_run_label}</b></span>{f'<span class="auto-error" id="pluginAutoError">{auto_error}</span>' if auto_error else '<span class="auto-error" id="pluginAutoError"></span>'}</div>
      <div class="plugin-progress" aria-label="仓库构建与候选包准备进度"><div class="plugin-progress-track"><div id="pluginAutoProgressBar" class="plugin-progress-bar" style="width:{auto_progress}%"></div></div><span id="pluginAutoProgressLabel" class="plugin-progress-label">{auto_progress}%</span></div>
      <div id="pluginAutoStage" class="plugin-stage">{auto_stage}</div>
      <div id="pluginAutoPrepared" class="plugin-stage">{prepared_info}</div>
    </section>
    <section class="mail-settings" aria-label="QQ 邮箱发信配置">
      <div class="mail-header"><div><h2>QQ 邮箱发信配置</h2><p>用于定时接收服务器状态、流量和出口 IP 变化报告。</p></div><label class="switch"><input type="checkbox" name="email_enabled" form="email-config-form" {'checked' if email_config['enabled'] else ''}><span>启用定时发信</span></label></div>
      <form id="email-config-form" method="post" action="/email-config">
        <input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}">
        <input type="hidden" name="email_enabled" value="off">
        <div class="form-grid">
          <label class="field"><span>发信邮箱</span><input name="sender_email" type="email" value="{html.escape(email_config['sender_email'])}" placeholder="例如：123456@qq.com" autocomplete="email"></label>
          <label class="field"><span>SMTP 服务器</span><input name="smtp_host" value="{html.escape(str(email_config['smtp_host']))}" placeholder="smtp.qq.com" autocomplete="url"></label>
          <label class="field"><span>SMTP 端口</span><input name="smtp_port" type="number" min="1" max="65535" value="{email_config['smtp_port']}"></label>
          <label class="field"><span>加密方式</span><select name="smtp_security"><option value="ssl" {'selected' if email_config['smtp_security'] == 'ssl' else ''}>SSL（465）</option><option value="starttls" {'selected' if email_config['smtp_security'] == 'starttls' else ''}>STARTTLS（587）</option></select></label>
          <label class="field"><span>QQ 邮箱授权码</span><input name="smtp_password" type="password" placeholder="{html.escape(masked_password)}" autocomplete="new-password"></label>
          <label class="field"><span>发送频率</span><select name="email_interval"><option value="15" {'selected' if email_config['interval_minutes'] == 15 else ''}>每 15 分钟</option><option value="30" {'selected' if email_config['interval_minutes'] == 30 else ''}>每 30 分钟</option><option value="60" {'selected' if email_config['interval_minutes'] == 60 else ''}>每 1 小时</option><option value="360" {'selected' if email_config['interval_minutes'] == 360 else ''}>每 6 小时</option><option value="1440" {'selected' if email_config['interval_minutes'] == 1440 else ''}>每天一次</option></select></label>
        </div>
        <div class="recipient-settings"><h3>管理员收件邮箱配置</h3><label class="field field-wide"><span>管理员收件邮箱</span><textarea name="recipients" rows="2" placeholder="admin@example.com，多个地址用逗号或分号分隔">{html.escape(recipient_text)}</textarea></label></div>
        <div class="mail-actions"><button class="save-mail" type="submit" name="action" value="save">&#128190; 保存配置</button><button class="test-mail" type="submit" name="action" value="test">&#9993; 保存并发送测试邮件</button></div>
      </form>
    </section>
    <div class="toolbar"><h2>备份文件</h2><div class="toolbar-actions"><form method="post" action="/backup" onsubmit="return confirm('立即执行一次数据库全量备份？')"><input type="hidden" name="csrf_token" value="{html.escape(CSRF_TOKEN)}"><button class="backup-now" type="submit" title="立即创建全量备份">&#128190; 立即备份</button></form><a class="refresh" href="/">&#8635; 刷新</a></div></div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>文件</th><th>创建时间</th><th>大小</th><th>较上一份</th><th></th></tr></thead>
        <tbody>{''.join(table_rows)}</tbody>
      </table>
    </div>
    <footer>数据库全量压缩备份，可单份下载或一键恢复；超出 10 份后自动删除最旧文件。</footer>
  </main>
  <script>
    function updateIpStatus(data) {{
      const badge = document.getElementById('countryBadge');
      document.getElementById('publicIp').textContent = data.ip || '查询中';
      document.getElementById('ipLocation').textContent = [data.city, data.region, data.country].filter(Boolean).join(' · ') || '正在获取位置';
      document.getElementById('ipOrg').textContent = data.org || '运营商待确认';
      document.getElementById('ipCheckedAt').textContent = '最后检测：' + (data.checked_at || '尚未完成');
      badge.className = 'country-badge ' + (data.is_us === true ? 'is-us' : data.is_us === false ? 'not-us' : 'unknown');
      badge.textContent = data.error ? '查询失败' : data.is_us === true ? '美国出口' : data.is_us === false ? '非美国出口' : '检测中';
    }}
    async function refreshIpStatus() {{
      try {{
        const response = await fetch('/api/ip-status', {{ cache: 'no-store' }});
        if (response.ok) updateIpStatus(await response.json());
      }} catch (_) {{}}
    }}
    function humanSize(value) {{
      if (!Number.isFinite(value)) return '未知';
      const units = ['B', 'KB', 'MB', 'GB', 'TB'];
      let amount = value;
      let unit = 0;
      while (amount >= 1024 && unit < units.length - 1) {{ amount /= 1024; unit += 1; }}
      return (unit === 0 ? Math.round(amount) : amount.toFixed(1)) + ' ' + units[unit];
    }}
    function serviceLabel(state) {{
      return ({{ active:'运行中', activating:'启动中', deactivating:'停止中', inactive:'已停止', failed:'故障', unknown:'未知' }})[state] || state;
    }}
    function updateServerStatus(data) {{
      document.getElementById('serverCheckedAt').textContent = '采集时间：' + (data.checked_at || '未知');
      document.getElementById('serverUptime').textContent = data.uptime || '未知';
      document.getElementById('serverLoad').textContent = data.load || '未知';
      document.getElementById('serverMemory').textContent = Number.isFinite(data.memory_used) && Number.isFinite(data.memory_total) ? humanSize(data.memory_used) + ' / ' + humanSize(data.memory_total) : '未知';
      document.getElementById('serverDisk').textContent = Number.isFinite(data.disk_used) && Number.isFinite(data.disk_total) ? humanSize(data.disk_used) + ' / ' + humanSize(data.disk_total) : '未知';
      const totals = data.traffic_total || {{}};
      document.getElementById('serverTraffic').textContent = '下载 ' + humanSize(totals.rx) + ' / 上传 ' + humanSize(totals.tx);
      const services = data.services || {{}};
      const healthy = Object.keys(services).length > 0 && Object.values(services).every(state => state === 'active');
      const health = document.getElementById('serverHealth');
      health.className = 'server-health ' + (healthy ? 'healthy' : 'unhealthy');
      health.replaceChildren();
      const healthDot = document.createElement('span');
      healthDot.className = 'service-dot ' + (healthy ? 'active' : 'failed');
      const healthText = document.createElement('span');
      healthText.textContent = healthy ? '全部服务正常' : '存在异常服务';
      health.append(healthDot, healthText);
      const list = document.getElementById('serviceList');
      list.replaceChildren();
      Object.entries(services).forEach(([name, state]) => {{
        const row = document.createElement('div');
        row.className = 'service-row';
        const stateDot = document.createElement('span');
        stateDot.className = 'service-dot ' + (state === 'active' ? 'active' : ['activating', 'deactivating'].includes(state) ? 'pending' : 'failed');
        const serviceName = document.createElement('span');
        serviceName.className = 'service-name';
        serviceName.textContent = name;
        const serviceState = document.createElement('strong');
        serviceState.textContent = serviceLabel(state);
        row.append(stateDot, serviceName, serviceState);
        list.append(row);
      }});
      const pluginUpdate = data.plugin_update || {{}};
      document.getElementById('pluginCurrentVersion').textContent = pluginUpdate.current_version || '未知';
      const currentChannel = pluginUpdate.current_channel || 'official';
      document.getElementById('pluginCurrentChannel').textContent = currentChannel === 'overdraft' ? '自用融合版' : '官方版';
      const official = pluginUpdate.official || {{}};
      const release = pluginUpdate.release || {{}};
      const workflow = pluginUpdate.workflow || {{}};
      document.getElementById('officialLatestVersion').textContent = official.version || '未知';
      document.getElementById('officialUpdateState').textContent = pluginUpdate.official_ahead === true ? '等待融合仓库跟进' : official.version ? '融合基线已跟进' : '未检测';
      document.getElementById('verifiedReleaseVersion').textContent = release.version || '未知';
      document.getElementById('verifiedReleaseBase').textContent = release.official_version || '未知';
      document.getElementById('verifiedReleaseState').textContent = release.tag ? release.tag + (release.binary_sha256 ? ' · ' + release.binary_sha256.slice(0, 16) + '...' : '') : '尚无已验证 Release';
      const workflowFailed = workflow.failed === true || (workflow.status === 'completed' && !['success', 'skipped', ''].includes(workflow.conclusion || ''));
      document.getElementById('builderWorkflowState').textContent = workflowFailed ? '构建失败：' + (workflow.conclusion || 'unknown') : workflow.status !== 'completed' ? (['queued', 'in_progress', 'pending'].includes(workflow.status) ? '正在编译' : '等待检测') : workflow.conclusion === 'success' ? '构建通过' : workflow.conclusion || '已完成';
      document.getElementById('builderWorkflowState').closest('.channel-row').classList.toggle('build-failure', workflowFailed);
      const workflowLink = document.getElementById('builderWorkflowLink');
      const workflowUrl = typeof workflow.html_url === 'string' && workflow.html_url.startsWith('https://github.com/') ? workflow.html_url : '';
      workflowLink.hidden = !workflowUrl;
      if (workflowUrl) workflowLink.href = workflowUrl;
      document.getElementById('pluginUpdateChecked').textContent = '检测时间：' + (pluginUpdate.checked_at || '尚未检测');
      const pluginAuto = data.plugin_auto_update || {{}};
      const pluginOperation = data.plugin_operation || {{}};
      const operationActive = pluginOperation.active === true;
      document.getElementById('pluginAutoEnabled').textContent = pluginAuto.enabled === true ? '已启用' : '已停用';
      document.getElementById('pluginAutoChecked').textContent = pluginAuto.last_checked_at || '尚未运行';
      const autoLabels = {{ no_update:'无更新', ready:'已下载，等待应用', apply_queued:'已进入后台应用队列', applying:'正在应用', apply_failed:'应用失败，可重试', updated:'已完成更新', restart_pending:'等待健康检查', rollback_pending:'正在恢复旧版', rolled_back:'候选失败，已恢复旧版', waiting_builder:'官方已更新，等待仓库编译', build_failed:'GitHub 编译失败，保留旧版', failed:'仓库监控失败，保留旧版', disabled:'已停用', building:'GitHub 正在编译', downloading:'正在下载已验证版本' }};
      document.getElementById('pluginAutoResult').textContent = autoLabels[pluginAuto.last_result] || pluginAuto.last_result || pluginAuto.status || '尚未运行';
      document.getElementById('pluginAutoError').textContent = pluginAuto.last_error || '';
      const progress = Math.max(0, Math.min(100, Number(pluginAuto.progress || 0)));
      const progressBar = document.getElementById('pluginAutoProgressBar');
      progressBar.style.width = progress + '%';
      progressBar.classList.toggle('failed', pluginAuto.status === 'apply_failed');
      progressBar.classList.toggle('rolled-back', pluginAuto.status === 'rolled_back');
      document.getElementById('pluginAutoProgressLabel').textContent = Math.round(progress) + '%';
      document.getElementById('pluginAutoStage').textContent = pluginAuto.stage || '尚未开始';
      const prepared = pluginAuto.prepared || {{}};
      const preparedReady = pluginUpdate.official_ahead !== true && ((pluginAuto.status === 'ready' && prepared.status === 'ready') || (pluginAuto.status === 'apply_failed' && ['ready', 'staged'].includes(prepared.status)));
      document.getElementById('pluginAutoPrepared').textContent = prepared.version && prepared.binary_sha256 ? '已验证候选 ' + prepared.version + ' · SHA256 ' + prepared.binary_sha256.slice(0, 16) + '...' : '暂无已验证候选包';
      document.getElementById('pluginApplyTag').value = prepared.release_tag || '';
      document.getElementById('pluginApplySha').value = prepared.binary_sha256 || '';
      const applyButton = document.getElementById('pluginApplyButton');
      const unavailableLabels = {{ apply_queued:'应用任务排队中', applying:'正在备份并应用', restart_pending:'正在重启并检查', rollback_pending:'正在自动恢复旧版', rolled_back:'已恢复旧版', apply_failed:'请先执行回退', waiting_builder:'等待仓库跟进官方版本', building:'仓库编译中', failed:'构建失败，保持旧版', downloading:'正在下载候选包' }};
      applyButton.disabled = operationActive || !preparedReady;
      applyButton.textContent = preparedReady && !operationActive ? (pluginAuto.status === 'apply_failed' ? '重新应用已验证版本' : '应用已验证版本') : unavailableLabels[pluginAuto.status] || '暂无可应用版本';
      pluginOperationActive = operationActive;
    }}
    let pluginOperationActive = {str(auto_active).lower()};
    let serverRefreshRunning = false;
    let serverRefreshTimer = null;
    async function refreshServerStatus() {{
      if (serverRefreshRunning) return;
      serverRefreshRunning = true;
      let delay = pluginOperationActive ? 1000 : 30000;
      try {{
        const response = await fetch('/api/server-status', {{ cache: 'no-store' }});
        if (response.ok) updateServerStatus(await response.json());
        delay = pluginOperationActive ? 1000 : 30000;
      }} catch (_) {{
        delay = pluginOperationActive ? 2000 : 10000;
      }} finally {{
        serverRefreshRunning = false;
        clearTimeout(serverRefreshTimer);
        serverRefreshTimer = setTimeout(refreshServerStatus, delay);
      }}
    }}
    const applyForm = document.getElementById('pluginApplyForm');
    applyForm.addEventListener('submit', async event => {{
      event.preventDefault();
      if (!confirm({json.dumps(auto_apply_confirm, ensure_ascii=False)})) return;
      const applyButton = document.getElementById('pluginApplyButton');
      applyButton.disabled = true;
      applyButton.textContent = '正在启动后台任务';
      document.getElementById('pluginAutoError').textContent = '';
      try {{
        const response = await fetch('/plugin', {{
          method: 'POST',
          headers: {{ Accept: 'application/json' }},
          body: new FormData(applyForm),
        }});
        const result = await response.json();
        if (!response.ok || result.ok !== true) throw new Error(result.message || '后台应用任务启动失败');
        pluginOperationActive = true;
        document.getElementById('pluginAutoResult').textContent = '已进入后台应用队列';
        document.getElementById('pluginAutoStage').textContent = result.message || '正在执行完整备份';
        document.getElementById('pluginAutoProgressBar').style.width = '1%';
        document.getElementById('pluginAutoProgressLabel').textContent = '1%';
        await refreshServerStatus();
      }} catch (error) {{
        pluginOperationActive = false;
        applyButton.disabled = false;
        applyButton.textContent = '重新应用已验证版本';
        document.getElementById('pluginAutoError').textContent = error instanceof Error ? error.message : '后台应用任务启动失败';
      }}
    }});
    setInterval(refreshIpStatus, 60000);
    serverRefreshTimer = setTimeout(refreshServerStatus, pluginOperationActive ? 500 : 30000);
  </script>
</body>
</html>"""
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def download(self, encoded_name):
        name = unquote(encoded_name)
        if not BACKUP_NAME.fullmatch(name):
            self.send_error(404)
            return
        path = (BACKUP_DIR / name).resolve()
        if path.parent != BACKUP_DIR or not path.is_file():
            self.send_error(404)
            return
        size = path.stat().st_size
        self.send_response(200)
        self.send_common_headers()
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with path.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, length=1024 * 1024)


if __name__ == "__main__":
    threading.Thread(target=public_ip_loop, name="public-ip-monitor", daemon=True).start()
    threading.Thread(target=email_loop, name="email-monitor", daemon=True).start()
    ThreadingHTTPServer((WEB_HOST, WEB_PORT), BackupHandler).serve_forever()
