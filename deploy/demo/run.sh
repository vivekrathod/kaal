#!/bin/sh
# Kaal's demo in one command: a Kaal holding only made-up data, for Apple's
# reviewers and anyone trying Kaal. It goes back to that data every night.
#
#   deploy/demo/run.sh --domain demo.example.org     a domain of its own; HTTPS is set up for you
#   deploy/demo/run.sh --behind https://demo.example.ts.net   behind Tailscale or a proxy you run
#   deploy/demo/run.sh --local                       only on this machine, to try it out
#
#   deploy/demo/run.sh reset      put the made-up data back now
#   deploy/demo/run.sh status     is it running, and where
#   deploy/demo/run.sh logs       what it has said (follow with -f)
#   deploy/demo/run.sh stop       stop it; start brings it back with the made-up data
#   deploy/demo/run.sh remove     remove its containers, certificate and image
#
# When starting: --port N (where it listens on this machine, default 8770),
# --reset-at HH:MM (UTC, default 03:00), --image REF (a ready-made image).
# The reviewer's password is KAAL_DEMO_PASSWORD when that is set, otherwise
# one is made and printed once; it stays the same across resets and restarts.
# See deploy/demo/README.md.
set -eu
cd "$(dirname "$0")"

say() { printf '\n==> %s\n' "$1"; }
stop() { printf '\nStopped: %s\n' "$1" >&2; exit 1; }

# KAAL_DEMO_PROJECT names a second demo on one machine. Each has its own
# containers and settings file, and none of them is a regular installation.
project=${KAAL_DEMO_PROJECT:-kaal-demo}
printf '%s' "$project" | grep -Eq '^[a-z0-9][a-z0-9_-]{0,62}$' || stop "KAAL_DEMO_PROJECT may hold only lower-case letters, digits, - and _."
[ "$project" != kaal ] || stop "\"kaal\" is the name of a regular installation (deploy/docker); the demo must never use it."
settings="$project.env"

# Compose prefers a variable of this shell to the same one in the settings
# file, so these are left out: the settings file alone says how the demo runs.
dc() {
    (
        unset COMPOSE_PROFILES KAAL_IMAGE KAAL_PUBLIC_URL KAAL_DOMAIN KAAL_LISTEN KAAL_COOKIE_SECURE KAAL_HSTS KAAL_HTTP_PORT KAAL_HTTPS_PORT \
            KAAL_DEMO_PASSWORD KAAL_DEMO_ADMIN_PASSWORD KAAL_DEMO_RESET_AT KAAL_DEMO_STORAGE KAAL_DEMO_FREE_SPACE_MB KAAL_DEMO_MAX_UPLOAD_BYTES KAAL_DEMO_MEMORY
        docker compose -p "$project" --env-file "$settings" -f compose.yaml "$@"
    )
}
setting() { sed -n "s/^$1=//p" "$settings" 2> /dev/null | tail -1; }
# A password that is easy to type on a phone: 16 letters and digits (about 79 bits), in fours.
new_password() { LC_ALL=C tr -dc 'abcdefghjkmnpqrstuvwxyz23456789' < /dev/urandom | head -c 16 | sed -e 's/\(....\)/\1-/g' -e 's/-$//'; }

ready() {
    tries=0
    until [ "$(docker inspect --format '{{.State.Health.Status}}' "$(dc ps -q kaal 2> /dev/null)" 2> /dev/null)" = healthy ]; do
        tries=$((tries + 1))
        [ "$tries" -lt 120 ] || return 1
        sleep 2
    done
}
# Changes each time the made-up data has been put in.
marker() { dc exec -T kaal cat /tmp/kaal-demo-ready 2> /dev/null || true; }

command=up
case "${1:-}" in
reset | status | logs | stop | start | remove) command=$1; shift ;;
-h | --help | help) sed -n '2,19p' run.sh | sed 's/^# \{0,1\}//'; exit 0 ;;   # this folder is the script's own by now
esac
[ "$command" = up ] || [ -f "$settings" ] || stop "the demo is not set up here yet. Start it with: deploy/demo/run.sh --local (or --domain, --behind)"

