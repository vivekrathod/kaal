# Kaal

Kaal is your local notes vault for personal data. Notes are Markdown by default and Kaal now uses best-effort local sensitivity detection to decide whether a note or attachment should be encrypted.

Vault: `$HOME/.secure-notes`
Primary CLI: `$HOME/.local/bin/kaal`
Compatibility CLI: `$HOME/.local/bin/secure-notes`
Key: macOS Keychain item `Hermes Secure Notes` / `vault-key-v1`
Encryption: AES-256-GCM using Python `cryptography`

## Security model

- Notes are Markdown by default.
- Kaal classifies note/attachment content locally as `public` or `sensitive`.
- Sensitive notes and attachments are encrypted at rest.
- Public notes are stored as plaintext Markdown in the vault.
- Attachments always preserve the original bytes; optional extracted/OCR Markdown sidecars are created separately.
- Sensitive parent notes force attachments to be encrypted too.
- The index stores metadata: id, title, tags, timestamps, storage mode, classification reasons, attachment names/sizes/hashes.
- The encryption key is stored in macOS Keychain, not in vault files.
- File permissions are restrictive: vault directories `700`, stored files `600`.
- Default note display is redacted.
- Full plaintext should usually be copied to clipboard rather than printed in chat.

Important: the classifier is a best guess. False negatives are the dangerous case. Use `--sensitivity sensitive` for anything you know is private, and only use `--sensitivity public` when you are comfortable storing the note or attachment as plaintext.

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
