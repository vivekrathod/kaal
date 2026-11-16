#!/usr/bin/env bash
# Server half of deploy/deploy.sh. It is piped to `sudo bash -s` on the server,
# so it must not read stdin and must work as root.
#
#   status                         print what is installed and whether it is healthy
#   install WHEEL COMMIT WITH_DEPS back up, snapshot, install, restart, check;
#                                  restore the snapshot if the service is unhealthy
#   rollback                       restore the newest snapshot
set -euo pipefail

PREFIX="${KAAL_PREFIX:-/opt/kaal}"
VENV="$PREFIX/venv"
PY="$VENV/bin/python"
ROLLBACK_DIR="$PREFIX/rollback"
RELEASE_FILE="$PREFIX/RELEASE"
LOCK_FILE="$PREFIX/requirements.lock"
SERVICE="${KAAL_SERVICE:-kaal-server.service}"
BACKUP_UNIT="${KAAL_BACKUP_UNIT:-kaal-backup.service}"
HEALTH_URL="http://127.0.0.1:${KAAL_PORT:-8765}/v1/health"
KEEP_SNAPSHOTS=5
# Who has an account and who belongs to which vault. A newer Kaal may upgrade
# this file's layout, which an older Kaal then refuses to open, so a rollback
# has to put back the copy taken before the install.
ACCOUNTS_FILE="${KAAL_ACCOUNTS_FILE:-${KAAL_HOME:-/srv/kaal}/accounts.db}"

# The layout version of an accounts file (0 when it cannot be read).
accounts_version() {
  "$PY" - "$1" <<'PYEOF' 2>/dev/null || echo 0
import sqlite3, sys
db = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(db.execute("PRAGMA user_version").fetchone()[0])
PYEOF
}

say() { printf '%s\n' "$*"; }
fail() { printf 'deploy: %s\n' "$*" >&2; exit 1; }

installed_version() {
  "$PY" -c 'import importlib.metadata as m; print(m.version("kaal"))'
}

# Paths of everything pip owns for kaal, relative to the venv.
package_paths() {
  "$PY" -c '
import importlib.metadata as m, os, sys, sysconfig
venv = sys.prefix
site = sysconfig.get_paths()["purelib"]
dist = m.distribution("kaal")
paths = [os.path.join(site, "kaal"), str(dist._path)]
paths += [os.path.join(venv, "bin", name) for name in ("kaal", "kaal-server")]
for path in paths:
    if os.path.exists(path):
        print(os.path.relpath(path, venv))
'
}