case "$command" in
reset)
    before=$(marker)
    [ -n "$before" ] || stop "the demo is not running, or is still putting the made-up data in. Start it with: deploy/demo/run.sh start"
    dc kill -s HUP kaal > /dev/null 2>&1 || stop "the demo could not be told to reset. See: deploy/demo/run.sh logs"
    tries=0
    while now=$(marker); [ -z "$now" ] || [ "$now" = "$before" ]; do
        tries=$((tries + 1))
        [ "$tries" -lt 120 ] || stop "the made-up data did not come back. See: deploy/demo/run.sh logs"
        sleep 2
    done
    echo "The made-up data is back; anything added since is gone."
    exit 0
    ;;
status)
    dc ps --format 'table {{.Service}}\t{{.Status}}'
    printf 'Address: %s\nPut back every night at %s UTC.\n' "$(setting KAAL_PUBLIC_URL)" "$(setting KAAL_DEMO_RESET_AT)"
    dc exec -T kaal kaal-server --version 2> /dev/null || true
    exit 0
    ;;
logs)
    dc logs --tail 200 "$@" kaal
    exit 0
    ;;
stop)
    dc stop > /dev/null 2>&1 || stop "Docker could not stop the demo."
    echo "Stopped. It held only made-up data, which is gone; start brings it back."
    exit 0
    ;;
start)
    dc up -d > /dev/null 2>&1 || stop "Docker could not start the demo. See: deploy/demo/run.sh logs"
    ready || stop "the demo did not come up. See: deploy/demo/run.sh logs"
    echo "Running at $(setting KAAL_PUBLIC_URL)"
    exit 0
    ;;
remove)
    out=$(dc --profile https down --volumes 2>&1) || { printf '%s\n' "$out" >&2; stop "Docker could not remove the demo; it may still be there."; }
    image=$(setting KAAL_IMAGE)
    if [ "$image" = "$project:local" ]; then
        docker image rm "$image" > /dev/null 2>&1 || true
        echo "Removed the demo's containers, certificate and image."
    else
        echo "Removed the demo's containers and certificate. The image $image was not built here and is left."
    fi
    echo "Its settings, with the passwords, are still in deploy/demo/$settings; delete that file too to forget them."
    exit 0
    ;;
esac

mode=""
value=""
image=""
port=""
reset_at=""
while [ $# -gt 0 ]; do
    case "$1" in
    --domain | --behind | --port | --reset-at | --image)
        [ $# -ge 2 ] || stop "$1 needs a value (see --help)"
        case "$1" in
        --domain) mode=domain; value=$2 ;;
        --behind) mode=behind; value=$2 ;;
        --port) port=$2 ;;
        --reset-at) reset_at=$2 ;;
        --image) image=$2 ;;
        esac
        shift 2
        ;;
    --local) mode=local; shift ;;
    *) stop "unknown option $1 (see --help)" ;;
    esac
done

command -v docker > /dev/null 2>&1 || stop "Docker is not installed. Install it from https://docs.docker.com/engine/install/ and run this again."
docker info > /dev/null 2>&1 || stop "Docker is installed but not running, or this user may not use it."
version=$(docker compose version --short 2> /dev/null || true)
[ -n "$version" ] || stop "Docker Compose is missing. Install the Compose plugin (https://docs.docker.com/compose/install/) and run this again."
version=${version#v}
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
case "$major.$minor" in
*[!0-9.]* | . | .* | *.) stop "could not tell which Docker Compose this is (it says \"$version\"); the demo needs 2.24 or newer." ;;
esac
if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 24 ]; }; then
    stop "Docker Compose $version is too old; the demo needs 2.24 or newer."
fi

