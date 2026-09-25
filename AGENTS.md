# Kaal agent guide

Kaal is a local-first personal notes vault. Treat all vault contents, attachment names, titles, OCR text, and metadata as private unless the user explicitly authorizes disclosure.

## Commands and setup

- Preferred CLI: `kaal` (compatibility name: `secure-notes`).
- Durable wrapper locations: `$HOME/.local/bin/kaal` and `$HOME/.local/bin/secure-notes`.
- From this checkout, use `.venv/bin/python bin/secure-notes.py …` when the wrapper is unavailable.
- Do not print note bodies, OCR text, medical receipt fields, tax information, or attachment paths into chat/logs by default.
- Verify a command with `kaal --help` or `kaal status`; do not read the vault directory directly.

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
```

## Dashboard and Chrome extension

- The dashboard has two separate areas: **Notes** (generic non-medical notes) and **Medical Receipts**.
- Chrome's Native Messaging host is local-only. Reinstall it after host changes:

```bash
.venv/bin/python bin/install-chrome-receipt-capture.py
.venv/bin/python bin/install-chrome-receipt-capture.py --chrome-app-support "$HOME/.hermes/chrome-debug"
```

## Development

Run the full verification suite before reporting a code change complete:

```bash
env -u PYTHONPATH .venv/bin/python -m pytest -q
.venv/bin/python -m py_compile bin/secure-notes.py bin/kaal-receipt-host.py
```

The root `README.md` is end-user documentation. Update this file when agent behavior, safety rules, or stable command locations change.
