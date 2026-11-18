# Chrome Web Store listing

What to enter in the Chrome Web Store Developer Dashboard for Kaal for Chrome.
Field limits are in the headings; `tests/test_store_material.py` checks that
the text fits them and that the pictures here are the sizes the store wants.

## The package

```bash
bin/package-extension.py
```

writes `dist/kaal-chrome-VERSION.zip` from exactly the files the extension
uses (it follows them from `manifest.json`), leaving out the README and this
folder, and prints what went in. Upload that zip.

The zip's `manifest.json` has no `key` field: the store refuses one, and gives
the extension an id of its own. Kaal's server only answers the extension whose
id it knows, so after the first upload:

1. In the dashboard, open the item's **Package** tab and copy its public key.
2. Put that key in `chrome-extension/manifest.json` as `key`, so that the copy
   loaded from this folder has the store's id too.
3. Set `OFFICIAL_EXTENSION_ID` in `kaal/server.py` to the id the dashboard
   shows (`tests/test_extension_access.py` checks that the two agree), and
   release the server. Until a server has that release, its operator can let
   the store's copy in with `KAAL_EXTENSION_IDS=STORE-ID`.

## Store listing tab

### Name (75 characters)

Taken from the manifest's `name`.

```text
Kaal
```

### Summary (132 characters)

Taken from the manifest's `description`; change it there.

```text
Save the page in front of you to your Kaal server, and check a medical receipt beside the page it came from.
```

### Description (16000 characters)

```text
Kaal for Chrome saves what you are looking at to your own Kaal server: a private vault for notes, documents and medical receipts that you, or someone you trust, run. It needs a Kaal server; Kaal is free, open-source software that installs with one command.

ONE BUTTON
Click the Kaal icon. The button says "Save to Kaal", or "Save receipt" on a site you have saved receipts from before, with the other choice underneath.
• A web page is kept as a PDF, printed by Chrome from the tab you are looking at, so a page behind a sign-in is saved as you see it.
• A PDF, or a file you link to, is kept exactly as it is.
• Right-click a page, a link or selected text for the same choices.
• Keyboard: Alt+Shift+K opens Kaal, Alt+Shift+S saves the page.

CHECK RECEIPTS ON THE SPOT
Saving a medical receipt opens Chrome's side panel beside the page, with the values Kaal read (amount, dates, patient, provider, insurer) and the line of the receipt each came from. "Looks right" marks it reviewed; "Save for later" leaves it to check another time. The number on the icon is how many receipts are waiting.

Notes, totals per person and year, reimbursements, the Trash and settings are in Kaal's web app, and in Kaal for iPhone.

PRIVATE BY DESIGN
• What you save goes only to the Kaal server you connect, with a token you create there. Nothing goes to Kaal's authors: no analytics, no ads, no tracking.
• The extension holds no access to any website. It reads the page you save through that tab, at the moment you save, and nothing else.
• If a file cannot be read that way, it offers to ask Chrome for access to that one site, and only if you press the button.

Setting up: in the extension's Settings, enter your Kaal's address and a token made in the web app under Settings → Tokens.

Kaal is free and open source under the Apache License 2.0.
```

### Category

Productivity → Tools.

### Language

English.

### Pictures

| Field | File |
|---|---|
| Store icon (128 x 128) | `chrome-extension/icons/icon-128.png` |
| Screenshot 1 (1280 x 800) | `1-check-a-receipt.png`: a receipt checked in the side panel beside the page it came from |
| Screenshot 2 | `2-save-in-one-click.png`: the popup on a receipt site |
| Screenshot 3 | `3-web-receipts.png`: receipts waiting to be checked, in the web app |
| Screenshot 4 | `4-web-note.png`: a note, with a table and a PDF, in the web app |
| Screenshot 5 | `5-web-totals.png`: totals, and what is still to claim |
| Small promo tile (440 x 280) | `promo-tile-440x280.png` |

Every page in them is made up: the clinic, the people and the amounts.
`chrome-extension/store/make-screenshots.sh` takes them again after the
screens change, against a throwaway Kaal holding made-up data.

### Addresses

