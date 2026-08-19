#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$EUID" -ne 0 ]]; then
  printf 'run as root\n' >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PANEL=/opt/sub2api-backup-web/backup_web.py
if [[ ! -f "$PANEL" ]]; then
  printf 'the port 2222 backup panel is not installed\n' >&2
  exit 1
fi

stamp="$(date '+%Y%m%d-%H%M%S')"
install -d -o root -g root -m 0700 /var/backups/sub2api/manual/panel-before-weekly-overdraft
install -o root -g root -m 0600 "$PANEL" \
  "/var/backups/sub2api/manual/panel-before-weekly-overdraft/backup_web.py.$stamp"
install -o root -g root -m 0755 "$SOURCE_DIR/sub2api-plugin-control" \
  /usr/local/sbin/sub2api-plugin-control
install -o root -g root -m 0440 "$SOURCE_DIR/sub2api-plugin-control.sudoers" \
  /etc/sudoers.d/sub2api-plugin-control
visudo -cf /etc/sudoers.d/sub2api-plugin-control
install -o root -g root -m 0644 "$SOURCE_DIR/backup-panel/backup_web.py" "$PANEL"
install -d -o root -g root -m 0755 /etc/systemd/system/sub2api-backup-web.service.d
install -o root -g root -m 0644 \
  "$SOURCE_DIR/backup-panel/weekly-overdraft-control.conf" \
  /etc/systemd/system/sub2api-backup-web.service.d/weekly-overdraft-control.conf
install -o root -g root -m 0644 \
  "$SOURCE_DIR/../systemd/sub2api-overdraft-apply.service" \
  /etc/systemd/system/sub2api-overdraft-apply.service
install -o root -g root -m 0644 \
  "$SOURCE_DIR/../systemd/sub2api-overdraft-apply-failed.service" \
  /etc/systemd/system/sub2api-overdraft-apply-failed.service
python3 -m py_compile "$PANEL"
systemctl daemon-reload
systemctl restart sub2api-backup-web.service
panel_ready=0
for _attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:2222/healthz >/dev/null 2>&1; then
    panel_ready=1
    break
  fi
  sleep 1
done
if [[ "$panel_ready" -ne 1 ]]; then
  systemctl status sub2api-backup-web.service --no-pager -l >&2 || true
  printf 'port 2222 backup panel did not become healthy within 30 seconds\n' >&2
  exit 1
fi
printf 'port 2222 plugin controls installed\n'
