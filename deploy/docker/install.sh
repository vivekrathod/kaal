#!/bin/sh
# Set Kaal up on this machine with Docker, in one command.
#
#   deploy/docker/install.sh                                   asks where Kaal will be reached
#   deploy/docker/install.sh --domain kaal.example.org         a domain of your own; HTTPS is set up for you
#   deploy/docker/install.sh --behind https://kaal.example.ts.net   behind Tailscale or a proxy you run
#   deploy/docker/install.sh --local                           only on this machine, to try it out
#
# Add --image REF to use a ready-made image instead of building one here.
# Running it again changes nothing that is stored; it only applies new answers.
set -eu
cd "$(dirname "$0")"

say() { printf '\n==> %s\n' "$1"; }
stop() { printf '\nStopped: %s\n' "$1" >&2; exit 1; }

mode=""
value=""
image=""
while [ $# -gt 0 ]; do
    case "$1" in
    --domain) mode=domain; value="${2:-}"; shift 2 ;;
    --behind) mode=behind; value="${2:-}"; shift 2 ;;
    --local) mode=local; shift ;;
    --image) image="${2:-}"; shift 2 ;;
    -h | --help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) stop "unknown option $1 (see --help)" ;;
    esac
done

command -v docker > /dev/null 2>&1 || stop "Docker is not installed. Install it from https://docs.docker.com/engine/install/ and run this again."
docker info > /dev/null 2>&1 || stop "Docker is installed but not running, or this user may not use it."
version=$(docker compose version --short 2> /dev/null || true)
[ -n "$version" ] || stop "Docker Compose is missing. Install the Compose plugin (https://docs.docker.com/compose/install/) and run this again."
major=${version%%.*}
rest=${version#*.}
minor=${rest%%.*}
if [ "$major" -lt 2 ] || { [ "$major" -eq 2 ] && [ "$minor" -lt 24 ]; }; then
    stop "Docker Compose $version is too old; Kaal needs 2.24 or newer."
fi

first_time=1
[ -f .env ] && first_time=0

if [ -z "$mode" ] && [ "$first_time" -eq 0 ]; then
    say "Already set up here; keeping the address it has. (Pass --domain, --behind or --local to change it.)"
elif [ -z "$mode" ]; then
    cat << 'ASK'

Where will people open Kaal?

  1) At a domain name of my own that points at this machine.
     HTTPS is set up for you. Ports 80 and 443 must be reachable from the internet.
  2) Through Tailscale, or another proxy I run myself.
  3) Only on this machine, to try it out.

ASK
    printf 'Choose 1, 2 or 3: '
    read -r choice
    case "$choice" in
    1) mode=domain; printf 'The domain name, such as kaal.example.org: '; read -r value ;;
    2) mode=behind; printf 'The full address people will open, such as https://kaal.example.ts.net: '; read -r value ;;
    3) mode=local ;;
    *) stop "that was not 1, 2 or 3" ;;
    esac
fi

if [ -n "$mode" ]; then
    profiles=""
    domain=""
    listen="127.0.0.1:8765"
    secure=1
    case "$mode" in
    domain)
        domain=$(printf '%s' "$value" | sed -e 's#^https\{0,1\}://##' -e 's#/.*$##' | tr 'A-Z' 'a-z')
        printf '%s' "$domain" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$' || stop "\"$value\" does not look like a domain name."
        address="https://$domain"
        profiles=https
        ;;
    behind)
        address=$(printf '%s' "$value" | sed -e 's#/*$##')
        printf '%s' "$address" | grep -Eq '^https://[A-Za-z0-9.-]+(:[0-9]+)?$' || stop "\"$value\" should look like https://kaal.example.ts.net."
        ;;
    local)
        address="http://localhost:8765"
        secure=0
        ;;
    esac
    old_image=$(sed -n 's/^KAAL_IMAGE=//p' .env 2> /dev/null || true)
    [ -n "$image" ] || image="$old_image"
    umask 077
    {
        echo "# Written by install.sh. Run it again to change these."
        echo "KAAL_PUBLIC_URL=$address"
        echo "KAAL_DOMAIN=$domain"
        echo "KAAL_LISTEN=$listen"
        echo "KAAL_COOKIE_SECURE=$secure"
        echo "COMPOSE_PROFILES=$profiles"
        [ -z "$image" ] || echo "KAAL_IMAGE=$image"
    } > .env
elif [ -n "$image" ]; then
    grep -v '^KAAL_IMAGE=' .env > .env.new
    echo "KAAL_IMAGE=$image" >> .env.new
    mv .env.new .env
fi

address=$(sed -n 's/^KAAL_PUBLIC_URL=//p' .env)
image=$(sed -n 's/^KAAL_IMAGE=//p' .env)

if [ -n "$image" ]; then
    say "Fetching $image"
    docker compose pull --quiet kaal
else
    say "Building Kaal (a minute or two the first time)"
    out=$(docker compose build --quiet kaal 2>&1) || { printf '%s\n' "$out" >&2; stop "the build failed"; }
fi

say "Starting"
# The HTTPS proxy belongs to the "domain of my own" choice only.
[ "$(sed -n 's/^COMPOSE_PROFILES=//p' .env)" = https ] || docker compose --profile https rm --stop --force caddy > /dev/null 2>&1 || true
out=$(docker compose up -d --quiet-pull --remove-orphans 2>&1) || { printf '%s\n' "$out" >&2; stop "Docker could not start Kaal. If it says a port is in use, something else on this machine already listens there (a domain of your own needs ports 80 and 443 free)."; }

tries=0
until [ "$(docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q kaal)" 2> /dev/null)" = healthy ]; do
    tries=$((tries + 1))
    [ "$tries" -lt 60 ] || stop "Kaal did not come up. See what it says with: deploy/docker/kaalctl logs"
    sleep 2
done
# Caddy reads its Caddyfile only when it starts; on a second run it is already running, perhaps with an older one.
[ "$(sed -n 's/^COMPOSE_PROFILES=//p' .env)" != https ] || [ "$first_time" -eq 1 ] || docker compose restart caddy > /dev/null 2>&1 || true

say "Kaal is running at $address"
# A link only for an administrator who has never signed in. A second run (another checkout over
# the same data, say) must not hand whoever runs it a way to replace an existing password.
link=""
[ "$first_time" -eq 1 ] && link=$(docker compose exec -T kaal kaal-server account setup-link owner --new-only 2> /dev/null | tail -1) || true
if [ "$first_time" -eq 1 ] && [ -z "$link" ]; then
    printf '\nThis Kaal already has its administrator, so no new sign-in link was made. Sign in as usual.\n'
    printf 'If the password is lost: deploy/docker/kaalctl setup-link owner\n'
elif [ "$first_time" -eq 1 ]; then
    cat << DONE

Open this link once to choose your password (it works for 24 hours):

  $link

You are the administrator. Add other people under Settings > People.

Two things to do now:
  1. Keep a copy of the key somewhere off this machine. Without it nothing in
     Kaal can be read again:   deploy/docker/kaalctl key-backup ~/kaal-master.key
  2. A copy of everything is made each day and the newest 14 are kept, but on
     this same machine. To keep copies elsewhere too, see docs/install.md.
DONE
fi
case "$(sed -n 's/^COMPOSE_PROFILES=//p' .env)$(sed -n 's/^KAAL_COOKIE_SECURE=//p' .env)" in
https*) printf '\nThe first visit can take a few seconds while the certificate for the domain is fetched.\n' ;;
1) printf '\nKaal listens on 127.0.0.1:8765. Point your proxy at it; with Tailscale:\n\n  tailscale serve --bg 8765\n' ;;
esac
