# Notes for App Review

A template for App Store Connect → the version → App Review Information. Fill
in the parts in CAPITALS, and paste the
text from "What Kaal is" down into **Notes**. The sign-in name and password
also go in the **Sign-in required** fields above it.

## Before submitting: the demo server

The app does nothing until it is signed in to a Kaal server, so App Review
needs one it can reach from the internet, over HTTPS, holding nothing real.

1. On a machine with Docker and a domain name pointing at it (for example
   `demo.example.org`; any small server will do), start the demo:

   ```bash
   KAAL_DEMO_PASSWORD=CHOOSE-ONE deploy/demo/run.sh --domain DEMO-ADDRESS
   ```

   That is the whole installation ([`deploy/demo/README.md`](../../deploy/demo/README.md)).
   It builds Kaal, sets up HTTPS for the domain, makes the account App Review
   signs in with (`reviewer`, shown as Jane Madeup, not an administrator) with
   the password you chose, and fills its vault with made-up notes and
   receipts. Sign-up is closed, so the sign-in page offers no way to create
   an account.

2. Nothing else needs setting up. Every night the demo goes back to the
   made-up data, and within a minute it puts back a changed password or a
   deleted account, so what one reviewer does is gone before the next looks.

3. The screenshots in `ios/AppStore/screenshots` were taken from the same
   kind of made-up data (`bin/demo-data.py`, which `make-screenshots.sh`
   runs); the demo's own notes and receipts differ in detail.

4. Sign in on an iPhone yourself, with the build you are about to upload, the
   address, `reviewer` and its password, and go through the steps below
   once. In particular the app must still be signed in after the sign-in
   sheet closes, and after it is closed and opened again. A server reached
   over https takes an app's session only in the `X-Kaal-Session` header: a
   build that sends it as a cookie instead (look for `Cookie` in the app's
   `API.swift`) is told "You were signed out" straight after signing in, and
   must not be uploaded.

Keep the server running until the review is finished, and for later versions
too. Nothing in it is real: every name, receipt and address is made up.

Three things to know about that server:

- The reviewer can change and delete what is in it, and can delete the
  `reviewer` account itself (see "Deleting the account" below). The demo puts
  the account back within a minute and everything else every night
  (`deploy/demo/run.sh reset` does it at once). Before each submission, sign
  in as `reviewer` and look.
- The sign-in pages take 30 requests a minute from one address
  (`KAAL_SIGN_IN_PER_MINUTE`), and five wrong passwords for a name slow
  further tries down. A reviewer signing in by hand never comes near either.
- The same server serves the Chrome extension's review; it needs the store's
  extension id allowed (`chrome-extension/store/listing.md`).

## What Kaal is

Kaal is free, open-source software for keeping notes, documents and medical
receipts on a server that the person, or someone they trust, runs (like a
self-hosted Nextcloud or Home Assistant). The iPhone app is a client for such
a server: it signs in to the server the person names and shows what is in
their vault there. It cannot be used without a Kaal server, and has no
content of its own.

We run a demo server with made-up data for you:

- Address: DEMO-ADDRESS
- Sign-in name: reviewer
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

The sign-in sheet shares Safari's cookies, as Apple's sheet does by default.
If you sign in a second time (after "Sign out of this app", say), the sheet
shows only Allow, because Safari is still signed in to the demo server; when
that was more than ten minutes ago, the page asks for the password once more
before Allow.

## Accounts

Nobody can sign up in the app: it has no sign-up screen, and it signs in only
to an account that already exists on the server it is pointed at. Accounts on
a Kaal server are made by whoever runs it. The one place the app makes an
account is for such an administrator: signed in as the administrator of
their own server, Settings → People lets them add a person (who then chooses
a password from a link the administrator sends them), and open or close
sign-up on that server's own web page. The `reviewer` account is not an
administrator, so those screens are not shown to it; an administrator sees
the same Settings with two more rows, People and Sign-in services.

## Deleting the account

Settings → Delete my account, at the bottom of the account's settings. "What
would be deleted?" lists it first (the account, its tokens, and every vault
only that person belongs to, with everything in it). It then asks for the
password and, after one more question, deletes the account on the server for
good and returns the app to its first screen. The server's web app offers the
same under Settings → Delete my account.

Please do not finish that step on the demo account: "What would be deleted?"
and the password field are enough to see it, and the account is the one the
next reviewer signs in with. (If it is deleted, we make it again.)

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
- **Notes:** search for `boiler` or `radiators`; the search also finds words
  in the PDFs attached to notes. Open "Made-up garden plan" to see a note with
  a picture in it, and "Made-up house notes" for one with a PDF.
- **Settings:** the account, the vault and who shares it, signed-in browsers
  and apps, "Delete my account" and "Sign out of this app".
- **Without a connection:** turn on Airplane Mode and write a note. It waits
  on the Add screen, and is sent when the connection is back.

## The share extension

Sign in to the app first. Then, in Safari, Files or Photos, tap the share
button and choose **Kaal** (under "More" the first time, as for any
app). The sheet shows what is being saved; choose Note or Medical receipt
(a receipt is offered for pictures and PDFs; several pictures become one
receipt of several pages), give it a title if you like, and tap Save. It
reaches the demo server like anything added in the app, and shows under
Notes or Receipts. Before the app has been signed in, the sheet says to open
Kaal and sign in first, and saves nothing.

## Permissions

- **Camera:** only when scanning a receipt or a page the person chose to
  save.
- **Microphone and speech recognition:** only while dictating a note.
- No location, contacts, photo library access, tracking or notifications.

## Contact

CONTACT NAME, CONTACT EMAIL, CONTACT PHONE (App Store Connect asks for these
in their own fields).
