# Kaal for Chrome

Saves the page in front of you to your Kaal server, and lets you check a
medical receipt beside the page it came from.

## What it does

- **One main button.** Click the Kaal icon. The button says **Save to Kaal**,
  or **Save receipt** on a site you have saved receipts from before. The other
  choice is a link underneath.
- **A web page is kept as a PDF**, printed by Chrome from the tab you are
  looking at, so a page behind a sign-in is saved as you see it: with the
  styles it has on the screen, not the site's print version, and on paper as
  wide as the page, so nothing is cut off at the right. A tab that is already
  a PDF, a linked file, and a file opened from this computer are kept
  unchanged, and so is a PDF that a web page shows inside itself: the PDF is
  what is saved, not a print of the page around it.
- **A receipt opens in the side panel**, beside the page. It shows the values
  Kaal read and where each came from. "Looks right" marks it reviewed; "Save
  for later" leaves it in the inbox. The panel then closes.
- **Right-click** has two entries: "Save to Kaal" (the page, a link, or the
  text you selected) and "Save as medical receipt".
- **The number on the icon** is how many receipts are waiting to be checked.
  The popup links to them in the web app.
- **Receipt sites follow you.** The sites you have saved receipts from are
  remembered by this browser and also learnt from the server, so a new browser
  profile offers "Save receipt" first on them straight away.
- **Keyboard:** Alt+Shift+K opens the popup, Alt+Shift+S saves the page with
  the main button's choice. Change them at `chrome://extensions/shortcuts`.

Notes, totals, the Trash and settings are in Kaal's web app.

## Setting it up

