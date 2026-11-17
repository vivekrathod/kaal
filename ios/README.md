# Kaal for iPhone

A native app for a Kaal server you or someone you know runs. It does what the
web app does (notes, medical receipts with review and reimbursement, totals,
Trash and settings) and adds what a phone is good at:

- **Write a note** in one tap; the first line becomes its title.
- **Speak a note.** Speech becomes text as you talk. Where the iPhone can
  recognise speech by itself, the audio never leaves it.
- **Scan a note or a receipt** with the camera's page scanner, which finds the
  edges of the paper and takes several pages in one go. A receipt of several
  pages is sent as one PDF.
- **Works without a connection.** What you add from the Add screen is kept on
  the phone and sent when Kaal can be reached. It is left out of backups of
  the phone, and removed once Kaal has it.

## Signing in

The app asks for the address of your Kaal and opens its sign-in page in a
browser sheet. Sign in there as you usually do (password, Google) and press
Allow. The app is then signed in as you; it appears under Settings, with your
signed-in browsers, as "Kaal app on iPhone", and can be signed out there. The
server needs Kaal 0.24.0 or newer. How the hand-over is protected is described
in the [main README](../README.md).

The app keeps one secret, the session it was given, in the iPhone's keychain
(this device only). It never sees your password.

## Building it

You need a Mac with a recent Xcode: it was built and tested with Xcode 27, and the
code needs the iOS 18 SDK or newer. There are no other dependencies.

```bash
open ios/Kaal.xcodeproj
```

Choose an iPhone simulator and press Run. The simulator has no camera, so
"Scan" offers the photo library instead.

To put it on your own iPhone, create `ios/Local.xcconfig` (it is not
committed) with your Apple developer team and an identifier of your own:

```
DEVELOPMENT_TEAM = ABCDE12345
PRODUCT_BUNDLE_IDENTIFIER = com.example.yourname.Kaal
```

Add your Apple ID in Xcode under Settings → Accounts, plug the phone in, turn
on Developer Mode on it (Settings → Privacy & Security), choose it as the
destination and press Run. With a free Apple ID the app has to be installed
again from the Mac every seven days; with a paid developer account it lasts a
year.

[`install-on-iphone.sh`](install-on-iphone.sh) does the build and the install
in one step once the Apple ID and the phone are set up, and is what to run
again each week with a free Apple ID:

```bash
ios/install-on-iphone.sh
```

## Tests

The unit tests cover the API client, signing in, the offline queue, capture
and the Markdown reader:

```bash
xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalTests \
  -destination 'platform=iOS Simulator,name=iPhone 17'
```

`KaalUITests` drives the real app screen by screen: signing in on Kaal's own
page, writing, editing and trashing a note, a note's Markdown and files,
checking a receipt and marking it reimbursed, totals, every Settings page,
adding a receipt and a note from a photo, dictation, and signing out. It needs
a Kaal that holds only made-up data (never a real vault), with a person who
has a password, at least one receipt waiting for review with an amount of
$45.00, and a note called "Made-up house notes":

```bash
TEST_RUNNER_KAAL_UI_SERVER=http://localhost:8765 \
TEST_RUNNER_KAAL_UI_LOGIN=someone TEST_RUNNER_KAAL_UI_PASSWORD=... \
xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17'
```

Without `TEST_RUNNER_KAAL_UI_SERVER` those tests are skipped. In the simulator
speech recognition is not available, so the dictation test only checks that
the app says so; dictation and the camera scanner need a real iPhone to try.

## How it is put together

- `Kaal/Core/` has no screens: the API client (`API.swift`, `Endpoints.swift`),
  signing in (`AppModel.swift`), the offline queue (`Outbox.swift`), dictation,
  capture from camera, photos and files, and the Markdown reader.
- `Kaal/Views/` has one file per screen.
- Answers from the server are read by field name (`JSON.swift`), as the web
  app reads them, so a server that gains a field does not break the app.
- Files open in the app's own viewer (pictures and PDFs with zoom, text as
  text); any other kind is offered through the share sheet, as the web app
  only downloads it. The copy fetched for viewing is deleted when it closes.
- A note's Markdown is shown natively. Nothing in a note is run or loaded from
  elsewhere: links go only to the web, to mail addresses or to the note's own
  files.
