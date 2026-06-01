# Kaal

Kaal is your local notes vault for personal data. Notes are Markdown by default and Kaal now uses best-effort local sensitivity detection to decide whether a note or attachment should be encrypted.

Vault: `/Users/vrathod/.secure-notes`
Primary CLI: `/Users/vrathod/.local/bin/kaal`
Compatibility CLI: `/Users/vrathod/.local/bin/secure-notes`
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

## Optional document extraction/OCR

- Text/Markdown-like files can be extracted into an `extracted.md` sidecar with `--extract`.
- PDFs use Microsoft MarkItDown when installed in the Kaal venv.
- Images use local Tesseract OCR with `--ocr` when `tesseract` is available.
- EasyOCR is intentionally not the default because it pulls in PyTorch and is much heavier.

Current installed support on this machine:

- Tesseract: `/etc/profiles/per-user/vrathod/bin/tesseract`
- MarkItDown: installed in `/Users/vrathod/.secure-notes/.venv`

## Commands

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