first_time=1
[ -f "$settings" ] && first_time=0
# A name Docker already knows, with no settings of ours beside it, is somebody else's project:
# starting the demo under it would put the demo's containers among theirs.
if [ "$first_time" -eq 1 ] && docker compose ls --all --quiet 2> /dev/null | grep -qx "$project"; then
    stop "Docker already has a project named \"$project\" that this script did not make. Choose another name with KAAL_DEMO_PROJECT."
fi
[ "$first_time" -eq 0 ] || [ -n "$mode" ] || stop "say where people will open the demo: --domain NAME, --behind ADDRESS or --local (see --help)"

# Start from what was chosen before, then apply what was asked now.
[ -n "$mode" ] || mode=$(setting KAAL_DEMO_MODE)
[ -n "$value" ] || value=$(setting KAAL_DEMO_ADDRESS)
[ -n "$port" ] || port=$(setting KAAL_LISTEN | sed 's/.*://')
[ -n "$port" ] || port=8770
[ -n "$reset_at" ] || reset_at=$(setting KAAL_DEMO_RESET_AT)
[ -n "$reset_at" ] || reset_at=03:00
[ -n "$image" ] || image=$(setting KAAL_IMAGE)
[ -n "$image" ] || image="$project:local"

printf '%s' "$port" | grep -Eq '^[0-9]{4,5}$' && [ "$port" -ge 1024 ] && [ "$port" -le 65535 ] || stop "--port must be a number from 1024 to 65535."
printf '%s' "$reset_at" | grep -Eq '^([01][0-9]|2[0-3]):[0-5][0-9]$' || stop "--reset-at must be a time of day such as 03:00 (UTC)."

profiles=""
domain=""
secure=1
hsts=0
case "$mode" in
domain)
    domain=$(printf '%s' "$value" | tr 'A-Z' 'a-z' | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##')
    printf '%s' "$domain" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || stop "\"$value\" does not look like a domain name."
    value=$domain
    address="https://$domain"
    profiles=https
    # A domain of its own serves nothing over plain http, so browsers are told to stay on https,
    # as deploy/docker/install.sh has it.
    hsts=1
    ;;
behind)
    address=$(printf '%s' "$value" | sed -e 's#/*$##')
    printf '%s' "$address" | grep -Eq '^https://[A-Za-z0-9.-]+(:[0-9]+)?$' || stop "\"$value\" should look like https://demo.example.ts.net."
    value=$address
    ;;
local)
    address="http://localhost:$port"
    value=""
    secure=0
    ;;
*) stop "the settings in deploy/demo/$settings name no address; run this again with --domain, --behind or --local." ;;
esac

# The reviewer's password: as given, else as before, else a new one. Kept to
# characters that need no quoting anywhere, since it is typed on a phone.
reviewer=${KAAL_DEMO_PASSWORD:-}
told="the one in KAAL_DEMO_PASSWORD"
if [ -n "$reviewer" ]; then
    printf '%s' "$reviewer" | grep -Eq '^[A-Za-z0-9._-]{10,128}$' || stop "KAAL_DEMO_PASSWORD must be 10 to 128 letters, digits, dots, dashes or underscores."
else
    told="unchanged"
    reviewer=$(setting KAAL_DEMO_PASSWORD)
    [ -n "$reviewer" ] || reviewer=$(new_password)
    # Shown once, which means once the demo has come up: a first run that failed part-way
    # (a port in use, say) has not shown them yet.
    [ "$(setting KAAL_DEMO_SHOWN)" = 1 ] || told="$reviewer   (shown this once)"
fi
admin=$(setting KAAL_DEMO_ADMIN_PASSWORD)
made_admin=0
[ -n "$admin" ] || admin=$(new_password)
[ "$(setting KAAL_DEMO_SHOWN)" = 1 ] || made_admin=1

