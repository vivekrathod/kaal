# Getting help

Kaal is free software made by volunteers. There is no help desk, but
questions and problem reports are welcome on GitHub.

## Asking a question or reporting a problem

Open an issue on the project's GitHub repository, under **Issues**. Say:

- which part you are using: the server, the web app, Kaal for iPhone or Kaal
  for Chrome;
- the versions: the web app shows Kaal's version on the Settings page, under
  Your account; the iPhone app shows both its own and the server's under
  Settings; `deploy/docker/kaalctl status` prints the server's; and the
  extension's is on its card at `chrome://extensions`;
- how the server was installed (the Docker install, or by hand), and where
  people open it (a domain, Tailscale, only that machine);
- what you did, what you expected, and what happened, with the exact message
  Kaal showed.

**Issues are public.** Never put anything from your vault in one: no notes,
receipts, screenshots of them, tokens, setup links, recovery codes or the
key. Describe the problem with made-up examples.

**A security problem** should not go in a public issue. Use **Report a
vulnerability** under the repository's **Security** tab, which only the
maintainers can read.

If someone else runs your Kaal server, ask them first about anything to do
with your account or your data: only they can see it or change it.

## Installing

[Installing Kaal](install.md) covers the one-command Docker install, the key
(keep a copy of it somewhere off the machine), updates, backups and removing
Kaal. Kaal for iPhone needs a Kaal server version 0.25.0 or newer.

## Common problems

### Signing in

- **Forgotten password:** use one of the recovery codes you were given when
  you chose it (the sign-in page offers "Forgot your password?"). With no
  codes left, ask whoever runs your Kaal's server for a new setup link
  (`kaal-server account setup-link NAME`, or `deploy/docker/kaalctl setup-link
  NAME`). An administrator cannot make one in the web app for an account that
  is already in use.
- **"Too many wrong tries":** after five wrong passwords for one sign-in
  name, each further try has to wait, from one minute up to fifteen.
- **"Too many tries from here. Wait a minute and try again.":** the sign-in
  pages take 30 requests a minute from one address. If everyone at your Kaal
  sees this at once, the server is behind a proxy it has not been told to
  trust, and takes everyone for one visitor: set `KAAL_TRUSTED_PROXIES`
  ([Installing Kaal](install.md), "Settings"; the Docker install sets it by
  itself).
- **Making a token asks for your password** (or, if you sign in only through
  Google, your sign-in name). That is on purpose: a token can do what you can.
