# Kaal

Kaal is your local encrypted notes vault for highly sensitive personal data. The name comes from the Sanskrit word for time.

Vault: `/Users/vrathod/.secure-notes`
Primary CLI: `/Users/vrathod/.local/bin/kaal`
Compatibility CLI: `/Users/vrathod/.local/bin/secure-notes`
Key: macOS Keychain item `Hermes Secure Notes` / `vault-key-v1`
Encryption: AES-256-GCM using Python `cryptography`

## Security model

- Note bodies and attachments are encrypted at rest.
- The index stores only metadata: id, title, tags, timestamps, and attachment names/sizes/hashes.
- The encryption key is stored in macOS Keychain, not in vault files.
- File permissions are restrictive: vault directories `700`, encrypted files `600`.
- Default note display is redacted.
- Full plaintext should usually be copied to clipboard rather than printed in chat.

Important: if full sensitive data is printed in a Hermes chat, it becomes part of that conversation context. Prefer `copy` for fields like SSN.

## Commands

Initialize/status:

```bash
kaal init
kaal status
```

Add a note:

```bash
kaal add --title "My ID Info" --tags pii,ids --body $'SSN: 123-45-6789\nDriver License: X1234567'
```

Or pipe content:

```bash
pbpaste | kaal add --title "Tax note" --tags taxes,pii
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
SSN: 123-45-6789
Driver License: X1234567
Passport: 123456789
DOB: 1970-01-01
```

Append to a note:

```bash
kaal append "My ID Info" --body "New detail here"
```

Attach encrypted files:

```bash
kaal attach "Tax 2025" /path/to/tax-document.pdf
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
- "List my Kaal notes."
- "Show my passport note redacted."
- "Copy the SSN field from my ID note to clipboard."
- "Attach this PDF to my Tax 2025 Kaal note: /path/to/file.pdf"

Avoid asking Hermes to print full SSNs or documents unless you really need that plaintext in the terminal/chat.