| Field | Address |
|---|---|
| Official URL / homepage | `https://OWNER.github.io/REPO/` |
| Support URL | `https://OWNER.github.io/REPO/support.html` |

`OWNER` and `REPO` are the repository's GitHub owner and name; see
`ios/AppStore/listing.md` for publishing these pages with GitHub Pages.

## Privacy practices tab

### Single purpose

```text
Save the web page, PDF or file in the current tab to the user's own Kaal server, and let the user check a saved medical receipt's values in the side panel beside the page it came from.
```

### Why each permission is needed

**activeTab**

```text
To know which page the user means when they click the toolbar button, a right-click entry or the keyboard shortcut, and to read that page's address and title, which are saved with it. The extension has no other access to tabs or pages.
```

**alarms**

```text
Every ten minutes, asks the user's own Kaal server how many receipts are waiting to be checked, to show that number on the toolbar icon, and refreshes the list of sites the user has saved receipts from.
```

**contextMenus**

```text
Adds two right-click entries, "Save to Kaal" and "Save as medical receipt", for a page, a link or selected text.
```

**debugger**

```text
Chrome gives an extension no other way to save a page as a true PDF: the extension attaches to the one tab the user is saving, only when they press Save, and calls Page.printToPDF. The same route fetches a PDF or linked file through that tab, so the user's own sign-in to that site applies. It detaches as soon as the save is done; Chrome shows its "debugging this browser" bar meanwhile. It never attaches to any other tab, never runs on its own, and is used for nothing else.
```

**sidePanel**

```text
After the user saves a medical receipt, shows Kaal's review screen in the side panel beside the page it came from, so the values read from it can be checked against the page.
```

**storage**

```text
Keeps, on this computer, the address of the user's Kaal server, the token they entered, the host names of sites they have saved receipts from, and the progress of a save under way (session storage, cleared when Chrome closes).
```

**Host permission (optional_host_permissions, \*://\*/\*)**

```text
Not requested at install, and the extension holds no access to any website. Only when a file cannot be read through the tab, and the user presses "Let Kaal fetch from <that site>", does the extension ask Chrome for access to that one site, so it can fetch the file directly. The user answers Chrome's own prompt, and can take the access back on the extension's page.
```

### Remote code

No, I am not using remote code. Every script is in the package; the
extension loads nothing to run from anywhere else.

### Data usage

Tick these, because a save sends them to the user's own Kaal server (and to
nowhere else):

- **Personally identifiable information**: names and other details printed on
  saved pages and receipts.
- **Health information**: medical receipts.
- **Financial and payment information**: amounts paid, on receipts.
- **Authentication information**: the token the user enters, sent only to
  their own server.
- **Website content**: the pages, files and text the user saves, with each
  page's title and address.

Leave the rest unticked: the extension does not read browsing history,
messages, location or activity on pages; it sees a page only when the user
saves it.

Then certify all three statements: the data is not sold or transferred to
third parties, not used or transferred for anything unrelated to the single
purpose, and not used to decide creditworthiness or for lending.

### Privacy policy URL

```text
https://OWNER.github.io/REPO/privacy.html
```

## Test instructions (for the reviewer)

The dashboard has a field for these. They need the same demo server as the
iPhone app's review (see `ios/AppStore/review-notes.md`), and a token for its
`appreview` account: sign in to the web app as `appreview`, Settings →
Tokens, a token with read, reveal and write. Keep that token for as long as
the extension is in review.

```text
Kaal for Chrome needs a Kaal server. Use our demo server, which holds only made-up data.
1. Open the extension's Settings (the button in its popup). Address: DEMO-ADDRESS. Token: DEMO-TOKEN. Press "Save and test"; it says "Connected as …".
2. Open any web page and click the Kaal icon, then "Save to Kaal". The popup says "Saved to Kaal." with a link to the note.
3. To try a receipt: open a page with a receipt or invoice on it (any page will do), click the Kaal icon, then "Save as a medical receipt instead". The side panel opens with what Kaal read; press "Looks right" or "Save for later".
4. To see everything saved: open DEMO-ADDRESS in a tab and sign in with the name appreview and the password DEMO-PASSWORD.
```

## Distribution tab

Public, every region, free.
