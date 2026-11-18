# Notes for App Review

A template for App Store Connect → the version → App Review Information. Fill
in the parts in CAPITALS, delete the share-extension section if the build has
no share extension, and paste the text from "What Kaal is" down into
**Notes**. The sign-in name and password also go in the **Sign-in required**
fields above it.

## Before submitting: the demo server

The app does nothing until it is signed in to a Kaal server, so App Review
needs one it can reach from the internet, over HTTPS, holding nothing real.

1. On a machine with Docker and a domain name pointing at it (for example
   `demo.example.org`; any small server will do), install Kaal:

   ```bash
   deploy/docker/install.sh --domain DEMO-ADDRESS
   ```

   Open the link it prints and choose a password for yourself, the
   administrator. Keep sign-up closed (the default), so the sign-in page
   offers no way to create an account.

2. Make the account App Review signs in with, and choose its password from
   the link this prints:

   ```bash
   deploy/docker/kaalctl run kaal-server account add appreview --name "App Review"
   deploy/docker/kaalctl setup-link appreview
   ```

   Save the recovery codes it shows somewhere; nobody needs them.

3. Fill its vault with the made-up notes and receipts the screenshots show.
   Sign in to the web app as `appreview`, make a token under Settings →
   Tokens with read, reveal and write, save it to a file, and on any computer
   with this repository and Python with Pillow:

   ```bash
   bin/demo-data.py fill --url https://DEMO-ADDRESS --token-file appreview.token
   ```

   Then revoke the token under Settings → Tokens, and delete the file.

4. Sign in on an iPhone yourself with the address, `appreview` and its
   password, and go through the steps below once.

Keep the server running until the review is finished, and for later versions
too. Nothing in it is real: every name, receipt and address is made up and
marked SAMPLE.

## What Kaal is

Kaal is free, open-source software for keeping notes, documents and medical
receipts on a server that the person, or someone they trust, runs (like a
self-hosted Nextcloud or Home Assistant). The iPhone app is a client for such
a server: it signs in to the server the person names and shows what is in
their vault there. It cannot be used without a Kaal server, and has no
content of its own.

We run a demo server with made-up data for you:

- Address: DEMO-ADDRESS
- Sign-in name: appreview
- Password: DEMO-PASSWORD

Nothing is sent to us: the app talks only to the server whose address is
typed in. There are no accounts with us, no purchases, no ads and no
analytics.

## How to sign in

1. Open the app and type `DEMO-ADDRESS` into "Address of your Kaal". Tap
   Sign in.
2. iOS asks whether "Kaal" may use the address to sign in. Tap Continue. The
   server's own sign-in page opens in Apple's sign-in sheet.
3. Type the sign-in name and password above, tap Sign in, then tap Allow to
   let the app in. The sheet closes and the app opens on the Add screen.

Accounts are made only by the person who runs a Kaal server, by invitation;
the demo server offers no way to create one, so neither does the app.

## What to try

- **Write a note:** Add → Write a note. Type a few lines and tap Save. It
  appears under Notes; the first line is its title.
- **Dictation:** Add → Speak a note. iOS asks for the microphone and for
  speech recognition; allow both and speak. The words appear as you talk.
  Where the iPhone recognises speech by itself, the audio never leaves it.
- **Camera:** Add → Scan a receipt (or Scan a note). iOS asks for the camera.
  Point it at any printed page; the scanner finds the edges, and the page is
  sent to the server as a receipt (or a note). Photos and files can be added
  the same way with "Receipt from a file" and "Files as a note"; those use
  Apple's pickers and need no permission.
- **Receipts:** the Receipts tab lists receipts waiting to be checked. Open
  one to see each value beside a picture of the receipt, then tap "Looks
  right". Reviewed receipts can be marked reimbursed.
- **Totals:** what was paid per person and per year, and what is still to
  claim.
- **Notes:** search for `boiler` or `October`; the search also finds words in
  the PDFs attached to notes. Open "Paint colours for the hall" to see a note
  with a picture in it.
- **Settings:** the account, the vault and who shares it, signed-in browsers
  and apps, and "Sign out of this app".

## The share extension

DELETE THIS SECTION IF THE BUILD HAS NO SHARE EXTENSION.

In Safari, Files or Photos, tap the share button and choose Kaal. Choose
whether to save it as a note or as a receipt, and tap Save. It reaches the
demo server like anything added in the app, and shows under Notes or
Receipts.

## Permissions

- **Camera:** only when scanning a receipt or a page the person chose to
  save.
- **Microphone and speech recognition:** only while dictating a note.
- No location, contacts, photo library access, tracking or notifications.

## Contact

CONTACT NAME, CONTACT EMAIL, CONTACT PHONE (App Store Connect asks for these
in their own fields).
