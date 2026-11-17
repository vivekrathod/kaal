#!/bin/sh
# Starts Kaal in its container. On the very first start it makes the
# installation's key and an empty vault; it never makes a new key when there
# is already data, because data without its own key cannot be read again.
set -eu
umask 077
mkdir -p "$KAAL_TMP_DIR"

has_data() {
    [ -e "$KAAL_VAULT/index.json" ] || [ -e "$KAAL_HOME/accounts.db" ]
}

case "${1:-}" in
kaal-server)
    if [ "${2:-}" = serve ]; then
        if [ ! -s "$KAAL_KEY_FILE" ]; then
            if has_data; then
                echo "Kaal's data is here but its key is not ($KAAL_KEY_FILE)." >&2
                echo "Put the key back; without it nothing in the vault can be read. A new key is never made over existing data." >&2
                exit 1
            fi
            echo "First start: making this installation's key and an empty vault."
            kaal init > /dev/null
            echo "Keep a copy of the key somewhere off this machine (deploy/docker/kaalctl key-backup)."
        elif ! has_data; then
            kaal init > /dev/null
        fi
    fi
    ;;
backup-daily)
    # A copy of everything each day, the newest 14 kept (unchanged files are hard links).
    until has_data; do sleep 60; done
    while :; do
        if kaal backup --keep "${KAAL_BACKUP_KEEP:-14}" > /dev/null; then echo "Backup made."; else echo "Backup failed." >&2; fi
        sleep 86400
    done
    ;;
esac
exec "$@"
