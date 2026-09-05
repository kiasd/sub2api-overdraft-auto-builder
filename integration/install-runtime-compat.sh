#!/usr/bin/env bash
set -Eeuo pipefail

# One-time maintenance for installations whose panel/runtime predates the
# codexrip/custom fusion tag formats. The operation never touches the
# Sub2API binary or database.
if [[ "${EUID}" -ne 0 ]]; then
  printf 'run as root\n' >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANAGER_SOURCE="${SUB2API_MANAGER_SOURCE:-$SOURCE_DIR/../manager.py}"
CONTROL_SOURCE="${SUB2API_CONTROL_SOURCE:-$SOURCE_DIR/sub2api-plugin-control}"
PLUGIN_DIR="${SUB2API_PLUGIN_DIR:-/opt/sub2api/plugins/weekly-overdraft}"
MANAGER_TARGET="$PLUGIN_DIR/manager.py"
CONTROL_TARGET="${SUB2API_CONTROL_TARGET:-/usr/local/sbin/sub2api-plugin-control}"
STATE_ROOT="${SUB2API_STATE_ROOT:-/var/lib/sub2api-weekly-overdraft}"
PANEL_SERVICE="${SUB2API_PANEL_SERVICE:-sub2api-backup-web.service}"
PANEL_HEALTH_URL="${SUB2API_PANEL_HEALTH_URL:-http://127.0.0.1:2222/healthz}"
LOCK_FILE="/run/lock/sub2api-runtime-compat-install.lock"
BACKUP_ROOT="${SUB2API_COMPAT_BACKUP_ROOT:-/var/backups/sub2api/manual/runtime-compat}"

for source in "$MANAGER_SOURCE" "$CONTROL_SOURCE"; do
  [[ -f "$source" && ! -L "$source" ]] || {
    printf 'missing or unsafe source: %s\n' "$source" >&2
    exit 1
  }
done
[[ -d "$PLUGIN_DIR" ]] || {
  printf 'plugin directory does not exist: %s\n' "$PLUGIN_DIR" >&2
  exit 1
}
[[ -d "$STATE_ROOT" ]] || {
  printf 'state directory does not exist: %s\n' "$STATE_ROOT" >&2
  exit 1
}

install -d -o root -g root -m 0700 "$(dirname "$LOCK_FILE")" "$BACKUP_ROOT"
exec 9>"$LOCK_FILE"
flock -n 9 || {
  printf 'another runtime compatibility install is running\n' >&2
  exit 1
}

apply_state="$(systemctl show --property=ActiveState --value sub2api-overdraft-apply.service 2>/dev/null || true)"
if [[ "$apply_state" == activating || "$apply_state" == active || "$apply_state" == reloading || "$apply_state" == deactivating ]]; then
  printf 'an apply task is running; retry after it finishes\n' >&2
  exit 1
fi

# Validate before taking or changing anything. py_compile is directed to a
# temporary cache so the source checkout remains untouched.
pycache_dir="$(mktemp -d)"
if ! PYTHONPYCACHEPREFIX="$pycache_dir" python3 -m py_compile "$MANAGER_SOURCE"; then
  rm -rf -- "$pycache_dir"
  exit 1
fi
rm -rf -- "$pycache_dir"
bash -n "$CONTROL_SOURCE"
python3 - "$MANAGER_SOURCE" <<'PY'
import importlib.util
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("compat_manager", path)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load manager source")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tag = "fusion-v0.2.0-codexrip.4-aa236488-e6a99b21-ub7c3ddd7"
if not module.FUSION_RELEASE_TAG_RE.fullmatch(tag):
    raise SystemExit("manager source does not accept the verified codexrip tag")
PY

stamp="$(date -u '+%Y%m%dT%H%M%SZ')"
transaction_dir="$BACKUP_ROOT/$stamp"
install -d -o root -g root -m 0700 "$transaction_dir/targets"
targets=("$MANAGER_TARGET" "$CONTROL_TARGET")
panel_was_active=0
if systemctl is-active --quiet "$PANEL_SERVICE"; then
  panel_was_active=1
fi
sub2api_was_active=0
if systemctl is-active --quiet sub2api.service; then
  sub2api_was_active=1
fi

transaction_started=0

