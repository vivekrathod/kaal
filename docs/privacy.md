# Privacy policy

Last changed: 17 November 2026.

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
- **Your account:** your sign-in name, the name shown for you, a hash of your
  password (Argon2id, never the password itself), hashes of your recovery
  codes, whether you are an administrator, and which vaults you belong to with
  what role.
- **Where you are signed in:** for each browser or app session, a hash of its
  secret, a short label such as "Chrome on macOS" or "Kaal app on iPhone"
  (worked out from the browser's description of itself, or given by the app;
  never your device's name), and when it started, was last used and runs out: 30 days for a
  browser, a year for the iPhone app. Kaal does not record IP addresses.
- **Tokens** for scripts and the Chrome extension: their names, what they may
  do, and a hash of each secret.
- **If you connect a Google account** (or another sign-in service the operator
  offers): that service's id for you and the email address it confirms. When
  such a sign-in creates a new account, the name the service gives becomes the
  account's display name.
- **Logs:** one line per request with the method, the kind of address (such as
  `/v1/notes/{id}`, never the actual one), the result, which token or account
  id made it, and how long it took. Never what was sent or received, search
  words, file contents, tokens or IP addresses. A proxy that the operator puts
  in front of Kaal may keep logs of its own; the one the Docker install sets up
  (Caddy) keeps none.
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

It keeps one cookie, `kaal_session`, which holds your session. Page scripts
cannot read it, it is sent only to your Kaal and only over HTTPS (unless the
operator is trying Kaal out on their own computer), and it lasts 30 days or
until you sign out. The only other thing it puts in your browser is a code
held while you sign an app in, removed once that is done.

## Kaal for iPhone

The app talks only to the Kaal address you type in. You sign in on your
Kaal's own page in Apple's sign-in sheet, so the app never sees your password.

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
Deleting the app removes everything it kept.

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
with the token you gave it, and to nothing else.

**What a save sends:** the page you are on as a PDF (printed by Chrome from
that tab), or the PDF or file you are saving, or the text you selected; with
it, the page's title and address. Kaal keeps the address without the part
after `?` or `#`, where sites often put session codes.

**What it keeps in Chrome:** your Kaal's address and token, and the names of
the sites you have saved receipts from (so it can offer "Save receipt" there),
in this Chrome profile on this computer; and, while a save is under way, its
progress, which Chrome forgets when it closes. Every ten minutes it asks your
Kaal how many receipts are waiting to be checked, for the number on its icon.
"Disconnect" in its settings removes the address and token; removing the
extension removes everything it kept.

**What Chrome asks you to allow, and why:**

- **Debugger**, the only way Chrome lets an extension make a true PDF of a
  page, and to fetch a PDF or linked file through the tab you are on, with
  your sign-in to that site. The extension attaches only to the tab you save
  from, only while saving, and Chrome shows a bar while it does. Chrome
  describes this permission in broad terms because a debugger could do more;
  this extension does nothing else with it.
- **Active tab**, to know which page you mean when you click the toolbar
  button, a right-click entry or the keyboard shortcut.
- **Side panel**, **context menus**, **storage** and **alarms**: the review
  screen beside the page, the two right-click entries, the settings above,
  and the ten-minute refresh of the number on the icon.
- **Access to one website**, only if you ask for it. The extension holds no
  access to any site. If a file cannot be read through the tab, it offers to
  ask Chrome for access to that one site; you can take it back on the
  extension's card at `chrome://extensions`.

The extension does not read your browsing history, runs nothing in the pages
you visit, and does not look at a page until you save it.

## Deleting your data

- **Notes and receipts:** move them to the Trash, then delete them forever
  (in the web app, the iPhone app or the command line; this needs the
  permission to delete, which an owner or editor of the vault has). Deleting
  forever removes Kaal's copies from the vault. Copies in the server's backups
  remain until those backups are replaced: 14 days with the Docker install.
- **Signed-in browsers and apps:** sign them out under Settings → Signed-in
  browsers and apps, in the web app or the iPhone app.
- **Tokens:** revoke them under Settings → Tokens.
- **A connected Google account:** disconnect it under Settings.
- **Your account:** Kaal cannot yet delete an account by itself. You can
  delete everything in your vault as above; the operator can switch your
  account off so that it can no longer be used, and can remove the whole
  installation. Ask the operator.
- **On your devices:** sign out of the iPhone app and delete it; choose
  "Disconnect" in the Chrome extension's settings or remove it.

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
