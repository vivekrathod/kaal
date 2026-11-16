# Kaal for Chrome

Saves the page in front of you to your Kaal server, and lets you check a
medical receipt beside the page it came from.

## What it does

- **One main button.** Click the Kaal icon. The button says **Save to Kaal**,
  or **Save receipt** on a site you have saved receipts from before. The other
  choice is a link underneath.
- **A web page is kept as a PDF**, printed by Chrome from the tab you are
  looking at, so a page behind a sign-in is saved as you see it. A tab that is
  already a PDF, and a linked file, are kept unchanged.
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

1. Open `chrome://extensions`, turn on **Developer mode**, choose **Load
   unpacked** and pick this `chrome-extension` folder. After an update, press
   the reload arrow on the extension's card.
2. On the server, create a token for this browser (skip this if you already
   have one for it):

   ```bash
   kaal-server token create chrome-extension --scopes read,reveal,write
   ```

3. Open the extension's **Settings** (the button in the popup), enter Kaal's
   address (for example `https://kaal.example.ts.net`) and paste the token.
   "Save and test" says who the server thinks you are.

The address and token are kept in this Chrome profile's extension storage, on
this computer only, readable by anything that can read the profile's files.
"Disconnect" removes them. Revoke the token on the server
(`kaal-server token revoke chrome-extension`) if the computer is lost.

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

It reaches your Kaal server because the server names this extension as allowed
to call it (see `KAAL_EXTENSION_IDS` in the main README). The server must be
version 0.19 or later.

## When something goes wrong

The popup or the side panel says what happened and what to do. The common ones:

- **"Kaal isn't connected yet"** or **"didn't accept this extension's token"**:
  open Settings and enter the address and a current token.
- **"Can't reach Kaal"**: this computer is not on the network the server is on.
- **"Chrome wouldn't let Kaal print this page"**: the developer tools are open
  on that tab, or it is one of Chrome's own pages.
- **A file on this computer** (`file://`): turn on "Allow access to file URLs"
  on the extension's card, or add the file in the web app.

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
run `bin/sync-extension-web.py`.

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
