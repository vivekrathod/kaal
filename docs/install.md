# Installing Kaal

Kaal runs on a machine you control: a small server you rent, a computer at
home, or your own laptop to try it. This guide uses Docker, which is the
shortest way. (Without Docker, see "Running Kaal as a server" in the
[README](../README.md) and the systemd unit in [`deploy/`](../deploy).)

## What you need

- A machine running Linux or macOS with [Docker](https://docs.docker.com/engine/install/)
  and its Compose plugin, version 2.24 or newer.
- About 1 GB of disk for Kaal itself, plus room for what you keep in it.
- One of: a domain name pointing at the machine, [Tailscale](https://tailscale.com),
  or nothing at all if you only want to try it on that machine.

## Install

```bash
git clone REPOSITORY-ADDRESS kaal
cd kaal
deploy/docker/install.sh
```

Use the address of the repository you are reading this in. The script asks
where people will open Kaal:

1. **A domain name of your own** that points at this machine, such as
   `kaal.example.org`. HTTPS is set up for you: a certificate is fetched and
   renewed automatically. Ports 80 and 443 must be reachable from the internet
   and not used by anything else.
2. **Through Tailscale, or another proxy you run.** Kaal listens only on this
   machine (`127.0.0.1:8765`) and your proxy provides HTTPS. With Tailscale,
   `tailscale serve --bg 8765` publishes it to your own devices only.
3. **Only on this machine**, at `http://localhost:8765`, to try it out.

The same choices without questions:

```bash
deploy/docker/install.sh --domain kaal.example.org
deploy/docker/install.sh --behind https://kaal.example.ts.net
deploy/docker/install.sh --local
```

It builds Kaal, starts it, and prints a link. Open the link once to choose
your password; you are then the administrator and can add other people under
Settings → People. Running the script again is safe: it keeps what is stored
and only applies new answers.

## Keep a copy of the key

Everything in Kaal is encrypted with one key, which lives on the machine but
apart from the data. **If the key is lost, nothing in Kaal can be read again**,
by you or anyone else. Copy it somewhere off the machine straight away:

```bash
deploy/docker/kaalctl key-backup ~/kaal-master.key
```

Move that file to a password manager, an encrypted drive, or paper in a safe.
Anyone who has both the key and a copy of the data can read everything, so do
not keep them together.

If the machine ever loses the key but still has the data, Kaal refuses to
start instead of making a new key. Put the saved one back with
`deploy/docker/kaalctl key-restore ~/kaal-master.key`.

## Day to day

```bash
deploy/docker/kaalctl status                 # is it running, which version, where
deploy/docker/kaalctl logs                   # what the server has said
deploy/docker/kaalctl setup-link sam         # a single-use link for sam to choose a password
deploy/docker/kaalctl run kaal-server account list
deploy/docker/kaalctl stop                   # and start; nothing stored is touched
```

Any command from the [README](../README.md) runs inside the container with
`kaalctl run`, for example `kaalctl run kaal-server sign-in list`.

## Updating

```bash
git pull
deploy/docker/kaalctl update
```

This makes a backup, builds the new version and restarts. If the new version
does not come up, the one that was running is put back.

## Backups

A copy of everything is made each day and the newest 14 are kept. Unchanged
files are shared between copies, so they take little room. They sit on the
same machine as the data, which protects against a mistake or a bad update but
not against losing the machine. `deploy/docker/kaalctl backup-now` makes one
at any time.

The key is never included in a backup.

## Restoring a backup

```bash
deploy/docker/kaalctl backups                 # the copies kept here, newest last
deploy/docker/kaalctl restore 20260101T020000Z
```

Kaal is stopped, everything is put back as that copy has it (vaults, accounts
and tokens), and Kaal starts again. What was there before is not deleted: it is
moved into a `before-restore-…` folder in the data volume. The key must be the
one the copy was made with; if it is not, nothing is changed.

To keep copies somewhere other than this machine, copy one out of Docker and
take it elsewhere:

```bash
deploy/docker/kaalctl backup-copy 20260101T020000Z ~/kaal-backups
```

A copy is encrypted, except the list of accounts; it is useless without the key,
so keep the key apart from it.

## Leaving

`deploy/docker/kaalctl export-readable ~/kaal-copy.zip` writes everything in
the first vault, decrypted: notes as Markdown, the files, and receipts as a
spreadsheet. Each person can download their own vaults the same way in the web
app (Settings → Vaults and sharing → Download a copy), and delete their account
under Settings.

## Settings

Extra settings go in `deploy/docker/kaal.env`, one `NAME=value` per line, and
take effect after `deploy/docker/kaalctl start`. The ones most people want:

| Setting | What it does |
|---|---|
| `KAAL_MAX_UPLOAD_BYTES` | The largest file Kaal accepts (default 100 MiB). |
| `KAAL_VAULT_QUOTA_MB` | The most any vault but the first may hold (default 10240, which is 10 GiB; 0 for no limit). |
| `KAAL_VAULTS_PER_PERSON` | How many vaults one person may own (default 10). |
| `KAAL_SIGNUPS_PER_HOUR` | How many accounts strangers may create per hour when sign-up is open (default 10). |
| `KAAL_SIGN_IN_PER_MINUTE` | How often one address may call the sign-in pages' routes (default 30 a minute). |
| `KAAL_PASSWORD_CHECKS` | How many passwords are checked at the same moment (default 4). Each takes memory and a processor for a moment. |
| `KAAL_TRUSTED_PROXIES` | Whose word is taken for a visitor's address: `private` (the default here, for the proxy in a container), `none`, or a list of networks. |
| `KAAL_FILE_JOBS`, `KAAL_FILE_MEMORY_MB` | How many files are read at once (default 2) and the memory each reading may use (default 1024). |
| `KAAL_DOWNLOAD_MEMORY_MB` | The most memory held by files on their way to people at one time (default 1024). |
| `KAAL_FREE_SPACE_MB` | Nothing new is stored once the disk has less than this free (default 1024; 0 for no floor). |
| `KAAL_SEARCH_MEMORY_MB` | The most decrypted text each vault keeps in memory for searching (default 256). |

Before opening sign-up to strangers, set the quota and the hourly allowance to
what the disk can bear: ten new accounts an hour, each with ten vaults of
10 GiB, is more than most disks.

Signing in with Google is set up in the web app, under Settings → Sign-in
services; see the README.

## Reading receipts with a model (optional)

Kaal reads receipts with fixed rules by default. A model reads more layouts.
To use one that runs on this same machine with [Ollama](https://ollama.com),
so no receipt leaves it:

```bash
ollama pull qwen2.5vl:3b
```

and put this in `deploy/docker/kaal.env`:

```
KAAL_RECEIPT_AI=ollama
KAAL_RECEIPT_AI_URL=http://host.docker.internal:11434
```

Ollama must accept connections from the container: start it with
`OLLAMA_HOST=0.0.0.0` (on Linux, `systemctl edit ollama` and add
`Environment="OLLAMA_HOST=0.0.0.0"`), and keep port 11434 closed to the
internet. To use Claude instead, set `KAAL_RECEIPT_AI=anthropic` and put your
API key in the environment as `ANTHROPIC_API_KEY`. The README lists every
choice, and the daily limit that keeps a paid service from running up a bill.

## Using a ready-made image

Each release is also published as a container image on GitHub's registry,
next to the repository, for `linux/amd64` and `linux/arm64`. To use it instead
of building:

```bash
deploy/docker/install.sh --image ghcr.io/OWNER/kaal:latest
```

where `OWNER` is the owner of the repository you cloned.

## Removing Kaal

`deploy/docker/kaalctl stop` stops it and keeps everything. To remove it and
**everything stored in it**, from the `deploy/docker` folder:

```bash
docker compose --profile https down --volumes
```

There is no undo for that. Export what you want to keep first.

## If something goes wrong

- **"port is already allocated" or "address already in use"**: something else
  on the machine uses port 80, 443 or 8765. Stop it, or choose the "behind a
  proxy" option and point that program at `127.0.0.1:8765`.
- **The certificate is not issued**: the domain must already point at this
  machine, and ports 80 and 443 must be reachable from the internet.
  `docker compose logs caddy` (in `deploy/docker`) says what failed.
- **Kaal says its key is missing**: see "Keep a copy of the key" above.