snapshot_targets() {
  local index target snapshot state
  : >"$transaction_dir/manifest.tsv"
  for index in "${!targets[@]}"; do
    target="${targets[$index]}"
    snapshot="$transaction_dir/targets/$index"
    state=absent
    if [[ -e "$target" || -L "$target" ]]; then
      cp -a -- "$target" "$snapshot"
      state=present
    fi
    printf '%s\n' "$state" >"$transaction_dir/targets/$index.state"
    printf '%s\t%s\n' "$state" "$target" >>"$transaction_dir/manifest.tsv"
  done
  transaction_started=1
}

restore_targets() {
  local index target snapshot state temporary rollback_failed=0
  for ((index=${#targets[@]} - 1; index >= 0; index--)); do
    target="${targets[$index]}"
    snapshot="$transaction_dir/targets/$index"
    if [[ ! -f "$transaction_dir/targets/$index.state" ]]; then
      continue
    fi
    state="$(<"$transaction_dir/targets/$index.state")"
    temporary="${target}.compat-rollback.$$"
    rm -f -- "$temporary"
    if [[ "$state" == present ]]; then
      if ! cp -a -- "$snapshot" "$temporary" || ! mv -Tf -- "$temporary" "$target"; then
        rollback_failed=1
      fi
    else
      if ! rm -f -- "$target"; then
        rollback_failed=1
      fi
    fi
  done
  return "$rollback_failed"
}

transaction_complete=0
on_exit() {
  local exit_code=$?
  trap - EXIT INT TERM
  set +e
  if [[ "$transaction_started" -eq 1 && "$transaction_complete" -ne 1 ]]; then
    rollback_failed=0
    restore_targets || rollback_failed=1
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ "$panel_was_active" -eq 1 ]]; then
      systemctl restart "$PANEL_SERVICE" >/dev/null 2>&1 || true
    fi
    if [[ "$rollback_failed" -eq 0 ]]; then
      printf 'runtime compatibility install failed; previous files restored\n' >&2
    else
      printf 'runtime compatibility install failed; rollback was incomplete\n' >&2
    fi
    [[ "$exit_code" -ne 0 ]] || exit_code=1
  fi
  exit "$exit_code"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

snapshot_targets

install_replacement() {
  local owner=$1 group=$2 mode=$3 source=$4 target=$5
  local temporary="${target}.compat-install.$$"
  install -o "$owner" -g "$group" -m "$mode" "$source" "$temporary"
  mv -Tf -- "$temporary" "$target"
}

install_replacement sub2api sub2api 0750 "$MANAGER_SOURCE" "$MANAGER_TARGET"
install_replacement root root 0755 "$CONTROL_SOURCE" "$CONTROL_TARGET"

[[ "$(sha256sum "$MANAGER_SOURCE" | awk '{print $1}')" == "$(sha256sum "$MANAGER_TARGET" | awk '{print $1}')" ]]
[[ "$(sha256sum "$CONTROL_SOURCE" | awk '{print $1}')" == "$(sha256sum "$CONTROL_TARGET" | awk '{print $1}')" ]]
python3 - "$MANAGER_TARGET" <<'PY'
import importlib.util
import sys

path = sys.argv[1]
spec = importlib.util.spec_from_file_location("installed_manager", path)
if spec is None or spec.loader is None:
    raise SystemExit("cannot load installed manager")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tag = "fusion-v0.2.0-codexrip.4-aa236488-e6a99b21-ub7c3ddd7"
if not module.FUSION_RELEASE_TAG_RE.fullmatch(tag):
    raise SystemExit("installed manager rejected the verified codexrip tag")
PY

if [[ "$panel_was_active" -eq 1 ]]; then
  systemctl restart "$PANEL_SERVICE"
  panel_ready=0
  for _attempt in {1..30}; do
    if curl -fsS --max-time 2 "$PANEL_HEALTH_URL" >/dev/null 2>&1; then
      panel_ready=1
      break
    fi
    sleep 1
  done
  [[ "$panel_ready" -eq 1 ]] || {
    printf 'panel did not become healthy after runtime update\n' >&2
    exit 1
  }
fi
if [[ "$sub2api_was_active" -eq 1 ]]; then
  systemctl is-active --quiet sub2api.service
fi

printf 'committed\n' >"$transaction_dir/result"
transaction_complete=1
printf 'runtime compatibility installed\n'
printf 'backup_dir=%s\n' "$transaction_dir"
printf 'manager_sha256=%s\n' "$(sha256sum "$MANAGER_TARGET" | awk '{print $1}')"
printf 'control_sha256=%s\n' "$(sha256sum "$CONTROL_TARGET" | awk '{print $1}')"
