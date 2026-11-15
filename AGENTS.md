# Kaal agent guide

Kaal is a personal notes vault. Every note, attachment, extracted text and the index are encrypted at rest; `kaal encrypt-all` (server only) converts readable content left by older versions. Never read or edit `index.json` directly: use `kaal` commands. Treat all vault contents, attachment names, titles, OCR text, and metadata as private unless the user explicitly authorizes disclosure.

## Commands and setup

- Preferred CLI: `kaal` (compatibility name: `secure-notes`).
- Durable wrapper locations: `$HOME/.local/bin/kaal` and `$HOME/.local/bin/secure-notes`.
- From this checkout, use `.venv/bin/python bin/secure-notes.py …` when the wrapper is unavailable.
- Do not print note bodies, OCR text, medical receipt fields, tax information, or attachment paths into chat/logs by default.
- Verify a command with `kaal --help` or `kaal status`; do not read the vault directory directly.
- `kaal` may be a client of `kaal-server`: when `~/.config/kaal/config.toml` (or `KAAL_CONFIG`/`KAAL_URL`) names a server, every command runs there with that machine's token and prints the same output. `kaal status` shows `"server"` in that case. `KAAL_LOCAL=1` forces local mode; only use it when told to.
- A token can lack a scope (`read`, `reveal`, `write`, `delete`, `admin`); an error saying so is expected, so do not look for another way around it.
- `kaal verify` checks the vault and prints ids only; `kaal export`/`kaal import` move a whole vault and are admin operations: run them only when explicitly asked.

## Safety rules

1. Search/list metadata first. `kaal list` and `kaal search` are metadata-only.
2. Retrieve plaintext only for an explicit user request. `kaal show` is redacted by default; `--full` is sensitive.
3. Use `--sensitivity sensitive` when the user identifies private data. Automatic classification is best effort, not a guarantee.
4. Preserve external source files. Kaal manages its own attachment copy and must not remove browser downloads or user-selected originals.
5. Use Trash before permanent deletion. Permanent removal requires the exact record ID and explicit confirmation.
6. Keep medical receipts in `kaal medical …`; do not use generic Library operations on them.
7. Do not commit vault data, `.hermes/` attachments, credentials, or Keychain material. They are local-only.

## Common agent workflows

```bash
# Metadata only
kaal list
kaal search "tax"

# Save a sensitive note
kaal add --title "Tax reference" --tags taxes --sensitivity sensitive --body-file /absolute/path/note.md

# Preserve and extract an attachment
kaal attach "Tax reference" /absolute/path/return.pdf --extract --sensitivity sensitive

# Export a managed attachment only when explicitly requested
kaal export-attachment NOTE_ID ATTACHMENT_ID --output /absolute/path/output.pdf

# Medical receipts use the separate workflow
kaal medical list --inbox
kaal medical capture /absolute/path/receipt.pdf

# Receipt totals (medical data: needs the reveal scope, and do not echo the rows)
kaal medical report --year 2026 --reimbursed no
```

`kaal medical show NOTE_ID` prints one receipt with the receipt lines its values came from (medical data: reveal scope, and do not echo it). `kaal medical reextract` re-reads stored receipts and keeps typed and confirmed values; on a server it only queues them. `kaal medical providers` lists provider names Kaal has learnt to show the user's way (medical data: reveal scope). `kaal medical evaluate` (server only, read-only) re-extracts receipts and prints per-field accuracy with ids and outcomes, never values; it is safe to show its output.

Receipt dates are stored as `YYYY-MM-DD` and amounts as plain decimals; `kaal medical update` refuses values it cannot read. `kaal medical reimburse` and `kaal medical people --add/--remove` change the user's records: run them only when asked.

## Dashboard and Chrome extension

- The dashboard has two separate areas: **Notes** (generic non-medical notes) and **Medical Receipts**.
- Chrome's Native Messaging host is local-only. Reinstall it after host changes:

```bash
.venv/bin/python bin/install-chrome-receipt-capture.py
.venv/bin/python bin/install-chrome-receipt-capture.py --chrome-app-support "$HOME/.hermes/chrome-debug"
```

## Development

Code lives in the `kaal` package: `core.py` (vault, encryption, command handlers), `receipts.py` (receipt value normalisation, family-member matching, and checking model values against receipt text), `receipt_ai.py` (the optional model call for receipt fields; off unless `KAAL_RECEIPT_AI` is set), `keys.py` (Keychain and key-file providers), `cli.py` (argument parsing, local or remote), `client.py` (remote mode), `server.py` (kaal-server) and `tokens.py`. `bin/secure-notes.py` is a launcher. Install dev dependencies with `pip install -e '.[dev]'` (add `extract` for Docling).

The web app is static files in `kaal/web/` (plain ES modules, no build step, no dependencies). It must stay free of inline script and style, must never write vault text as HTML, and may only call the API through `kaal/web/api.js`; `tests/test_web_app.py` enforces all three. Notes are rendered by `kaal/web/markdown.js`, which returns a tree of plain objects and never HTML text (`tests/test_markdown.py` runs it under Node).

Tests use a throwaway key file and never the Keychain, and never a real `~/.config/kaal/config.toml` (see `tests/conftest.py`); keep it that way. Run the full verification suite before reporting a code change complete:

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q
.venv/bin/python -m py_compile bin/secure-notes.py bin/kaal-receipt-host.py kaal/*.py
```

After changing an endpoint, regenerate the API spec (a test fails until you do):

```bash
.venv/bin/python -m kaal.server openapi --output docs/openapi.json
```

## Deployment

`deploy/deploy.sh` deploys one commit to a server and can roll it back. It tests and builds that commit (never the working tree), then on the server backs the vault up, snapshots the installed version, installs, restarts `kaal-server` and checks health, restoring the snapshot if the check fails.

```bash
deploy/deploy.sh --host USER@SERVER --status
deploy/deploy.sh --host USER@SERVER --dry-run REF
deploy/deploy.sh --host USER@SERVER REF          # add --with-deps when dependencies changed
deploy/deploy.sh --host USER@SERVER --rollback
```

Deploying or rolling back changes a live vault's server: do it only when the user has approved that specific release, and pass `--yes` only after they have. `--status` and `--dry-run` change nothing on the server. The script does not install systemd unit files.

The root `README.md` is end-user documentation. Update this file when agent behavior, safety rules, or stable command locations change.
