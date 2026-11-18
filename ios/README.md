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
- **Save from other apps.** Kaal is in the share menu of Photos, Safari,
  Files, Mail and other apps. It saves up to ten pictures, PDFs and other
  files, a web page or some text as a note, and pictures or a PDF as a medical
  receipt (several pictures become one PDF, a page each). A web page becomes a
  note with its title and a link to it. It shows what it is about to save, and
  you can give it a title. It needs a connection: if Kaal can't be reached, it
  says so and stays open so you can try again.

## Signing in

The app asks for the address of your Kaal and opens its sign-in page in a
browser sheet. Sign in there as you usually do (password, Google) and press
Allow. The app is then signed in as you; it appears under Settings, with your
signed-in browsers, as "Kaal app on iPhone", and can be signed out there. The
server needs Kaal 0.25.0 or newer. How the hand-over is protected is described
in the [main README](../README.md).

The app keeps one secret, the session it was given, in the iPhone's keychain
(this device only, and not in backups). It never sees your password.

"Save to Kaal" in other apps' share menus is the app's share extension, and
is signed in whenever the app is: the app keeps the session and the address
of your Kaal in a keychain group that only the two of them can read (Keychain
Sharing, named in [`Kaal.entitlements`](Kaal.entitlements) after the app's
own identifier). A phone signed in before the extension existed stays signed
in: the next time it is opened, the app puts what the extension needs there. Signing
out of the app signs the extension out too. Whenever the extension cannot read
a sign-in (the app is signed out, or the build has no such group) it says
"Open Kaal and sign in first", and the app goes on working by itself. A build
that is not signed at all (`CODE_SIGNING_ALLOWED=NO`, as in CI) is given no
keychain by the simulator, so it cannot stay signed in, with or without the
extension; it is good for building and for the unit tests only. A build from
Xcode for the simulator is signed to run locally, which is enough.

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

The share extension is built and installed with the app, under the app's
identifier with `.share` added (`com.example.yourname.Kaal.share`), so there
is nothing else to set. Xcode registers both identifiers for you. A free Apple
ID is enough: Keychain Sharing between an app and its own extension needs no
paid account, and the app uses no App Groups. A free Apple ID may register
only ten identifiers a week, and this takes two.

[`install-on-iphone.sh`](install-on-iphone.sh) does the build and the install
in one step once the Apple ID and the phone are set up, and is what to run
again each week with a free Apple ID:

```bash
ios/install-on-iphone.sh
```

## Tests

The unit tests cover the API client, signing in, where the sign-in is kept,
the offline queue, capture, the Markdown reader, and what "Save to Kaal" makes
of what another app shares (what each thing is taken to be, titles, the note's
text, the receipt's PDF, and the requests it sends):

```bash
xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalTests \
  -destination 'platform=iOS Simulator,name=iPhone 17'
```

`KaalUITests` drives the real app screen by screen: signing in on Kaal's own
page, writing, editing and trashing a note, a note's Markdown and files,
checking a receipt and marking it reimbursed, totals, every Settings page,
adding a receipt and a note from a photo, dictation, and signing out. It needs
a Kaal that holds only made-up data (never a real vault), with a person who
has a password and the notes and receipts `bin/demo-data.py` puts in (the
tests open "Boiler and radiators" and check a receipt of $64.50):

```bash
TEST_RUNNER_KAAL_UI_SERVER=http://localhost:8765 \
TEST_RUNNER_KAAL_UI_LOGIN=someone TEST_RUNNER_KAAL_UI_PASSWORD=... \
xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal -only-testing:KaalUITests \
  -destination 'platform=iOS Simulator,name=iPhone 17'
```

`bin/demo-data.py serve` starts such a Kaal, with the made-up notes and
receipts the tests look for, and prints its address, sign-in name and
password. Its account is an administrator, so the tests go through the People
and Sign-in services pages too.

They also save from other apps (`ShareScenarioTests.swift`): a web page shared
from Safari becomes a note, two of the simulator's sample photos shared from
Photos become one receipt of two pages, and once the app has signed out the
sheet says to sign in first. The page shared is the Kaal being tested, or the
one named in `TEST_RUNNER_KAAL_UI_PAGE`. They rely on the simulator's Photos
and Safari being in English. All of `KaalUITests` needs a build that is
signed, which Xcode's default for the simulator ("Sign to Run Locally") is:
with `CODE_SIGNING_ALLOWED=NO` the app has no keychain to keep its sign-in in.

Without `TEST_RUNNER_KAAL_UI_SERVER` those tests are skipped. In the simulator
speech recognition is not available, so the dictation test only checks that
the app says so; dictation and the camera scanner need a real iPhone to try.

Those tests look at what deleting the account would delete and stop there.
`LeavingScenarioTests` deletes one, from Settings → Delete my account, with a
wrong password first. It runs only when given a made-up account of its own to
delete, one that is not the server's only administrator
(`kaal-server account add leaver`), and that account is gone afterwards:

```bash
TEST_RUNNER_KAAL_UI_SERVER=http://localhost:8765 \
TEST_RUNNER_KAAL_UI_LEAVER_LOGIN=leaver TEST_RUNNER_KAAL_UI_LEAVER_PASSWORD=... \
xcodebuild test -project ios/Kaal.xcodeproj -scheme Kaal \
  -only-testing:KaalUITests/LeavingScenarioTests \
  -destination 'platform=iOS Simulator,name=iPhone 17'
```

## Publishing

[`AppStore/`](AppStore/listing.md) holds what App Store Connect asks for: the
listing, the privacy answers, notes for App Review, export compliance and the
screenshots. `AppStore/make-screenshots.sh` takes the screenshots again on a
simulator of its own, against a throwaway Kaal holding made-up data
(`bin/demo-data.py`). The privacy manifest is `Kaal/PrivacyInfo.xcprivacy`.

## How it is put together

- `Shared/` is the code the app and its share extension both use: the API
  client (`API.swift`, `Endpoints.swift`), the keychain, sending a note with
  its files (`Sending.swift`), making pictures and PDFs ready to send
  (`Pictures.swift`), the Markdown reader, and deciding what something shared
  from another app is and what note or receipt it becomes (`Sharing.swift`,
  `ShareReading.swift`). It has no screens.
- `Kaal/Core/` is the rest of the app without screens: signing in
  (`AppModel.swift`), the offline queue (`Outbox.swift`), dictation, and the
  camera, photo and file pickers.
- `Kaal/Views/` has one file per screen.
- `KaalShare/` is the share extension: one sheet, in SwiftUI. Safari runs
  `PageDetails.js` in the page being shared, which reads only its title and
  address. Pictures are made small as they are read and other files wait on
  disk, since iOS closes a share extension that uses much memory; it takes
  files of up to 25 MB.
- Answers from the server are read by field name (`JSON.swift`), as the web
  app reads them, so a server that gains a field does not break the app.
- Files open in the app's own viewer (pictures and PDFs with zoom, text as
  text); any other kind is offered through the share sheet, as the web app
  only downloads it. The copy fetched for viewing is deleted when it closes.
- A note's Markdown is shown natively. Nothing in a note is run or loaded from
  elsewhere: links go only to the web, to mail addresses or to the note's own
  files.
