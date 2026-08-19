#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLUGIN_DIR="${SUB2API_PLUGIN_DIR:-/opt/sub2api/plugins/weekly-overdraft}"
STATE_DIR="${SUB2API_STATE_ROOT:-/var/lib/sub2api-weekly-overdraft}"
ENV_FILE="${SUB2API_MANAGER_ENV:-/etc/sub2api/weekly-overdraft-manager.env}"
DROPIN_DIR="/etc/systemd/system/sub2api.service.d"

install -d -m 0750 -o sub2api -g sub2api "$PLUGIN_DIR" "$STATE_DIR"
cp -a "$SOURCE_DIR/." "$PLUGIN_DIR/"
chmod 0750 "$PLUGIN_DIR/manager.sh" "$PLUGIN_DIR/manager.py" "$PLUGIN_DIR/auto_update.py"
chown -R sub2api:sub2api "$PLUGIN_DIR" "$STATE_DIR"

if [[ ! -f "$STATE_DIR/runtime.env" ]]; then
  install -m 0640 -o sub2api -g sub2api /dev/null "$STATE_DIR/runtime.env"
  printf 'GATEWAY_CODEX_QUOTA_OVERDRAFT_ENABLED=false\n' >"$STATE_DIR/runtime.env"
fi

if [[ ! -f "$STATE_DIR/auto-update.json" ]]; then
  install -m 0640 -o sub2api -g sub2api /dev/null "$STATE_DIR/auto-update.json"
  printf '%s\n' '{"enabled":true,"status":"enabled","min_interval_hours":3,"max_interval_hours":5}' >"$STATE_DIR/auto-update.json"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  install -m 0640 -o root -g sub2api "$SOURCE_DIR/config/manager.env.example" "$ENV_FILE"
  echo "created $ENV_FILE; set the PostgreSQL password before any upgrade" >&2
fi

install -d -m 0755 "$DROPIN_DIR"
install -m 0644 "$SOURCE_DIR/systemd/weekly-overdraft-manager.conf" "$DROPIN_DIR/weekly-overdraft-manager.conf"
install -m 0644 "$SOURCE_DIR/systemd/sub2api-overdraft-auto-update.service" /etc/systemd/system/sub2api-overdraft-auto-update.service
install -m 0644 "$SOURCE_DIR/systemd/sub2api-overdraft-auto-update.timer" /etc/systemd/system/sub2api-overdraft-auto-update.timer
systemctl daemon-reload
systemctl enable --now sub2api-overdraft-auto-update.timer

echo "plugin files installed; automatic 3-5 hour update timer is enabled"
echo "next: review $ENV_FILE, then run $PLUGIN_DIR/manager.sh verify 0.1.178 --channel official"
echo "overdraft build: $PLUGIN_DIR/manager.sh verify 0.1.178 --channel overdraft"
