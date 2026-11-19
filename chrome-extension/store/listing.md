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
folder, and prints what went in. `bin/package-extension.py --check FILE.zip`
unpacks a zip and checks that every file its manifest, pages and scripts name
is in it, and that its version is the one in `manifest.json`. Upload that zip.

The zip's `manifest.json` has no `key` field: the store gives the extension an
id of its own and does not take a key from the package. Kaal's server only
answers an extension whose id it knows, both for saving and for "Connect with
my Kaal sign-in".

The store's copy has the id `jimjfomjjedakpipjfpagdmkecjiehll`. A Kaal server
from 1.0.0-rc2 on knows it, beside the id of the copy loaded from this folder
(`STORE_EXTENSION_ID` and `OFFICIAL_EXTENSION_ID` in `kaal/server.py`), so
nothing needs setting. An older server's operator lets the store's copy in by
naming both ids (the setting replaces the list):

```
KAAL_EXTENSION_IDS=ebmkcbpcihgiaoimcpncmadadogclmld,jimjfomjjedakpipjfpagdmkecjiehll
```

The demo server the reviewer uses must run 1.0.0-rc2 or later.

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
Click the Kaal icon. The button says "Save to Kaal", or "Save receipt" on a site that receipts in your vault were saved from before, with the other choice underneath.
• A web page is kept as a PDF, printed by Chrome from the tab you are looking at, so a page behind a sign-in is saved as you see it.
• A PDF, or a file you link to, is kept exactly as it is.
• Right-click a page, a link or selected text for the same choices.
• Keyboard: Alt+Shift+K opens Kaal, Alt+Shift+S saves the page.

CHECK RECEIPTS ON THE SPOT
Saving a medical receipt opens Chrome's side panel beside the page, with the values Kaal read (amount, dates, patient, provider, insurer) and the line of the receipt each came from. "Looks right" marks it reviewed; "Save for later" leaves it to check another time. The number on the icon is how many receipts are waiting.

Notes, totals per person and year, reimbursements, the Trash and settings are in Kaal's web app, and in Kaal for iPhone.

PRIVATE BY DESIGN
• What you save goes only to the Kaal server you connect. Nothing goes to Kaal's authors: no analytics, no ads, no tracking.
• You connect it by signing in on your Kaal's own page. The extension never sees your password, and gets a token of its own that you can revoke.
• The extension asks for no access to websites of its own. It reads the page you save through that tab, at the moment you save, and nothing else. Chrome's warning when you install it is broad because of the debugger permission, which Chrome describes for everything a debugger could do; this extension uses it only on the tab you save, while it saves.
• If a file cannot be read that way, it offers to ask Chrome for access to that one site, and only if you press the button.

Setting up: in the extension's Settings, enter your Kaal's address and press "Connect with my Kaal sign-in". Kaal's own sign-in page opens; sign in as you usually do and press Allow. If that browser was already signed in for a while, Kaal asks for your password again before Allow. (Or paste a token made in the web app under Settings → Tokens.) Whoever runs your Kaal server may need to allow this extension first: see the support page.

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
Every ten minutes, asks the user's own Kaal server how many receipts are waiting to be checked, to show that number on the toolbar icon, and for the host names of the sites that receipts in the user's vault were saved from, so that "Save receipt" is offered first on those sites.
```

**contextMenus**

```text
Adds two right-click entries, "Save to Kaal" and "Save as medical receipt", for a page, a link or selected text.
```

**debugger**

```text
Chrome gives an extension no other way to save a page as a true PDF. The extension attaches to the one tab the user is saving, only when they press Save, and sends these commands and no others. Runtime.evaluate, with the one fixed expression "document.contentType", to learn whether the tab is showing a PDF; this is the only thing evaluated in a page, it is never built from page content, and it changes nothing. Page.printToPDF, to print a web page to a PDF. For a PDF or a linked file: Page.getFrameTree, Network.loadNetworkResource, IO.read and IO.close, which load the file through that tab, so the user's own sign-in to that site applies, and read it. The extension detaches as soon as the save is done; Chrome shows its "debugging this browser" bar meanwhile. It never attaches to any other tab and never runs on its own.
```

**identity**

```text
Used for one thing: the "Connect with my Kaal sign-in" button on the settings page calls chrome.identity.launchWebAuthFlow to open the sign-in page of the user's own Kaal server (the address they typed) in a window of its own, and to receive the single-use code that server sends back to the extension's own chromiumapp.org address. The extension exchanges the code, with a secret that never left it (PKCE), for a token for that server. It never calls getAuthToken or getProfileUserInfo, has no "oauth2" section and no "identity.email" permission, and gets no access to the user's Google account, Chrome profile or email address.
```

**sidePanel**

```text
After the user saves a medical receipt, shows Kaal's review screen in the side panel beside the page it came from, so the values read from it can be checked against the page.
```

**storage**

```text
Keeps, on this computer, the address of the user's Kaal server, the token that server gave the extension (or one the user pasted), the host names of the sites that receipts in their vault were saved from (the ones saved here, and the list their server gives), and the progress of a save under way (session storage, cleared when Chrome closes).
```

**Host permission (optional_host_permissions)**

```text
Not requested at install: the extension asks for no access to websites of its own. Only when a file cannot be read through the tab, and the user presses "Let Kaal fetch from <that site>", does the extension ask Chrome for access to that one site, so it can fetch the file directly. The user answers Chrome's own prompt, and can take the access back on the extension's page.
```

(The manifest names every site, `*://*/*`, as *optional*. That grants nothing
by itself; it only lets the extension ask for one site at a time. Chrome's
install warning, "Read and change all your data on all websites", comes from
the debugger permission, not from this.)

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
- **Authentication information**: the token for the user's Kaal server, sent
  only to that server. The user's password is typed into their server's own
  page and never reaches the extension.
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
iPhone app's review (see `ios/AppStore/review-notes.md`), running Kaal 1.0.0-rc2
or later, which knows the store's copy; an older one answers "This Kaal does
not know that extension". Try the steps yourself before submitting, with a
copy that has the store's extension id: a draft cannot be installed from the
store, so unzip the uploaded package, put the store item's public key (the
dashboard's Package tab, "View public key", the text between the BEGIN and
END lines) in its manifest as `"key"`, and load that folder with "Load
unpacked" on `chrome://extensions`. Chrome then gives it the store's id.

```text
Kaal for Chrome needs a Kaal server. Use our demo server, which holds only made-up data.
1. Open the extension's Settings (the button in its popup). Type the address DEMO-ADDRESS and press "Connect with my Kaal sign-in". The demo server's sign-in page opens in a window: sign in with the name reviewer and the password DEMO-PASSWORD, then press Allow. (If the window shows Allow at once because this browser was signed in to the demo server earlier, it asks for the same password before Allow.) The settings page says "Connected as …". (The identity permission is used only for this window.)
2. Open any web page and click the Kaal icon, then "Save to Kaal". Chrome shows its debugging bar for a moment while the page is printed to a PDF. The popup says "Saved to Kaal." with a link to the note.
3. To try a receipt: open a page with a receipt or invoice on it (any page will do), click the Kaal icon, then "Save as a medical receipt instead". The side panel opens with what Kaal read; press "Looks right" or "Save for later".
4. To see everything saved: open DEMO-ADDRESS in a tab and, if it asks, sign in with the same name and password.
```

## Distribution tab

Public, every region, free.
