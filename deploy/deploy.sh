#!/usr/bin/env bash
# Deploy one commit of Kaal to a server that runs kaal-server under systemd,
# or roll the last deploy back.
#
#   deploy/deploy.sh --host USER@SERVER REF          test, build and deploy REF
#   deploy/deploy.sh --host USER@SERVER --status     show what is installed
#   deploy/deploy.sh --host USER@SERVER --rollback   restore the previous version
#
# REF is any git ref; exactly that commit is deployed, never the working tree.
# The server side (deploy-remote.sh) backs the vault up first, keeps a snapshot
# of the installed version, checks health after the restart and restores the
# snapshot if the check fails. It needs passwordless sudo on the server.
#
# Options:
#   --host H        ssh destination (or set KAAL_DEPLOY_HOST)
#   --dry-run       test and build, show what would happen, change nothing
#   --yes           do not ask for confirmation
#   --with-deps     also install new dependencies and refresh requirements.lock
#   --skip-tests    do not run the test suite on REF first
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
remote_script="$here/deploy-remote.sh"

host="${KAAL_DEPLOY_HOST:-}"
python="${KAAL_DEPLOY_PYTHON:-$repo/.venv/bin/python}"
mode="deploy"
ref=""
dry_run=0
assume_yes=0
with_deps=0
skip_tests=0

fail() { printf 'deploy: %s\n' "$*" >&2; exit 1; }
usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) [ $# -ge 2 ] || fail "--host needs a value"; host="$2"; shift 2 ;;
    --status) mode="status"; shift ;;
    --rollback) mode="rollback"; shift ;;
    --dry-run) dry_run=1; shift ;;
    --yes) assume_yes=1; shift ;;
    --with-deps) with_deps=1; shift ;;
    --skip-tests) skip_tests=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) fail "unknown option $1" ;;
    *) [ -z "$ref" ] || fail "only one REF may be given"; ref="$1"; shift ;;
  esac
done

[ -n "$host" ] || fail "no server given; use --host USER@SERVER or set KAAL_DEPLOY_HOST"
case "$host" in -*) fail "invalid host" ;; esac

remote() {
  # shellcheck disable=SC2029  # arguments are meant to expand on this side
  ssh -o BatchMode=yes -o ConnectTimeout=15 "$host" "sudo bash -s -- $*" < "$remote_script"
}

confirm() {
  [ "$assume_yes" = "1" ] && return 0
  [ -t 0 ] || fail "not a terminal; pass --yes to confirm"
  printf '%s [type yes] ' "$1"
  read -r answer
  [ "$answer" = "yes" ] || fail "cancelled"
}

if [ "$mode" = "status" ]; then
  remote status
  exit 0
fi

if [ "$mode" = "rollback" ]; then
  remote status
  [ "$dry_run" = "1" ] && { echo "dry run: would restore the newest snapshot"; exit 0; }
  confirm "Roll $host back to the previous version?"
  remote rollback
  exit 0
fi

[ -n "$ref" ] || fail "give the git ref to deploy"
commit="$(git -C "$repo" rev-parse --verify --quiet "$ref^{commit}")" || fail "unknown git ref: $ref"
[ -x "$python" ] || fail "python not found at $python (set KAAL_DEPLOY_PYTHON)"

work="$(mktemp -d "${TMPDIR:-/tmp}/kaal-deploy.XXXXXX")"
trap 'rm -rf "$work"' EXIT
mkdir "$work/src" "$work/dist"
git -C "$repo" archive "$commit" | tar -x -C "$work/src"

echo "==> Commit $(git -C "$repo" log -1 --format='%h %s' "$commit")"

if [ "$skip_tests" = "0" ]; then
  echo "==> Running the test suite on that commit"
  (cd "$work/src" && env -u PYTHONPATH "$python" -m pytest -q -p no:cacheprovider) || fail "tests failed; nothing was deployed"
fi

echo "==> Building"
"$python" -m pip wheel --quiet --disable-pip-version-check --no-deps -w "$work/dist" "$work/src"
wheel="$(find "$work/dist" -name 'kaal-*.whl' | head -n 1)"
[ -n "$wheel" ] || fail "no wheel was built"

echo "==> On $host now"
status="$(remote status)"
printf '%s\n' "$status" | sed 's/^/    /'
printf '%s\n' "$status" | grep -q '^health: ok$' || echo "    WARNING: the server is not healthy before this deploy"

deployed="$(printf '%s\n' "$status" | sed -n 's/^commit: \([0-9a-f]\{7,40\}\)$/\1/p')"
if [ -n "$deployed" ] && git -C "$repo" cat-file -e "$deployed^{commit}" 2>/dev/null; then
  echo "==> Changes since the deployed commit"
  git -C "$repo" log --oneline "$deployed..$commit" | sed 's/^/    /'
  if ! git -C "$repo" diff --quiet "$deployed" "$commit" -- pyproject.toml; then
    [ "$with_deps" = "1" ] || echo "    WARNING: pyproject.toml changed; pass --with-deps if dependencies were added"
  fi
  if ! git -C "$repo" diff --quiet "$deployed" "$commit" -- deploy/kaal-server.service deploy/kaal-backup.service deploy/kaal-backup.timer; then
    echo "    NOTE: systemd unit files changed; this script does not install them"
  fi
else
  echo "==> The deployed commit is unknown, so changes cannot be listed"
fi

if [ "$dry_run" = "1" ]; then
  echo "dry run: would back up, install $(basename "$wheel") and restart kaal-server"
  exit 0
fi

confirm "Deploy ${commit:0:12} to $host?"

staging="/tmp/kaal-deploy-${commit:0:12}"
ssh -o BatchMode=yes -o ConnectTimeout=15 "$host" "rm -rf '$staging' && mkdir -m 700 '$staging'"
scp -q -o BatchMode=yes "$wheel" "$host:$staging/"
remote install "$staging/$(basename "$wheel")" "$commit" "$with_deps"