healthy() {
  local _
  for _ in $(seq 1 30); do
    if systemctl is-active --quiet "$SERVICE" && curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

snapshot() {
  local version name
  version="$(installed_version)"
  name="$(date -u +%Y%m%dT%H%M%SZ)-$version"
  mkdir -p "$ROLLBACK_DIR"
  chmod 700 "$ROLLBACK_DIR"
  # shellcheck disable=SC2046  # one path per line, none contain spaces
  tar -C "$VENV" -cf "$ROLLBACK_DIR/$name.tar" $(package_paths)
  [ -f "$RELEASE_FILE" ] && cp "$RELEASE_FILE" "$ROLLBACK_DIR/$name.release"
  [ -f "$LOCK_FILE" ] && cp "$LOCK_FILE" "$ROLLBACK_DIR/$name.lock"
  if [ -f "$ACCOUNTS_FILE" ]; then
    # SQLite's own backup, so the copy is whole even while the server is writing.
    ( umask 077; "$PY" - "$ACCOUNTS_FILE" "$ROLLBACK_DIR/$name.accounts.db" <<'PYEOF'
import sqlite3, sys
source, target = sqlite3.connect(sys.argv[1], timeout=15), sqlite3.connect(sys.argv[2])
source.backup(target)
target.close()
source.close()
PYEOF
    ) || fail "could not copy the accounts file; nothing was changed"
  fi
  say "$ROLLBACK_DIR/$name"
}

restore() {
  local base="$1" path
  [ -f "$base.tar" ] || fail "snapshot $base.tar is missing"
  package_paths | while IFS= read -r path; do
    rm -rf "${VENV:?}/$path"
  done
  tar -C "$VENV" -xf "$base.tar"
  if [ -f "$base.release" ]; then cp "$base.release" "$RELEASE_FILE"; else rm -f "$RELEASE_FILE"; fi
  if [ -f "$base.lock" ]; then cp "$base.lock" "$LOCK_FILE"; fi
  # Put the accounts file back only if the version being left upgraded its
  # layout: otherwise the current one is fine and holds anything added since.
  if [ -f "$base.accounts.db" ] && [ -f "$ACCOUNTS_FILE" ] && [ "$(accounts_version "$ACCOUNTS_FILE")" -gt "$(accounts_version "$base.accounts.db")" ]; then
    systemctl stop "$SERVICE" || true
    cp -p "$ACCOUNTS_FILE" "$ACCOUNTS_FILE.newer-layout"
    cat "$base.accounts.db" > "$ACCOUNTS_FILE"
    rm -f "$ACCOUNTS_FILE-wal" "$ACCOUNTS_FILE-shm"
    say "    accounts file put back as it was before the install (the newer one is kept beside it)"
  fi
}

newest_snapshot() {
  local newest
  newest="$(find "$ROLLBACK_DIR" -maxdepth 1 -name '*.tar' 2>/dev/null | sort | tail -n 1)"
  [ -n "$newest" ] || return 1
  say "${newest%.tar}"
}

drop_snapshot() {
  rm -f "$1.tar" "$1.release" "$1.lock" "$1.accounts.db"
}

prune_snapshots() {
  local count
  count="$(find "$ROLLBACK_DIR" -maxdepth 1 -name '*.tar' | wc -l | tr -d ' ')"
  [ "$count" -gt "$KEEP_SNAPSHOTS" ] || return 0
  find "$ROLLBACK_DIR" -maxdepth 1 -name '*.tar' | sort | head -n "$((count - KEEP_SNAPSHOTS))" | while IFS= read -r old; do
    drop_snapshot "${old%.tar}"
  done
}

cmd_status() {
  say "version: $(installed_version)"
  if [ -f "$RELEASE_FILE" ]; then cat "$RELEASE_FILE"; else say "commit: unknown (installed before deploy.sh)"; fi
  say "service: $(systemctl is-active "$SERVICE" || true)"
  if curl -fsS -m 3 "$HEALTH_URL" >/dev/null 2>&1; then say "health: ok"; else say "health: failing"; fi
  say "snapshots: $(find "$ROLLBACK_DIR" -maxdepth 1 -name '*.tar' 2>/dev/null | wc -l | tr -d ' ')"
}

cmd_install() {
  local wheel="$1" commit="$2" with_deps="$3" base version
  case "$wheel" in /tmp/kaal-deploy-*/kaal-*.whl) ;; *) fail "unexpected wheel path" ;; esac
  [ -f "$wheel" ] || fail "wheel not found on the server"
  case "$commit" in *[!0-9a-f]*|"") fail "commit must be a hexadecimal id" ;; esac

  say "==> Backing up the vault"
  systemctl start "$BACKUP_UNIT" || fail "backup failed; nothing was changed"

  say "==> Saving the installed version"
  base="$(snapshot)"

  say "==> Installing"
  if ! {
    "$PY" -m pip install --quiet --disable-pip-version-check --no-deps --force-reinstall "$wheel" &&
    if [ "$with_deps" = "1" ]; then
      "$PY" -m pip install --quiet --disable-pip-version-check -c "$LOCK_FILE" "${wheel}[server,extract]" &&
      "$PY" -m pip freeze --disable-pip-version-check --exclude kaal > "$LOCK_FILE.new" &&
      mv "$LOCK_FILE.new" "$LOCK_FILE"
    fi
  }; then
    restore "$base"
    drop_snapshot "$base"
    fail "install failed; the previous version was restored and the service was not restarted"
  fi
  version="$(installed_version)"
  printf 'commit: %s\nversion: %s\ndeployed_at: %s\n' "$commit" "$version" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RELEASE_FILE"

  say "==> Restarting $SERVICE"
  systemctl restart "$SERVICE" || true
  if healthy; then
    prune_snapshots
    rm -rf "$(dirname "$wheel")"
    say "==> Deployed $version ($commit)"
    return 0
  fi

  say "==> Unhealthy after restart; restoring the previous version"
  restore "$base"
  # The snapshot now equals what is installed, so a later rollback must not pick it.
  drop_snapshot "$base"
  systemctl restart "$SERVICE" || true
  if healthy; then
    fail "deploy failed and was rolled back; the previous version is running"
  fi
  fail "deploy failed and the previous version is ALSO unhealthy; check: journalctl -u $SERVICE"
}

cmd_rollback() {
  local base
  base="$(newest_snapshot)" || fail "no snapshot to roll back to"
  say "==> Restoring $(basename "$base")"
  restore "$base"
  systemctl restart "$SERVICE" || true
  healthy || fail "the restored version is unhealthy; check: journalctl -u $SERVICE"
  drop_snapshot "$base"
  say "==> Rolled back to $(installed_version)"
}

[ "$(id -u)" = "0" ] || fail "must run as root"
[ -x "$PY" ] || fail "$PY not found"

# Only commands that change the server take the lock, so status writes nothing.
take_lock() {
  exec 9>"$PREFIX/.deploy.lock"
  flock -n 9 || fail "another deploy is running"
}

case "${1:-}" in
  status) cmd_status ;;
  install) [ $# -eq 4 ] || fail "usage: install WHEEL COMMIT WITH_DEPS"; take_lock; cmd_install "$2" "$3" "$4" ;;
  rollback) take_lock; cmd_rollback ;;
  *) fail "usage: status | install WHEEL COMMIT WITH_DEPS | rollback" ;;
esac