- **"Enter your password to allow this"** (or "Type your sign-in name to
  allow this") when you let the iPhone app or the Chrome extension in: that
  browser was signed in more than ten minutes ago, and what you are allowing
  lasts longer than its sign-in does, so Kaal asks who you are again.
- **"You have made a lot of tokens in the last hour"** or **"You have
  connected a lot of apps in the last hour":** a person can make 20 tokens an
  hour, counting each time the app or the extension is let in. Wait and try
  again. Fifty tokens at once is the most a person can hold; revoke ones you
  no longer use.
- **Signed out unexpectedly:** a browser stays signed in for 30 days and the
  iPhone app for a year, unless the session is signed out under Settings, the
  password is changed, or a recovery code is used.

### Notes and receipts

- **Where are my receipts?** Kaal opens on your notes. Medical receipts are
  under **Receipts** at the top, which appears once the open vault holds a
  receipt. The number beside it is how many wait to be checked.
- **There is no Receipts tab:** the vault that is open has no medical receipts
  yet. Add the first one from the arrow beside **New note** (Medical receipt),
  with "Save receipt" in Kaal for Chrome, or from the Add tab on the iPhone.
  If you belong to more than one vault, the receipts may be in another one:
  pick it at the top right.
- **Where did Totals go?** Into Receipts: its three views are To review,
  Reviewed and Totals.
- **Adding files without writing anything:** the arrow beside **New note**,
  then "Files as a note". The note opens with the files in it, named after the
  first; save it as it is or write around them.
- **Bringing notes over from Google Keep:** at takeout.google.com, press
  "Deselect all", tick only Keep and create the export. Download the .zip
  Google emails you, then in Kaal open **Settings**, find **Import notes** and
  press "Choose the .zip" beside Google Keep. Text, checklists, labels (as tags), pictures and
  Keep's dates come across; notes in Keep's Trash do not, and archived notes
  get the tag `archived`. Importing the same or a newer export again adds only
  notes that are new. A very large export may be over the server's upload
  limit; whoever runs Kaal can import it on the server with
  `kaal import-keep takeout.zip`.
- **A search on Notes does not find a receipt:** receipts are searched on
  their own screen, by provider, person or title. While you search the notes,
  a link under the search box takes what you typed to Receipts; there, both
  lists (To review and Reviewed) show how many receipts match.
- **A trashed receipt is not in the notes' Trash:** receipts have a Trash of
  their own, linked from the notes' Trash and from the bottom of Receipts.
- **Family and Provider names are missing from Settings:** they only matter
  to medical receipts, and are shown once the open vault holds one.

### Sharing a vault

- **Someone is not in the list**, or **"Nobody here has that sign-in name"**:
  they have no account on this Kaal yet. An administrator who owns the vault
  invites them under Settings → Vaults and sharing → Invite someone new, and
  sends them the link; otherwise ask whoever runs your Kaal to add them, then
  let them in.
- **Seeing someone else's vault:** they share it with you, from their own
  Settings → Vaults and sharing. Being the administrator does not let you
  into anyone's vault.
- **Working in a vault someone shared with you:** once you belong to more
  than one vault, the web app shows the open one at the top right of every
  page. Pick another there to work in it. What you may do there follows your
  role: a viewer reads, an editor also adds, changes and deletes notes, and
  only an owner shares, renames, exports or deletes the vault.
- **"This is in “…”"** after following a link: the note is in another of
  your vaults than the one open in this browser. Press the button under it to
  open that vault.
- **Which name is my sign-in name?** Settings → Your account shows it in
  quotes after "Signed in as", however you signed in.
- **"No account here is connected to that sign-in"** after pressing Continue
  with Google: whoever runs this Kaal has to invite you first. Open the link
  they send you in the same browser and press Continue with Google there.

### Kaal for iPhone

- **"That doesn't look like an address":** type just the address you open
  Kaal at in a browser, such as `kaal.example.org`, with nothing after it.
- **"There is no Kaal at that address"** or **"Can't reach Kaal":** open the
  same address in Safari on the phone. If that fails too, the phone is not on
  a network that can reach your Kaal (for Tailscale, turn it on in the
  Tailscale app).
- **"That Kaal is version …":** the server is older than the app needs. Update
  it (`deploy/docker/kaalctl update`).
- **"You were signed out":** sign in again. Anything you added meanwhile is
  kept on the phone and sent once you are signed in.
- **Something you added has not arrived:** it waits on the Add screen until
  Kaal can be reached, and is sent by itself after that. If Kaal refused it,
  the Add screen says "Kaal did not take …" and why.
- **"Dictation isn't available right now":** the iPhone cannot recognise
  speech at the moment (often no connection, on a phone that cannot do it by
  itself). The keyboard's microphone still works.
- **"Kaal needs the microphone and speech recognition":** allow both in the
  iPhone's Settings, under Apps → Kaal.

### Kaal for Chrome

- **"Kaal isn't connected yet"** or **"didn't accept this extension's
  token":** open the extension's Settings, enter Kaal's address and press
  "Connect with my Kaal sign-in", sign in on the page that opens and press
  Allow (if you were already signed in there for a while, Kaal asks for your
  password again first). Or paste a token made in the web app under
  Settings → Tokens.
- **Saving into another of your vaults:** when you belong to more than one,
  the popup says "Saving into" above its button, with the list of your
  vaults. The choice is kept until you change it, and the popup says where
  each save went. A vault you may only view is listed but cannot be chosen.
  If the popup instead says "To choose another of your vaults here, connect
  again in the settings", the extension was connected before it could do
  this, or with a pasted token, which works in one vault: press "Connect with
  my Kaal sign-in" once more. This needs a Kaal server newer than 1.0.0-rc3.
- **"Kaal wouldn't do that in the vault you chose":** you may only view that
  vault, or you no longer belong to it. Nothing was saved anywhere else.
  Choose another vault in the popup and save again.
- **"Can't reach Kaal":** this computer is not on a network that can reach
  your Kaal server; or the server does not allow this copy of the extension.
  A Kaal server answers only the extensions it knows: whoever runs it lists
  their ids in `KAAL_EXTENSION_IDS` (the id is on the extension's card at
  `chrome://extensions`).
- **"This Kaal does not know that extension"** when connecting: the same
  cause, and the same cure. A server older than 1.0.0-rc2 does not know the
  copy from the Chrome Web Store: update it.
- **"Connecting was cancelled, or Kaal's page could not be opened":** the
  sign-in window was closed before Allow was pressed, or the address is
  wrong. The button needs a Kaal server version 0.25.0 or newer; with an
  older one, update the server or paste a token.
- **"Chrome wouldn't let Kaal read this page":** the developer tools are open
  on that tab, or it is one of Chrome's own pages.
- **"This PDF can't be fetched a second time"**: to keep a file, Kaal asks
  its site for it again, and this site answered with a web page instead. That
  happens when a PDF is made for one viewing, such as a statement shown after
  you press a button. Press **Load it again and save**: Kaal loads the tab
  again and keeps the PDF as it arrives. The site is asked again, exactly as
  if you pressed Reload, so do not use it on a page that followed a payment
  or an order. Or download the PDF with the arrow at its top right, open the
  downloaded file in Chrome and save that.
- **A file on this computer** (a PDF opened from a folder, at a `file://`
  address) is saved as the file it is. A copy of the extension from the Chrome
  Web Store may not read files until you allow it: right-click the Kaal icon,
  choose Manage extension, and turn on "Allow access to file URLs". "Kaal
  hasn't been let into files on this computer yet" has a button under it that
  asks Chrome; press it and the file is saved. Or add the file in the web app.

### The server

- **"port is already allocated" or "address already in use":** something else
  on the machine uses port 80, 443 or 8765. Stop it, or install with the
  "behind a proxy" choice.
- **The HTTPS certificate is not issued:** the domain must already point at the
  machine, and ports 80 and 443 must be reachable from the internet.
- **Kaal says its key is missing:** put back the copy you saved with
  `deploy/docker/kaalctl key-restore`. Without the key nothing in Kaal can be
  read, by anyone; Kaal never makes a new key over existing data.

### Leaving

- **Taking everything with you:** Settings → Vaults and sharing → Download a
  copy, in the web app, gives an owner of a vault a zip of it that needs no
  Kaal to read.
- **Deleting your account or a vault:** Settings → Delete my account, in
  the web app or the iPhone app, and Settings → Vaults and sharing → Delete
  this vault, in the web app. The [privacy policy](privacy.md) says exactly
  what goes and what stays.

More answers are in [Installing Kaal](install.md). How Kaal treats your data
is in the [privacy policy](privacy.md).
