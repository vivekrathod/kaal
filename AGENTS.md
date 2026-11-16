# Kaal agent guide

Kaal is a personal notes vault. Every note, attachment, extracted text and the index are encrypted at rest; `kaal encrypt-all` (server only) converts readable content left by older versions. Never read or edit `index.json` directly: use `kaal` commands. Treat all vault contents, attachment names, titles, OCR text, and metadata as private unless the user explicitly authorizes disclosure.

## Commands and setup

- The command is `kaal`. From this checkout, use `bin/kaal …` (or `.venv/bin/python bin/kaal.py …`) when it is not on `PATH`.
- Do not print note bodies, OCR text, medical receipt fields, tax information, or attachment paths into chat/logs by default.
- Verify a command with `kaal --help` or `kaal status`; do not read the vault directory directly.
- `kaal` may be a client of `kaal-server`: when `~/.config/kaal/config.toml` (or `KAAL_CONFIG`/`KAAL_URL`) names a server, every command runs there with that machine's token and prints the same output. `kaal status` shows `"server"` in that case. `KAAL_LOCAL=1` forces local mode; only use it when told to.
- A token can lack a scope (`read`, `reveal`, `write`, `delete`, `admin`); an error saying so is expected, so do not look for another way around it.
- `kaal verify` checks the vault and prints ids only; `kaal export`/`kaal import` move a whole vault and are admin operations: run them only when explicitly asked.

## Safety rules

1. Search/list metadata first. `kaal list` and `kaal search` are metadata-only. `kaal library search "words"` also looks inside note text and the text read from files, and its results quote a line around each match, so treat its output as private content.
2. Retrieve plaintext only for an explicit user request. `kaal show` is redacted by default; `--full` is sensitive.
3. Everything is stored encrypted and labelled sensitive unless you pass `--sensitivity public`. The label is information only; nothing is inferred from the text.
4. Preserve external source files. Kaal manages its own attachment copy and must not remove browser downloads or user-selected originals.
5. Use Trash before permanent deletion. Permanent removal requires the exact record ID and explicit confirmation.
6. Keep medical receipts in `kaal medical …`; do not use generic Library operations on them.
7. Do not commit vault data, credentials, tokens or key material. They are local-only.

## Common agent workflows

```bash
# Metadata only
kaal list
kaal search "tax"

# Save a note
kaal add --title "Tax reference" --tags taxes --body-file /absolute/path/note.md

# Preserve and extract an attachment
kaal attach "Tax reference" /absolute/path/return.pdf --extract

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

## Chrome extension

`chrome-extension/` is an unpacked Manifest V3 extension with no build step. It talks to kaal-server directly with its own device token (set on its settings page and kept in `chrome.storage.local`); there is no native helper, nothing is written to Downloads, and it holds no access to any website: a page or file is read through Chrome's debugger on the tab being saved, and the server names the extension's origin for `/v1/` (`ExtensionCorsMiddleware`, `KAAL_EXTENSION_IDS`). `background.js` saves pages, `popup.js` is the one-button popup, `panel.js` is the side panel, `options.js` is the settings page, and `lib.js` holds the decisions that need no browser.

The side panel shows the web app's own review screen. `chrome-extension/web/` holds copies of the web files it needs; edit the originals in `kaal/web/` and run `bin/sync-extension-web.py` (a test fails while they differ). In those shared files, show a picture or link to one of Kaal's files with `picture()` and `fileLink()` from `ui.js`, never a bare `/v1/...` address: the extension has no sign-in cookie and must fetch the file with its token.

## Development

Code lives in the `kaal` package: `core.py` (vault, encryption, command handlers), `receipts.py` (receipt value normalisation, family-member matching, and checking model values against receipt text), `receipt_ai.py` (the optional model call for receipt fields; off unless `KAAL_RECEIPT_AI` is set), `keys.py` (the key file and wrapped per-vault keys), `accounts.py` (who has an account, who belongs to which vault, and people's sessions), `passwords.py` (Argon2id hashing), `cli.py` (argument parsing, local or remote), `client.py` (remote mode), `server.py` (kaal-server) and `tokens.py`. `bin/kaal.py` is a launcher. Install dev dependencies with `pip install -e '.[dev]'`.

The web app is static files in `kaal/web/` (plain ES modules, no build step, no dependencies). It must stay free of inline script and style, must never write vault text as HTML, and may only call the API through `kaal/web/api.js`; `tests/test_web_app.py` enforces all three. Notes are rendered by `kaal/web/markdown.js`, which returns a tree of plain objects and never HTML text (`tests/test_markdown.py` runs it under Node). A note refers to its own files as `attachment:<attachment id>` (files added in Kaal) or `:/<resource id>` (imported from Joplin); the renderer hands both to the caller and never builds an address from note text.

Tests use a throwaway key file, and never a real `~/.config/kaal/config.toml` (see `tests/conftest.py`); keep it that way. Run the full verification suite before reporting a code change complete:

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q
.venv/bin/python -m py_compile bin/kaal.py bin/sync-extension-web.py bin/make-icons.py kaal/*.py
```

After changing the Chrome extension or a web file it shares, also run its browser test, which is off by default (it needs Chrome for Testing or Chromium, and Node 22 or later):

```bash
KAAL_E2E=1 env -u PYTHONPATH .venv/bin/python -m pytest -q tests/test_extension_e2e.py
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
