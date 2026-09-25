# Kaal Capture Chrome extension

This unpacked Chrome extension captures browser items into local Kaal storage without uploading them.

## Capture modes

- **Save to Kaal:** saves the current page, PDF, Downloads file, or linked item as a normal Kaal note and managed attachment. Kaal applies its standard automatic sensitivity classification; the original download stays untouched.
- **Save as medical receipt:** creates the distinct plaintext `medical`, `receipt`, `inbox` record used by the receipt dashboard and review lifecycle.
- **Save selected text to Kaal:** right-click selected page text to create a normal Kaal note containing only that selection and its sanitized page provenance. It never triggers medical receipt processing.

The toolbar popup presents both choices explicitly. Page and link context menus do the same. HTML pages are printed to a PDF through Chrome's authenticated current tab; PDFs and linked files are downloaded unchanged. A Downloads `file://` item is copied directly into Kaal without re-downloading it.

The local Native Messaging host invokes one of:

```bash
kaal capture <artifact> --source-url <url> --source-title <title>
kaal medical capture <artifact> --source-url <url> --source-title <title>
```

General-capture URL provenance strips query strings and fragments before Kaal stores it. The extension only talks to the host on the same Mac; it does not access any cloud API.

## One-time installation on macOS

From the Kaal checkout:

```bash
/Users/vivek.rathod/SOURCE/kaal/.venv/bin/python bin/install-chrome-receipt-capture.py
```

If Chrome is launched with a dedicated `--user-data-dir`, Chrome looks for a
user-level Native Messaging manifest in that profile's own
`NativeMessagingHosts/` directory. Install an additional manifest for that
profile (the default installation remains appropriate for ordinary Chrome):

```bash
/Users/vivek.rathod/SOURCE/kaal/.venv/bin/python bin/install-chrome-receipt-capture.py \
  --chrome-app-support "$HOME/.hermes/chrome-debug"
```

If Chrome uses a non-default download directory, install with its absolute
directory so Kaal can trust only that directory's `Kaal Capture/` staging
subfolder:

```bash
/Users/vivek.rathod/SOURCE/kaal/.venv/bin/python bin/install-chrome-receipt-capture.py \
  --downloads-dir "/absolute/path/to/Chrome Downloads"
```

Then in Chrome:

1. Visit `chrome://extensions`.
2. Enable **Developer mode**.
3. Select **Load unpacked** and choose this `chrome-extension` directory.
4. Pin **Kaal Capture** to the toolbar.

The extension has a fixed public extension ID, so the host manifest installed above is already restricted to this extension alone.

## Normal use

1. Open a page, PDF, Downloads file, or linked item in Chrome. To save just text, select it first.
2. Click the Kaal toolbar icon and select **Save to Kaal** for a normal capture, or **Save as medical receipt** for a paid medical receipt. The same choices are available from the page/link context menus; selected text has its own **Save selected text to Kaal** context-menu item.
3. The toolbar badge stays `OK` when Kaal copied the artifact; `ERR` opens a page with the actual failure reason.
### Manage Kaal Notes

Choose **Open Kaal dashboard** from the toolbar popup or page context menu.
The default **Notes** tab is a metadata-first view of the 100 newest
non-medical Kaal notes: browser captures, selected-text captures, manually
created notes, imports, and other ordinary records. Medical receipts remain in
the dedicated **Medical Receipts** tab so their evidence and review workflow is
not mixed into regular notes. The Notes toolbar includes **Trash**, which changes
to **Back to notes** while viewing recoverable deleted notes.

Notes actions are local-only:

- **New note** and **Edit** create or update titles, tags, and Markdown bodies.
- **Reveal note** (or **Edit**) explicitly retrieves its body. Note bodies are
  never prefetched into the dashboard.
- An optional local file can be attached when saving a new or edited note.
  Chrome stages it in the configured `Kaal Capture/` directory; Kaal copies it
  into managed storage and leaves the selected original untouched.
- **Extract / OCR attachments** uses the generic Kaal extraction pipeline;
  **View extracted text** is also explicit and capped by the host.
- **Move to Trash** is recoverable. In Notes Trash, **Restore** reverses it;
  **Delete permanently** requires typing the note record ID and removes only
  Kaal-managed note, attachment, and sidecar copies.

CLI equivalents are available for diagnostics and automation:

```bash
kaal library list
kaal library show <note-id>
kaal library trash <note-id>
kaal library restore <note-id>
kaal library purge <note-id> --yes
```

### Verify captured receipts

Open the **Medical Receipts** dashboard tab. Use **Needs review**, **Reviewed**,
or **Trash** to narrow the receipt list. It lists the 100 newest receipt
records, including capture date, inbox status, original
attachment filename, extraction status, source, tags, and Kaal record ID. Click
**Extract / OCR text** to retry text extraction for that specific receipt; medical
PDFs use `pdftotext -layout`, macOS Vision OCR, then Docling as needed.
Once text exists, **View extracted text** displays it only after you explicitly
request it.

### Manage receipts safely

The dashboard has a local title/source/ID filter plus **Current receipts** and
**Trash** views. Use **Move to Trash** for normal deletion: it immediately
hides the record from the current list but keeps Kaal's note, attachment copy,
and sidecar text so **Restore** can undo it. The original browser download is
never moved or deleted.

In the Trash view, **Delete permanently** requires typing the receipt record
ID. You can also select multiple trashed receipts and use **Delete selected
permanently**; this requires typing all selected IDs exactly, comma-separated.
Only then does Kaal erase its own note, attachment copies, extracted-text
sidecars, and metadata. This action cannot be undone.

For active receipts, use **Mark reviewed** after checking the stored receipt
and optional extracted text. This removes the `inbox` status; **Return to
inbox** reverses that decision.

Chrome briefly uses a `Kaal Capture/` subdirectory inside the download location
registered during installation. The native host deletes only a successfully
copied file from that exact absolute directory whose path matches this
extension's generated `receipt-<timestamp>-<title>.pdf` pattern; it never
deletes a file elsewhere.

List captured receipts:

```bash
kaal medical list --inbox
```

Manage from the CLI if needed:

```bash
kaal medical trash <receipt-id>
kaal medical restore <receipt-id>
kaal medical purge <receipt-id> --yes
kaal medical purge-bulk <receipt-id> <receipt-id> --yes
```

## Permissions

- `downloads`: save the original PDF/link or generated page PDF.
- `debugger`: invoke Chrome's `Page.printToPDF` for the active authenticated HTML receipt page. This is only used after you click the extension button.
- `nativeMessaging`: pass the local staging-file path and page metadata to Kaal.
- `activeTab`, `tabs`, `contextMenus`: identify the clicked tab/link and offer the capture controls.
