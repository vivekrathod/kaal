# Kaal

Kaal is your local notes vault for personal data. Notes are Markdown by default and Kaal now uses best-effort local sensitivity detection to decide whether a note or attachment should be encrypted.

Vault: `$HOME/.secure-notes`
Primary CLI: `$HOME/.local/bin/kaal`
Compatibility CLI: `$HOME/.local/bin/secure-notes`
Key: macOS Keychain item `Hermes Secure Notes` / `vault-key-v1`, or an owner-only key file (see below)
Encryption: AES-256-GCM using Python `cryptography`
Server: optional `kaal-server`, so several machines share one vault (see [Running Kaal as a server](#running-kaal-as-a-server))

## Security model

- Notes are Markdown by default.
- Kaal classifies note/attachment content locally as `public` or `sensitive`.
- Sensitive notes and attachments are encrypted at rest.
- Public notes are stored as plaintext Markdown in the vault.
- Attachments always preserve the original bytes; optional extracted/OCR Markdown sidecars are created separately.
- Sensitive parent notes force attachments to be encrypted too.
- The index stores metadata: id, title, tags, timestamps, storage mode, classification reasons, attachment names/sizes/hashes.
- The encryption key is never stored in vault files: it lives in the macOS Keychain or, with `KAAL_KEY_PROVIDER=file`, in the file named by `KAAL_KEY_FILE`, which Kaal refuses to read unless only its owner can read it.
- File permissions are restrictive: vault directories `700`, stored files `600`.
- Default note display is redacted.
- Full plaintext should usually be copied to clipboard rather than printed in chat.

Important: the classifier is a best guess. False negatives are the dangerous case. Use `--sensitivity sensitive` for anything you know is private, and only use `--sensitivity public` when you are comfortable storing the note or attachment as plaintext.

## Running Kaal as a server

`kaal-server` puts one vault behind an authenticated HTTP API, so the CLI, the
Chrome extension and agents on other machines share it. The server is the only
process that opens the vault or reads the key. Every endpoint runs the same code
as the local CLI, and the API is described in [`docs/openapi.json`](docs/openapi.json).

```bash
pip install '.[server]'               # add ,extract for Docling and MarkItDown
export KAAL_HOME=/srv/kaal KAAL_VAULT=/srv/kaal/data
export KAAL_KEY_PROVIDER=file KAAL_KEY_FILE=/etc/kaal/master.key
kaal init                             # new vault and key, or `kaal import` an export
kaal-server token create mac-cli --scopes read,reveal,write,delete --output mac-cli.token
kaal-server serve                     # 127.0.0.1:8765; publish with `tailscale serve`
```

[`deploy/kaal-server.service`](deploy/kaal-server.service) is a hardened systemd
unit, and the [`Dockerfile`](Dockerfile) builds a container image.

**Updating a server.** `deploy/deploy.sh --host USER@SERVER REF` tests and
builds one commit, backs the vault up on the server, installs it, restarts
`kaal-server` and checks its health. If the check fails it restores the version
that was running. `--rollback` restores the previous version by hand,
`--dry-run` shows what would happen, and `--status` shows what is installed. It
expects the layout of the systemd unit above and passwordless `sudo` on the
server.

**Tokens** are shown once (or written to a new mode-600 file with `--output`)
and stored only as SHA-256 hashes. `kaal-server token list` and
`kaal-server token revoke NAME` manage them. Each token has scopes:

| Scope | Allows |
|---|---|
| `read` | Metadata: list, search, redacted views, status, verify |
| `reveal` | Plaintext: full note bodies, field values, attachment contents, extracted text, medical receipt fields |
| `write` | Create, edit, append, attach, capture, extract, trash, restore |
| `delete` | Permanent deletion and purges |
| `admin` | Whole-vault export |

**Clients.** Point the same `kaal` command at the server with
`~/.config/kaal/config.toml` (or `KAAL_CONFIG`), mode 600:

```toml
url = "https://storage.example.ts.net"
token_file = "~/.config/kaal/mac-cli.token"
```

Every command then runs remotely and prints exactly what it prints locally.
Files you attach are uploaded, and exports are written on your machine.
`KAAL_LOCAL=1` forces local mode. For the Chrome extension, reinstall its helper with
`bin/install-chrome-receipt-capture.py --kaal-config PATH`, pointing at a config
that holds the extension's own token.

**Checks and moving data.** `kaal verify [--deep]` compares the index with the
files on disk and reports ids only. `kaal export --output FILE.tar` writes the
vault exactly as stored (the key is not included). `kaal import FILE.tar`
restores into an empty vault and verifies it.

## Threat model

**What encryption protects.** Notes and attachments classified sensitive are
encrypted with AES-256-GCM, so someone holding a copy of the vault files without
the key (a stolen disk, a backup, a snapshot) cannot read them.

**What it does not protect.** Public notes, medical receipts (plaintext by
design) and all metadata are stored unencrypted: titles, tags, attachment names,
sizes and hashes in `index.json` and each `metadata.json`. Only file permissions
protect them: the `kaal` user, folders 700, files 600. Backups and snapshots of
the vault hold the same plaintext parts, so keep them as private as the vault.

**On the server.** Only `kaal-server`, running as its own user, reads the vault
and the key file. The key is never stored next to the data, and keep a recovery
copy offline. Anyone with root on the server host, or on the hypervisor under
it, can read everything: that is the trust boundary.

**On the network.** The server listens on loopback only. TLS and the choice of
which devices can connect come from the proxy in front of it (for example
`tailscale serve`, reachable from your tailnet only). Each client has its own
revocable token with only the scopes it needs. The CLI refuses plain http to
anything but loopback, and refuses token or config files that others can read.

**What the server never records.** Logs carry the method, route template,
status, token name and duration only, never bodies, query strings, file
contents or tokens. Validation errors name the bad fields without echoing the
values. Uploads go to a private folder that is removed after each request.

**Not defended against.** A compromised client can do whatever its token allows
until you revoke it, and a token with `admin` can export everything. A
compromised server host exposes everything. The classifier can miss sensitive
text, which would then be stored as plaintext. Rate limits are basic and are not
denial-of-service protection.

## Local receipt extraction and confirmation

- Text/Markdown-like files can be extracted into an `extracted.md` sidecar with `--extract`.
- PDF receipts start with `pdftotext -layout`, preserving printed columns when a PDF has a native text layer.
- A non-empty text layer is not automatically considered adequate: medical PDF receipts use the small ordered route `pdftotext -layout` → macOS Vision OCR → Docling, stopping when explicit paid-amount and date evidence is present. MarkItDown and Tesseract remain available only for generic, non-receipt attachment extraction.
- Kaal never guesses the paid amount or date from an unlabelled total, balance, or date-like value. Ambiguous fields stay blank for review.
- The narrow exception is an explicitly **SALE/PAYMENT - APPROVED** card receipt with a labelled **Total Amount**: that is recorded as the completed card payment. A bare `Total` or `Total Amount` is still rejected.
- Kaal records field provenance (`labelled-text`, `docling-structured`, `manual`, or `confirmed`). Manual corrections survive re-extraction.
- A receipt cannot be marked reviewed until it has a paid amount and at least one receipt date (service date or payment date). Marking it reviewed confirms non-manual values currently shown in the review screen.

Current installed support on this machine:

- Tesseract: `/etc/profiles/per-user/vrathod/bin/tesseract`
- MarkItDown: installed in `/Users/vrathod/.secure-notes/.venv`

## Commands

For safe, repeatable use by Claude Code, Cursor, Hermes, and other local coding
agents, see [`AGENTS.md`](AGENTS.md).

Initialize/status:

```bash
kaal init
kaal status
```

Classify text without saving:

```bash
kaal classify /path/to/note.md
pbpaste | kaal classify
```

Add a Markdown note with automatic classification:

```bash
kaal add --title "Groceries" --tags home --body $'# Groceries\n\n- milk\n- eggs'
kaal add --title "My ID Info" --tags pii,ids --body $'SSN: <your-ssn>\nDriver License: <your-license>'
```

Override classification:

```bash
kaal add --title "Tax note" --sensitivity sensitive --body-file /path/to/tax-note.md
kaal add --title "Public note" --sensitivity public --body "No sensitive content"
```

Or pipe content:

```bash
pbpaste | kaal add --title "Tax note" --tags taxes,pii --sensitivity auto
```

List/search metadata only:

```bash
kaal list
kaal list --tag pii
kaal search passport
```

Show redacted by default:

```bash
kaal show "My ID Info"
```

Show full plaintext only when explicitly needed:

```bash
kaal show "My ID Info" --full
```

Copy a field without printing it:

```bash
kaal copy "My ID Info" --field ssn
kaal copy "My ID Info" --field driver_license
```

Field copy works best when notes contain lines like:

```text
SSN: <your-ssn>
Driver License: <your-license>
Passport: <your-passport-number>
DOB: <your-date-of-birth>
```

Append to a note. A plaintext note that becomes sensitive after append is upgraded to encrypted storage automatically:

```bash
kaal append "My note" --body "New detail here"
```

Attach a file and preserve the original:

```bash
kaal attach "Tax 2025" /path/to/tax-document.pdf --extract
kaal attach "IDs" /path/to/license.jpg --ocr
kaal attach "Public note" /path/to/public.txt --extract --sensitivity public
kaal attach "Private note" /path/to/statement.pdf --extract --sensitivity sensitive
```

Export an attachment when needed:

```bash
kaal export-attachment "Tax 2025" ATTACHMENT_ID --output /tmp/tax-document.pdf
```

## One-click browser capture from Chrome

The Chrome extension presents two explicit capture choices:

- **Save to Kaal** saves a webpage, PDF, local download, or linked item as a
  normal Kaal note with an attached managed copy. Standard automatic sensitivity
  classification applies, and source URL query strings/fragments are not kept.
- **Save selected text to Kaal** appears when right-clicking selected browser
  text and creates a normal note from that text plus sanitized page provenance.
- **Save as medical receipt** uses the distinct medical receipt workflow below.

General browser captures preserve the source artifact and never delete the
original browser download:

```bash
kaal capture /path/to/file --title "Useful reference"
```

### Kaal Dashboard

The extension's **Open Kaal dashboard** command opens a local two-tab view:

- **Notes** is the generic interface for every non-medical Kaal note,
  including browser captures, selected-text captures, manually created notes,
  and imports. It supports metadata search; explicit body/extracted-text reveal;
  create and edit of title/tags/body; optional managed local-file attachments;
  generic extraction/OCR; and recoverable trash/restore.
- **Medical Receipts** preserves the dedicated receipt inbox, evidence review,
  and reporting workflow. Receipts never appear in Notes.

Notes listing is metadata-first; note bodies and extracted text are sent to
the browser only after an explicit reveal action. A note must be moved
to Trash before permanent deletion, then its record ID must be typed exactly.
Permanent deletion removes only Kaal-managed note, attachment, and extraction
sidecar copies—never the original browser/local source.

```bash
# The CLI namespace remains `library` for compatibility.
kaal library list
kaal library show NOTE_ID
kaal library trash NOTE_ID
kaal library restore NOTE_ID
kaal library purge NOTE_ID --yes
```

## One-click medical receipt capture from Chrome

Medical receipts are intentionally stored as **plaintext** in Kaal when using
this workflow. The receipt original and its extracted/OCR text remain local,
with normal macOS account permissions (not Kaal application-level encryption).

Capture a downloaded receipt directly:

```bash
kaal medical capture /path/to/receipt.pdf \
  --source-url "https://portal.example/receipt/123" \
  --source-title "Payment confirmed"
kaal medical list --inbox
```

The capture command creates a `medical`, `receipt`, `inbox` record, preserves a
plaintext copy of the original, and extracts PDF text/OCR when possible.

### Receipt values, family members and reimbursement

Each receipt stores dates as `YYYY-MM-DD` and amounts as plain numbers such as
`123.45`, whatever the receipt printed; the printed text is kept beside them in
`field_raw`. Numeric dates are read month first. A date or amount Kaal cannot
read is left empty and flagged, and a correction you type is refused if it
cannot be read. Older records are converted automatically.

```bash
# The people receipts belong to. Names on receipts are matched to this list
# ignoring order, case and punctuation ("DOE, JANE Q" matches "Jane Doe").
kaal medical people --add "Jane Doe" --add "John Doe"
kaal medical people

# Correct a field, then mark the receipt reviewed.
kaal medical update NOTE_ID --fields-json '{"service_date": "3/4/2026", "amount": "40.00"}'
kaal medical review NOTE_ID

# Record a reimbursement (for example an HSA withdrawal), or undo it.
kaal medical reimburse NOTE_ID OTHER_ID --date 2026-05-01 --note "HSA withdrawal 12"
kaal medical reimburse NOTE_ID --undo

# Totals: overall, per person, per year, and what is not yet reimbursed.
kaal medical report --year 2026
kaal medical report --reimbursed no
```

`kaal medical evaluate` measures how well extraction is doing. It runs on the
server, re-extracts each receipt that has values you typed or confirmed,
compares the two, and prints accuracy per field with receipt ids and outcomes
only, never a value. It changes nothing in the vault, so run it before and
after an extraction change to see the difference.

The report dates a receipt by its service date, or by its payment date when it
has no service date. Receipts with no date are listed under `undated` and
receipts with no amount under `missing_amount`, so nothing is left out
silently. `needs_review` shows how much of the total comes from receipts nobody
has confirmed yet. Only a reviewed receipt can be marked reimbursed.

For the one-click Chrome toolbar and right-click workflow, see
[`chrome-extension/README.md`](chrome-extension/README.md). It includes the
one-time local Native Messaging host installation and Chrome's **Load unpacked**
step.

Delete a note and its attachments:

```bash
kaal delete NOTE_ID --yes
```

## Recommended Hermes usage

Good prompts:

- "Save this in Kaal as 'Driver license' with tags pii,ids: ..."
- "Classify this note before saving it in Kaal: ..."
- "List my Kaal notes."
- "Show my passport note redacted."
- "Copy the SSN field from my ID note to clipboard."
- "Attach this PDF to my Tax 2025 Kaal note and extract Markdown: /path/to/file.pdf"
- "Attach this JPG to my IDs note and OCR it: /path/to/file.jpg"

Avoid asking Hermes to print full SSNs or documents unless you really need that plaintext in the terminal/chat.