umask 077
# Lines this script does not write, such as KAAL_DEMO_STORAGE (see README.md), are kept.
ours='^(#|$|(KAAL_DEMO_MODE|KAAL_DEMO_ADDRESS|KAAL_PUBLIC_URL|KAAL_DOMAIN|KAAL_LISTEN|KAAL_COOKIE_SECURE|KAAL_HSTS|COMPOSE_PROFILES|KAAL_IMAGE|KAAL_DEMO_RESET_AT|KAAL_DEMO_PASSWORD|KAAL_DEMO_ADMIN_PASSWORD|KAAL_DEMO_SHOWN|KAAL_DEMO_CODE)=)'
{
    echo "# Written by deploy/demo/run.sh; run it again to change these. Keep this file private: it holds the passwords."
    echo "KAAL_DEMO_MODE=$mode"
    echo "KAAL_DEMO_ADDRESS=$value"
    echo "KAAL_PUBLIC_URL=$address"
    echo "KAAL_DOMAIN=$domain"
    echo "KAAL_LISTEN=127.0.0.1:$port"
    echo "KAAL_COOKIE_SECURE=$secure"
    echo "KAAL_HSTS=$hsts"
    echo "COMPOSE_PROFILES=$profiles"
    echo "KAAL_IMAGE=$image"
    echo "KAAL_DEMO_RESET_AT=$reset_at"
    echo "KAAL_DEMO_PASSWORD=$reviewer"
    echo "KAAL_DEMO_ADMIN_PASSWORD=$admin"
    echo "KAAL_DEMO_SHOWN=$([ "$(setting KAAL_DEMO_SHOWN)" = 1 ] && echo 1 || echo 0)"
    # Changes when the demo's own script does, so that running this again after an update
    # makes the container again with the new one.
    echo "KAAL_DEMO_CODE=$(cksum < demo.py | cut -d' ' -f1)"
    [ "$first_time" -eq 1 ] || grep -Ev "$ours" "$settings" || true
} > "$settings.new"
chmod 600 "$settings.new"
mv "$settings.new" "$settings"

# Its own image name, so that building the demo never replaces the image a
# regular installation on this machine runs (kaal:local).
if [ "$image" = "$project:local" ]; then
    say "Building Kaal (a minute or two the first time)"
    out=$(dc build --quiet kaal 2>&1) || { printf '%s\n' "$out" >&2; stop "the build failed"; }
else
    say "Fetching $image"
    dc pull --quiet kaal
fi

say "Starting the demo"
[ "$profiles" = https ] || dc --profile https rm --stop --force caddy > /dev/null 2>&1 || true
out=$(dc up -d --quiet-pull 2>&1) || { printf '%s\n' "$out" >&2; stop "Docker could not start the demo. If it says a port is in use, something else on this machine listens there: choose another --port, and for --domain ports 80 and 443 must be free."; }
ready || stop "the demo did not come up. See what it says with: deploy/demo/run.sh logs"
# The passwords have been (or are about to be) shown: note it, so they are not shown again.
{ grep -v '^KAAL_DEMO_SHOWN=' "$settings"; echo "KAAL_DEMO_SHOWN=1"; } > "$settings.new"
chmod 600 "$settings.new"
mv "$settings.new" "$settings"

say "Kaal's demo is running at $address"
cat << DONE

Everything in it is made up, and it goes back to that every night at $reset_at UTC
(or now, with: deploy/demo/run.sh reset).

  Sign-in name for App Review:   reviewer
  Its password:                  $told
DONE
if [ "$made_admin" -eq 1 ]; then
    printf '  Administrator:                 owner, password %s   (shown this once; keep it to yourself)\n' "$admin"
fi
cat << DONE

Both passwords are kept in deploy/demo/$settings and stay the same across
resets. What to tell App Review is in deploy/demo/README.md.
DONE
if [ "$mode" = behind ]; then
    printf '\nThe demo listens on 127.0.0.1:%s. Point your proxy at it; with Tailscale:\n\n  tailscale serve --bg %s\n' "$port" "$port"
    printf '\nA proxy of your own must pass on who is asking (the X-Forwarded-For header), or Kaal\ncannot tell visitors apart. Tailscale and Caddy do; for nginx add:\n\n  proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
fi
