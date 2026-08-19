#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "$EUID" -ne 0 ]]; then
  printf 'run as root\n' >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PANEL=/opt/sub2api-backup-web/backup_web.py
CONTROL=/usr/local/sbin/sub2api-plugin-control
SUDOERS=/etc/sudoers.d/sub2api-plugin-control
DROPIN_DIR=/etc/systemd/system/sub2api-backup-web.service.d
DROPIN="$DROPIN_DIR/weekly-overdraft-control.conf"
APPLY_UNIT=/etc/systemd/system/sub2api-overdraft-apply.service
APPLY_FAILED_UNIT=/etc/systemd/system/sub2api-overdraft-apply-failed.service
PANEL_SERVICE=sub2api-backup-web.service
BACKUP_ROOT=/var/backups/sub2api/manual/panel-before-weekly-overdraft
LOCK_FILE=/run/lock/sub2api-backup-panel-install.lock

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  printf 'another port 2222 integration install is running\n' >&2
  exit 1
fi

if [[ ! -f "$PANEL" ]]; then
  printf 'the port 2222 backup panel is not installed\n' >&2
  exit 1
fi

SOURCES=(
  "$SOURCE_DIR/backup-panel/backup_web.py"
  "$SOURCE_DIR/sub2api-plugin-control"
  "$SOURCE_DIR/sub2api-plugin-control.sudoers"
  "$SOURCE_DIR/backup-panel/weekly-overdraft-control.conf"
  "$SOURCE_DIR/../systemd/sub2api-overdraft-apply.service"
  "$SOURCE_DIR/../systemd/sub2api-overdraft-apply-failed.service"
)
TRANSACTION_TARGETS=(
  "$PANEL"
  "$CONTROL"
  "$SUDOERS"
  "$DROPIN"
  "$APPLY_UNIT"
  "$APPLY_FAILED_UNIT"
)

for source_file in "${SOURCES[@]}"; do
  if [[ ! -f "$source_file" ]]; then
    printf 'missing install source: %s\n' "$source_file" >&2
    exit 1
  fi
done

# Validate inputs before taking a snapshot or touching live files.
visudo -cf "$SOURCE_DIR/sub2api-plugin-control.sudoers"
python3 - "$SOURCE_DIR/backup-panel/backup_web.py" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1])
compile(source.read_bytes(), str(source), "exec")
PY

stamp="$(date '+%Y%m%d-%H%M%S')"
install -d -o root -g root -m 0700 "$BACKUP_ROOT"
transaction_dir="$(mktemp -d "$BACKUP_ROOT/install-$stamp.XXXXXX")"
install -d -o root -g root -m 0700 "$transaction_dir/targets"

panel_was_active=0
if systemctl is-active --quiet "$PANEL_SERVICE"; then
  panel_was_active=1
fi
dropin_dir_was_present=0
if [[ -d "$DROPIN_DIR" ]]; then
  dropin_dir_was_present=1
fi

snapshot_targets() {
  local index target snapshot state
  : >"$transaction_dir/manifest.tsv"
  for index in "${!TRANSACTION_TARGETS[@]}"; do
    target="${TRANSACTION_TARGETS[$index]}"
    snapshot="$transaction_dir/targets/$index"
    state=absent
    if [[ -e "$target" || -L "$target" ]]; then
      cp -a -- "$target" "$snapshot"
      state=present
    fi
    printf '%s\n' "$state" >"$transaction_dir/targets/$index.state"
    printf '%s\t%s\n' "$state" "$target" >>"$transaction_dir/manifest.tsv"
  done
}

transaction_started=0
transaction_complete=0