1. Install **Kaal** from the
   [Chrome Web Store](https://chromewebstore.google.com/detail/kaal/jimjfomjjedakpipjfpagdmkecjiehll).
   Chrome keeps it up to date. A Kaal server knows that copy from 1.0.0-rc2 on;
   an older one says "This Kaal does not know that extension" until it is
   updated.

   To run this folder instead (to try a change to the extension), open
   `chrome://extensions`, turn on **Developer mode**, choose **Load
   unpacked** and pick this `chrome-extension` folder. It has an id of its
   own, which Kaal servers also know; turn off the store's copy while you use
   it. After an update, open the Kaal popup and press **Finish updating** (or
   press the reload arrow on the extension's card). Restarting Chrome is not
   enough: it reads the new manifest but keeps running the old background
   worker.
2. Open the extension's **Settings** (the button in the popup), enter Kaal's
   address (for example `https://kaal.example.ts.net`) and press **Connect
   with my Kaal sign-in**. Kaal's own page opens in a window of its own: sign
   in as you usually do (password or Google) and press **Allow**. The
   extension gets a token of its own for the vault you had open, allowed to
   read, reveal and write as far as your role there allows. It appears under
   Settings, Tokens, in the web app, where you can revoke it.

   The single-use code Kaal hands back goes only to this extension's own
   address, which Kaal chooses itself, and is useless without a secret the
   extension made and never sends anywhere else.

   You can still paste a token instead (under "Or paste a token"), for
   example one made on the server with
   `kaal-server token create chrome-extension --scopes read,reveal,write`.

The address and token are kept in this Chrome profile's extension storage, on
this computer only, readable by anything that can read the profile's files.
"Disconnect" removes them. Revoke the token (web app: Settings, Tokens; server:
`kaal-server token revoke NAME`) if the computer is lost.

## What Chrome will ask for, and why

The extension holds **no access to any website**. It asks for:

- **Debugger.** Chrome gives an extension no other way to make a true PDF of a
  page, and the same route fetches a PDF or linked file through the tab you
  are looking at, so that your sign-in on that site applies. It is used only
  for the moment a save takes, and Chrome shows a "debugging this browser" bar
  while it does. Chrome describes this permission in broad terms ("access the
  page debugger backend", "read and change all your data on all websites"),
  because the debugger could do that; this extension attaches only to the tab
  you save from, when you save.
- **Active tab.** Chrome tells the extension which page is in front only when
  you invoke it there: the toolbar button, a right-click entry, or the
  keyboard shortcut.
- **Side panel, context menus, storage, alarms.** The review panel, the two
  right-click entries, the saved address and token, and the periodic refresh of
  the number on the icon and the list of receipt sites.
- **Identity.** Only for "Connect with my Kaal sign-in": it lets the extension
  open Kaal's own sign-in page in a window of its own and receive the
  single-use code at the extension's own address. It gives no access to your
  Google or Chrome account.

If a file cannot be read through the tab (a site that refuses, or a file on
another site), nothing is saved and the panel offers **"Let Kaal fetch from
that-site"**. Pressing it has Chrome ask you for access to that one site; if
you agree, files from it are fetched directly from then on. Access is only ever
asked for one site at a time, by you, and can be withdrawn on the extension's
card at `chrome://extensions`.

It reaches your Kaal server because the server names this extension as allowed
to call it (see `KAAL_EXTENSION_IDS` in the main README). The server must be
version 0.19 or later, and 0.25 or later for "Connect with my Kaal sign-in".

## When something goes wrong

The popup or the side panel says what happened and what to do. The common ones:

- **"Kaal isn't connected yet"** or **"didn't accept this extension's token"**:
  open Settings and enter the address and a current token.
- **"Can't reach Kaal"**: this computer is not on the network the server is on.
- **"Chrome wouldn't let Kaal print this page"**: the developer tools are open
  on that tab, or it is one of Chrome's own pages.
- **"This PDF can't be fetched a second time"**: to keep a file, Kaal asks
  its site for it again, and this site answered with a web page instead. That
  happens when a PDF is made for one viewing, such as a statement shown after
  you press a button. Download it with the arrow at the top right of the PDF,
  open the downloaded file in Chrome and save that, or add it in the web app.
- **A file on this computer** (a PDF opened from a folder, at a `file://`
  address) is saved as the file it is. A copy of the extension from the Chrome
  Web Store may not read files until you allow it: right-click the Kaal icon,
  choose Manage extension, and turn on "Allow access to file URLs". "Kaal
  hasn't been let into files on this computer yet" has a button under it that
  asks Chrome; press it and the file is saved. Or add the file in the web app.

## Upgrading from the earlier version

The earlier extension passed captures through a helper program on the Mac. That
helper is gone. Its registration file can be deleted:
`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/com.kaal.receipt_capture.json`
(and the same file inside any other Chrome profile folder it was installed
for). Any `Kaal Capture` folder left in Downloads is no longer used.

## For developers

No build step. `lib.js` holds the decisions that need no browser
(`tests/test_extension.py` runs it under Node). `web/` holds copies of the web
app's files that the side panel shares; change the originals in `kaal/web/` and
run `bin/sync-extension-web.py`. The icons in `icons/` are drawn by
`bin/make-icons.py`.

`bin/package-extension.py` builds the zip for the Chrome Web Store from the
files the extension uses, then unpacks it and checks that every file it names
is in it. [`store/`](store/listing.md) holds the store's text and pictures
(`store/make-screenshots.sh` takes them again). The store's copy has an id of
its own, which a Kaal server has to be told about: see the listing.

`tests/test_extension_e2e.py` drives the extension in a real browser against a
throwaway server and a made-up receipt page: connect, save a page, save a
receipt, review it in the side panel. It is off by default and needs Chrome for
Testing or Chromium (ordinary Chrome no longer loads an unpacked extension from
the command line) and Node 22 or later:

```bash
KAAL_E2E=1 .venv/bin/python -m pytest -q tests/test_extension_e2e.py
```

It cannot press the right-click entries or the keyboard shortcut; try those by
hand after changing them.

One more test needs a person. Chrome's question when the extension asks for
access to a site cannot be answered by a test or a switch, so this one opens a
visible test browser and waits up to three minutes for you to press "Allow":

```bash
KAAL_E2E=1 KAAL_E2E_ASK=1 .venv/bin/python -m pytest -q tests/test_extension_e2e.py -k asking
```
