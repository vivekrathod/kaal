# Privacy policy

Last changed: 18 November 2026.

This policy covers every part of Kaal: the Kaal server (`kaal-server`,
however it is installed), the web app it serves, Kaal for iPhone and Kaal for
Chrome.

## In short

- **Kaal is software, not a service.** It runs on a server that you, or
  someone you trust, set up and look after. Everything you put into Kaal goes
  to that server and is kept there.
- **Kaal's authors receive nothing.** The people who write Kaal and publish
  the iPhone app and the Chrome extension run no server that your data passes
  through, and have no way to see what you keep in Kaal or how you use it.
  (Apple and Google show publishers only overall figures, such as how many
  people installed the app, under their own stores' policies.)
- **No analytics, no advertising, no tracking.** No part of Kaal contains
  analytics, advertising, crash reporting or tracking code, or anyone else's
  code that sends information about you anywhere.
- **Two optional features involve another company**, and both are off until
  whoever runs the server turns them on: reading receipts with a language
  model, and signing in with Google. Both are described below.

## Who is responsible for your data

The person or organisation that runs the Kaal server you use (here called the
**operator**) decides where it runs, who has an account, which optional
features are on, and how backups are kept. The operator holds the key that
Kaal's encryption uses and can read every vault on their server. If you run
Kaal yourself, that is you. Questions about your data, or requests to see,
correct or remove it, go to the operator: Kaal's authors cannot see or change
anything on someone else's server.

## What the server keeps

- **What you save:** notes, files, medical receipts, the text read from files
  and receipts, and the values read from receipts (amounts, dates, people,
  providers). All of it is encrypted at rest with AES-256-GCM, including the
  index of titles, tags and file names.
- **Your account:** your sign-in name (which may be an email address, if that
  is what was chosen), the name shown for you, a hash of your password
  (Argon2id, never the password itself), hashes of your recovery codes,
  whether you are an administrator, and which vaults you belong to with what
  role. The list of accounts is a file beside the vaults; it is not encrypted
  as they are, which is why it holds secrets only as hashes.
- **Where you are signed in:** for each browser or app session, a hash of its
  secret, a short label such as "Chrome on macOS" or "Kaal app on iPhone"
  (worked out from the browser's description of itself, or given by the app;
  never your device's name), and when it started, was last used and runs out:
  30 days for a browser, a year for the iPhone app.
- **Tokens** for scripts and the Chrome extension: their names, what they may
  do, and a hash of each secret.
- **The address a request comes from** (its IP address) is never written
  down. The server holds it in memory for a short while, to slow down someone
  guessing passwords and to limit how often one address may call the pages
  that need no sign-in; a restart forgets it.
- **If you connect a Google account** (or another sign-in service the operator
  offers): that service's id for you and the email address it confirms. When
  such a sign-in creates a new account, the name the service gives becomes the
  account's display name.
- **Logs:** one line per request with the method, the kind of address (such as
  `/v1/notes/{id}`, never the actual one), the result, which token or account
  id made it, and how long it took. Never what was sent or received, search
  words, file contents, tokens or IP addresses. A proxy that the operator puts
  in front of Kaal may keep logs of its own. The one the Docker install sets up
  (Caddy) is set to keep no record of requests, failed ones included: it logs
  its own starting up and what happens with its HTTPS certificate, which names
  the domain. An installation made before that setting was added (November
  2026) logged each request that failed, such as one made while Kaal was
  restarting, with the visitor's address, the address asked for and the
  request's headers, until it is updated (`deploy/docker/kaalctl update`);
  Docker keeps at most 30 MB of those older lines and replaces them as new
  ones are written.
- **Backups:** the Docker install copies everything once a day and keeps the
  newest 14 copies, on the same machine. The encryption key is never part of a
  backup.

## What leaves the server

Nothing, unless the operator turns one of these on.

**Reading receipts with a model.** By default Kaal reads receipts with fixed
rules on the server. The operator can instead have a language model read each
medical receipt (`KAAL_RECEIPT_AI`). The receipt's text and pictures of its
first pages are then sent to the model they chose:

| Choice | Where the receipt goes |
|---|---|
| `ollama` | A model the operator runs on their own machine or network. Nothing leaves it. |
| `anthropic` or `claude-cli` | Anthropic, which makes Claude. |
| `openai` | The service at the address the operator gives: OpenAI, OpenRouter, or a program they run themselves. |

Only medical receipts are sent, one at a time as they arrive or when you ask
for one to be read again; notes and other files never are. A paid service
handles them under the operator's own account and its terms and data
settings. The model is given no tools and is asked only which value on the
receipt is which.

**Signing in with Google** (or another OpenID Connect service). When the
operator offers it and you choose it, your browser goes to Google's sign-in
page, and Google tells your Kaal who you are: an id, your email address and
your basic profile, of which Kaal uses only your name. Kaal asks for nothing
else, and gets no access to your mail, files or contacts. To finish the
sign-in, the server checks with Google and fetches Google's published signing
keys. Google learns that you signed in to this Kaal, as it does whenever you
sign in somewhere with Google.

**HTTPS certificates.** If the operator lets the Docker install set up HTTPS
for a domain name, it asks a certificate authority (Let's Encrypt or ZeroSSL)
for a certificate. That request carries the domain name only.

Kaal never checks for updates or contacts its authors. Installing and updating
it downloads software from where the operator chooses.

## The web app

The web app is served by your Kaal server and loads nothing from anywhere
else; the server tells the browser to refuse anything that is not its own.

It keeps one cookie, which holds your session: `__Host-kaal_session` over
HTTPS (`kaal_session` when the operator is trying Kaal out over plain http).
Page scripts cannot read it, it is sent only to your Kaal, never from another
site, and it lasts 30 days or until you sign out. Signing in with Google adds
a second cookie for the few minutes that trip takes. The only other thing the
web app puts in your browser is a code held, for that tab only, while you
sign the iPhone app or the Chrome extension in; it is removed once that is
done.

## Kaal for iPhone

The app talks only to the Kaal address you type in. You sign in on your
Kaal's own page in Apple's sign-in sheet, so the app does not see your
password when you sign in. It asks for your password only in Settings, when
you change it, make new recovery codes or a token, or delete your account; it
sends it to your Kaal for that one request and does not keep it.

**What it keeps on the phone:**

- The session your Kaal gave it, in the iPhone's keychain, for this device
  only.
- Your Kaal's address, and what the server last said about your account (your
  name, your vaults and your role), so the app can open without a connection.
- Anything you add while Kaal cannot be reached, until it has been delivered.
  It is left out of backups of the phone and deleted as soon as Kaal has it.
- A file you open, for as long as it is open. Pictures are kept in memory only
  while the app is running.

Signing out deletes the session and what the app knew about your account.
Anything still waiting to be sent stays on the phone (the app says so before
you sign out) and is sent when you next sign in. Deleting the app removes
everything it kept.

**Safari stays signed in.** Apple's sign-in sheet shares Safari's cookies, so
that someone already signed in to their Kaal in Safari need not type the
password again. Signing in there therefore also leaves Safari on the phone
signed in to your Kaal, for up to 30 days, listed with your other browsers as
"Safari on iPhone". Signing out of the app, or deleting it, does not end
that: sign it out under Settings → Signed-in browsers and apps, or open your
Kaal in Safari and sign out there.

**What it asks permission for, and why:**

- **Camera**, to scan a receipt or a page when you choose Scan. The pages
  become the note or receipt you are adding, and nothing else.
- **Microphone and Speech Recognition**, only while you dictate a note. Where
  the iPhone can recognise speech by itself, the audio never leaves the
  phone. Where it cannot, iOS sends the audio to Apple to be turned into text,
  as the keyboard's own dictation does, under
  [Apple's privacy policy](https://www.apple.com/legal/privacy/). Kaal never
  keeps or sends the audio; only the text goes into your note.
- **Photos and files** are chosen in Apple's own pickers. Kaal sees only the
  photos or files you pick, and needs no access to your photo library.
- If your Kaal is on your home network, iOS may ask whether Kaal may connect
  to devices on your local network. That is the connection to your Kaal.

## Kaal for Chrome

The extension sends what you save to the Kaal server named in its settings,
with a token for that server, and to nothing else.

**Connecting it.** "Connect with my Kaal sign-in" opens your Kaal's own
sign-in page in a window of its own. You sign in there as you usually do and
press Allow (if that browser was signed in more than ten minutes ago, your
Kaal asks for your password again first); your Kaal then makes a token for
the extension (to read, reveal
and write in the vault you had open, as far as your role there allows) and
hands it over. The extension never sees your password. You can paste a token
you made yourself instead. Either way the token is listed in the web app
under Settings → Tokens, where you can revoke it.

**What a save sends:** the page you are on as a PDF (printed by Chrome from
that tab), or the PDF or file you are saving, or the text you selected; with
it, the page's title and address. Kaal keeps the address without the part
after `?` or `#`, where sites often put session codes.

**What it keeps in Chrome:** your Kaal's address and token, and the names of
the sites that receipts in your vault were saved from, by you or by anyone
who shares the vault (so it can offer "Save receipt" there), in this Chrome
profile on this computer; and, while a save is under way, its progress, which
Chrome forgets when it closes. Every ten minutes it asks your Kaal how many
receipts are waiting to be checked, for the number on its icon, and for that
list of sites.
"Disconnect" in its settings removes the address and token; removing the
extension removes everything it kept.

**What Chrome asks you to allow, and why:**

- **Debugger**, the only way Chrome lets an extension make a true PDF of a
  page, and to fetch a PDF or linked file through the tab you are on, with
  your sign-in to that site. The extension attaches only to the tab you save
  from, only while saving, and Chrome shows a bar while it does. Through it,
  the extension asks the page what kind of document it is (one fixed
  expression, `document.contentType`, evaluated in that page), has Chrome
  print the page, or loads the file and reads it. Chrome describes this
  permission in broad terms ("Read and change all your data on all
  websites") because a debugger could do more; this extension does nothing
  else with it.
- **Active tab**, to know which page you mean when you click the toolbar
  button, a right-click entry or the keyboard shortcut.
- **Side panel**, **context menus**, **storage** and **alarms**: the review
  screen beside the page, the two right-click entries, the settings above,
  and the ten-minute refresh of the number on the icon.
- **Identity**, only for "Connect with my Kaal sign-in". Chrome's name for
  this permission covers two things; the extension uses one of them: opening
  a sign-in page (your Kaal's own, and no other) in a window of its own and
  receiving the single-use code your Kaal sends back. It does not ask Chrome
  who you are, and it gives the extension no access to a Google account, a
  Chrome profile, or your email address.
- **Access to one website**, only if you ask for it. The extension asks for
  no access to websites of its own when it is installed. If a file cannot be
  read through the tab, it offers to ask Chrome for access to that one site;
  you can take it back on the extension's card at `chrome://extensions`.

The extension does not read your browsing history, runs nothing in a page you
have not asked it to save, and does not look at a page until you save it.

## Deleting your data

Everything here is done in the web app, at the address of your Kaal, unless
it says otherwise. None of it needs the operator, or Kaal's authors.

- **Taking a copy first.** An owner of a vault can download everything in it
  under Settings → Vaults and sharing → Download a copy: a zip with each note
  as a Markdown file and its files beside it, each receipt's original file,
  and every receipt's values in a spreadsheet (`receipts.csv`). It is not
  encrypted and needs no Kaal to read, so keep it as safe as the vault.
- **Notes and receipts:** move them to the Trash, then delete them forever
  (in the web app, the iPhone app or the command line; this needs the
  permission to delete, which an owner or editor of the vault has). Deleting
  forever removes Kaal's copies from the vault.
- **A whole vault:** an owner deletes the open vault under Settings → Vaults
  and sharing → Delete this vault, confirming with their password (or, for an
  account that signs in only through Google, the vault's name). Every note,
  file and receipt in it is deleted for good, for everyone who shares it, and
  tokens made for it stop working. The first vault an installation was set
  up with cannot be deleted this way.
- **Your account:** Settings → Delete my account, in the web app or the
  iPhone app. "What would be deleted?" shows it before anything is asked:
  your sign-in, your tokens, your sessions, any connected Google account, and
  every vault that only you belong to, with everything in it. Vaults you
  share stay with the people you share them with; if you are the only owner
  of one, make someone else an owner first. You confirm with your password
  (or your sign-in name, if you have no password). Three things stop it, and
  Kaal says which: the only administrator of an installation cannot delete
  their account until someone else is an administrator too; a vault that is
  only yours cannot go if it is the first vault the installation was set up
  with, until you share it and make someone else its owner; and an
  installation's only account cannot be deleted at all: that person runs the
  server, and removes the whole installation instead
  ([Installing Kaal](install.md), "Removing Kaal"). An administrator can
  remove someone else's account under Settings → People, and whoever runs the
  server can with `kaal-server account remove`.
- **Signed-in browsers and apps:** sign them out under Settings ("Signed-in
  browsers" in the web app, "Signed-in browsers and apps" in the iPhone app).
- **Tokens:** revoke them under Settings → Tokens.
- **A connected Google account:** disconnect it under Settings.
- **On your devices:** sign out of the iPhone app and delete it, and sign
  Safari out of your Kaal if you signed the app in on that phone (see "Safari
  stays signed in" above); choose "Disconnect" in the Chrome extension's
  settings or remove it.
- **Backups.** What you delete stays in the copies the server's daily backup
  has already made until those are replaced: 14 days with the Docker install.
  Only the operator can delete those copies sooner.

Kaal for iPhone has "Delete my account" under Settings, which does what the
web app's does and then signs the app out; a vault is deleted, and a copy
downloaded, in the web app ("Open Kaal in the browser" in the app's
Settings). Kaal for Chrome has no such buttons of its own; open your Kaal's
address in a tab.

## Children

Kaal is not aimed at children. Its authors collect nothing about anyone,
children included.

## Changes

This policy is kept with Kaal's source code, so every change to it is
recorded there with its date. A change in what Kaal does with data will be
described here before it is released.

## Questions

For questions about Kaal itself, or this policy, open an issue on the
project's GitHub repository (see [Getting help](support.md)). For anything
about your own data, ask the operator of your Kaal server.
