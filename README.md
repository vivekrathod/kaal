# Kaal

Kaal is your notes vault for personal data. Notes are Markdown, and every note, attachment and piece of extracted text is encrypted at rest.

Vault: `$HOME/.secure-notes`
Primary CLI: `$HOME/.local/bin/kaal`
Compatibility CLI: `$HOME/.local/bin/secure-notes`
Key: macOS Keychain item `Hermes Secure Notes` / `vault-key-v1`, or an owner-only key file (see below)
Encryption: AES-256-GCM using Python `cryptography`
Server: optional `kaal-server`, so several machines share one vault (see [Running Kaal as a server](#running-kaal-as-a-server))

## Security model

- Notes are Markdown by default.
- Every note body, attachment and extracted/OCR text is encrypted at rest, medical receipts included.
- Kaal still labels content `public` or `sensitive` with a best-effort local classifier, or as you say with `--sensitivity`. The label is information only: it no longer decides how anything is stored.
- Attachments always preserve the original bytes; optional extracted/OCR Markdown sidecars are created separately.
- The index is encrypted too. It holds the metadata: titles, tags, timestamps, attachment names, sizes and hashes, and medical receipt fields. A copy of each attachment's metadata is kept beside it, also encrypted.
- A vault from an older version may hold non-sensitive notes, attachments, extracted text and the index readable. `kaal encrypt-all` (run on the server) encrypts them in place; `--dry-run` counts them first. It reads each item back before removing the readable copy and can be re-run after an interruption. Until it has been run, such a vault keeps its index readable so that the older version can still open it.
- The encryption key is never stored in vault files: it lives in the macOS Keychain or, with `KAAL_KEY_PROVIDER=file`, in the file named by `KAAL_KEY_FILE`, which Kaal refuses to read unless only its owner can read it.
- File permissions are restrictive: vault directories `700`, stored files `600`.
- Default note display is redacted.
- Full plaintext should usually be copied to clipboard rather than printed in chat.

The classifier is a best guess, and since everything is encrypted a wrong label no longer exposes anything.

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

**Web app.** `kaal-server` serves a small web app at `/`: open the server's
address in a browser on a laptop or phone and sign in with a device token. It
covers medical receipts: add one from a file or the phone's camera, see what
still needs review, check each value against a picture of the receipt (with
where it came from and how far to trust it), mark receipts reviewed or
reimbursed, see totals per person and year, and restore or permanently delete
what is in the Trash. It also covers notes (search, read, write, trash) and
settings: the family list and the provider names Kaal has learnt. Notes are
shown as formatted Markdown; pictures inside a note appear in place, each PDF
shows its first page with the text read from it, and a "Show source" switch
shows the raw note. HTML inside a note is displayed as text, never run.

Notes are written in Markdown, with a Preview switch beside the text. Pictures,
PDFs and other files can be added while writing a note or from the note itself
("Add files"). A file added to a new note is stored when the note is saved. In
an existing note, "Insert in text" places a file where the cursor is, as
`![name](attachment:ID)` for a picture or `[name](attachment:ID)` for anything
else; files not placed in the text are listed under the note.

A picture, PDF or plain-text file can be opened in the browser
(`GET /v1/notes/{id}/attachments/{attachment}/view`); the server checks that
the file really is what its name says. Every other kind of file, such as a
saved web page, is download-only, because opened from Kaal's own address it
could act with your sign-in. Permanent
deletion needs a token with the `delete` scope. It is plain files in
`kaal/web/` with no build step, and it only talks to the API below. The server
tells the browser to run nothing but those files and never to show them inside
another site.

**Browsers.** A web page served from the same address signs in by sending a
device token once to `POST /v1/session`. The server answers with a cookie that
page scripts cannot read (`HttpOnly`, `Secure`, `SameSite=Strict`, 30 days), and
`DELETE /v1/session` signs out. A request that changes anything and is
authenticated by that cookie must also carry the header `X-Kaal-Request: 1`,
which another website cannot make a browser send, so a forged request is
refused. Tools that send `Authorization: Bearer` are unaffected. Revoking the
token ends its browser sessions too.

For a user interface the API also offers: one receipt with the line of its text
each value came from (`GET /v1/medical/receipts/{id}`, or `kaal medical show`),
a page image of a receipt
(`GET /v1/medical/receipts/{id}/attachments/{attachment}/preview`), and
`status`, `q`, `sort`, `offset` and `limit` on the receipt list, `q` and
`offset` on the note list, with the total returned beside each page.

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

**What encryption protects.** Every note body, attachment and extracted text
is encrypted with AES-256-GCM, so someone holding a copy of the vault files
without the key (a stolen disk, a backup, a snapshot) cannot read them. Losing
the key therefore loses the vault: keep a copy of it somewhere off the server.

**What stays readable.** Not the contents and not the names: titles, tags,
attachment names and medical receipt fields are in the encrypted index. What a
copy of the vault files still shows is its shape: how many notes and attachments
there are, their ids, sizes and file dates, and the size and hash recorded beside
each encrypted attachment. Token names and their scopes are in `tokens.json`,
outside the vault. Backups made before `kaal encrypt-all` was run still hold the
older readable copies until they are pruned, so keep backups as private as the
vault.

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

**What leaves the server.** Nothing, unless `KAAL_RECEIPT_AI` is set. With it
set, each captured or re-extracted medical receipt's text and page images are
sent to the model provider behind the configured command (for `claude-cli`,
Anthropic), under that account's terms and data settings. Notes and other
attachments are never sent. The login for that command is a credential on the
server: keep it readable only by root and the service.

**Not defended against.** A compromised client can do whatever its token allows
until you revoke it, and a token with `admin` can export everything. A
compromised server host exposes everything. Rate limits are basic and are not
denial-of-service protection.

## Local receipt extraction and confirmation

- Text/Markdown-like files can be extracted into an `extracted.md` sidecar with `--extract`.
- PDF receipts start with `pdftotext -layout`, preserving printed columns when a PDF has a native text layer.
- A non-empty text layer is not automatically considered adequate: medical PDF receipts use the small ordered route `pdftotext -layout` → macOS Vision OCR → Docling, stopping when explicit paid-amount and date evidence is present. A PDF attached to an ordinary note is read from its text layer, or by OCR when it has none; Docling and MarkItDown are tried only if both give nothing.
- Kaal never guesses the paid amount or date from an unlabelled total, balance, or date-like value. Ambiguous fields stay blank for review.
- The narrow exception is an explicitly **SALE/PAYMENT - APPROVED** card receipt with a labelled **Total Amount**: that is recorded as the completed card payment. A bare `Total` or `Total Amount` is still rejected.
- Kaal records field provenance (`labelled-text`, `docling-structured`, `ai-grounded`, `ai-unverified`, `ai-amount-due`, `manual`, or `confirmed`). Manual corrections and confirmed values survive re-extraction.
- A receipt cannot be marked reviewed until it has a paid amount and at least one receipt date (service date or payment date). Marking it reviewed confirms non-manual values currently shown in the review screen.

### Reading receipt fields with a model (optional, off by default)

The label rules above only recognise a few fixed layouts. A server can instead
ask a language model which values on a receipt are which:

```bash
KAAL_RECEIPT_AI=claude-cli            # default: off
KAAL_RECEIPT_AI_COMMAND=/path/to/claude   # default: claude on PATH
KAAL_RECEIPT_AI_MODEL=haiku           # default
KAAL_RECEIPT_AI_TIMEOUT=90            # seconds, default
```

With this on, a receipt PDF's own text layer is used as it is (a scan with no
text layer is read by OCR; Docling is not used), and the model is given that
text plus an image of the first pages, or the photo itself. It runs with no
tools, no saved session, no user or project settings and an empty working
directory, so nothing printed on a receipt can make it do anything.

The model only labels; Kaal decides what to trust:

- A value found in the text read from the receipt is `ai-grounded`. Amounts and
  dates are matched by value, so `$1,234.50` grounds `1234.50`.
- A value not found there (a provider name that exists only as a logo, anything
  from a blurry photo) is kept as `ai-unverified`. Unverified amounts and dates
  are left out of report totals until you review the receipt.
- Where a label rule and the model disagree on an amount or a date, the rule's
  value stays; for names the model's value is used. Both are kept under
  `conflicts` for you to see, and reviewing or correcting the field settles it.
- When a receipt lists several payments and no total (two prescriptions on one
  pharmacy receipt), Kaal adds them up, keeps the parts in `field_raw`, and
  marks the total `ai-unverified` because that number is printed nowhere.
- When the document is a bill that shows no payment, the amount it says you owe
  is offered as `ai-amount-due`. Like any unverified amount it stays out of the
  totals until you review the receipt.
- An individual clinician is never accepted as the provider.

**Remembered provider names.** Receipts often print a billing company's legal
name, or a short brand, where you would write the practice's name. When you
type or confirm a provider on a receipt where the model read a different name,
Kaal remembers the pair, and the next receipt printing that name shows your
wording, marked `remembered`, with the printed name kept in `field_raw`. A
provider you typed is never replaced.

```bash
kaal medical providers                          # what Kaal has learnt
kaal medical providers --forget "Printed Name"  # stop replacing it; not relearnt
```

If the model is unreachable, signed out or rate-limited, the receipt is still
captured with whatever the label rules find, and the reason is recorded on it.
`kaal status` shows whether the model is configured and its command exists.
This sends receipt text and page images to the model's provider: see the
threat model.

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

Append to a note. A note labelled public whose text becomes sensitive after an append is relabelled sensitive:

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

Medical receipts are encrypted like everything else: the original and its
extracted/OCR text are stored encrypted, and the fields read from a receipt
live in the encrypted index.

Capture a downloaded receipt directly:

```bash
kaal medical capture /path/to/receipt.pdf \
  --source-url "https://portal.example/receipt/123" \
  --source-title "Payment confirmed"
kaal medical list --inbox
```

The capture command creates a `medical`, `receipt`, `inbox` record, preserves an
encrypted copy of the original, and extracts PDF text/OCR when possible.

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

`kaal medical reextract NOTE_ID…` reads stored receipts again, and
`kaal medical reextract --unreviewed` does it for everything still awaiting
review. Values you typed or confirmed are kept, and so is the reimbursement.

On a server, a captured receipt is stored at once and read afterwards: it shows
status `extracting` until the background worker has read it, then
`needs_review`, or `failed` if it could not be read. A re-read is queued the
same way. Work left unfinished by a restart is picked up when the server
starts. Set `KAAL_BACKGROUND_EXTRACTION=0` to read during the capture request
instead.

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