restore_transaction() {
  local index target snapshot state temporary rollback_failed=0 restored_ready _attempt
  printf 'installation failed; restoring previous port 2222 integration\n' >&2
  for ((index=${#TRANSACTION_TARGETS[@]} - 1; index >= 0; index--)); do
    target="${TRANSACTION_TARGETS[$index]}"
    snapshot="$transaction_dir/targets/$index"
    state="$(<"$transaction_dir/targets/$index.state")"
    temporary="${target}.weekly-overdraft-rollback.$$"
    rm -f -- "${target}.weekly-overdraft-install.$$" "$temporary"
    if [[ "$state" == present ]]; then
      if ! cp -a -- "$snapshot" "$temporary" || ! mv -Tf -- "$temporary" "$target"; then
        printf 'failed to restore %s\n' "$target" >&2
        rollback_failed=1
      fi
    elif ! rm -f -- "$target"; then
      printf 'failed to remove newly installed %s\n' "$target" >&2
      rollback_failed=1
    fi
  done

  if [[ "$dropin_dir_was_present" -eq 0 ]]; then
    rmdir "$DROPIN_DIR" 2>/dev/null || true
  fi
  if ! systemctl daemon-reload; then
    printf 'failed to reload systemd while restoring the previous integration\n' >&2
    rollback_failed=1
  fi
  if [[ "$panel_was_active" -eq 1 ]]; then
    if ! systemctl restart "$PANEL_SERVICE"; then
      printf 'failed to restart the restored port 2222 panel\n' >&2
      rollback_failed=1
    else
      restored_ready=0
      for _attempt in {1..10}; do
        if curl -fsS --max-time 2 http://127.0.0.1:2222/healthz >/dev/null 2>&1; then
          restored_ready=1
          break
        fi
        sleep 1
      done
      if [[ "$restored_ready" -ne 1 ]]; then
        printf 'restored port 2222 panel did not become healthy\n' >&2
        rollback_failed=1
      fi
    fi
  elif ! systemctl stop "$PANEL_SERVICE"; then
    printf 'failed to restore the stopped state of the port 2222 panel\n' >&2
    rollback_failed=1
  fi

  if [[ "$rollback_failed" -eq 0 ]]; then
    printf 'rolled_back\n' >"$transaction_dir/result"
    printf 'previous port 2222 integration restored\n' >&2
  else
    printf 'rollback_incomplete\n' >"$transaction_dir/result"
  fi
}

on_exit() {
  local exit_code=$?
  trap - EXIT INT TERM
  set +e
  if [[ "$transaction_started" -eq 1 && "$transaction_complete" -ne 1 ]]; then
    restore_transaction
    if [[ "$exit_code" -eq 0 ]]; then
      exit_code=1
    fi
  fi
  exit "$exit_code"
}

trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

snapshot_targets
transaction_started=1

install_replacement() {
  local owner=$1 group=$2 mode=$3 source=$4 target=$5
  local temporary="${target}.weekly-overdraft-install.$$"
  rm -f -- "$temporary"
  install -o "$owner" -g "$group" -m "$mode" "$source" "$temporary"
  mv -Tf -- "$temporary" "$target"
}

if [[ ! -d "$DROPIN_DIR" ]]; then
  install -d -o root -g root -m 0755 "$DROPIN_DIR"
fi
install_replacement root root 0755 "$SOURCE_DIR/sub2api-plugin-control" "$CONTROL"
install_replacement root root 0440 "$SOURCE_DIR/sub2api-plugin-control.sudoers" "$SUDOERS"
visudo -cf "$SUDOERS"
install_replacement root root 0644 "$SOURCE_DIR/backup-panel/backup_web.py" "$PANEL"
install_replacement root root 0644 \
  "$SOURCE_DIR/backup-panel/weekly-overdraft-control.conf" "$DROPIN"
install_replacement root root 0644 \
  "$SOURCE_DIR/../systemd/sub2api-overdraft-apply.service" "$APPLY_UNIT"
install_replacement root root 0644 \
  "$SOURCE_DIR/../systemd/sub2api-overdraft-apply-failed.service" "$APPLY_FAILED_UNIT"

systemctl daemon-reload
systemctl restart "$PANEL_SERVICE"
panel_ready=0
for _attempt in {1..30}; do
  if curl -fsS --max-time 2 http://127.0.0.1:2222/healthz >/dev/null 2>&1; then
    panel_ready=1
    break
  fi
  sleep 1
done
if [[ "$panel_ready" -ne 1 ]]; then
  systemctl status "$PANEL_SERVICE" --no-pager -l >&2 || true
  printf 'port 2222 backup panel did not become healthy within 30 seconds\n' >&2
  exit 1
fi

printf 'committed\n' >"$transaction_dir/result"
transaction_complete=1
printf 'port 2222 plugin controls installed\n'
