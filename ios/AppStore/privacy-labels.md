# App Store privacy answers

What to answer under App Store Connect → App Privacy for Kaal for iPhone, and
why. The answers follow from the code in `ios/Kaal/`, its share extension
`ios/KaalShare/` and what the two share in `ios/Shared/`, and match
[the privacy policy](../../docs/privacy.md) and the app's privacy manifest,
[`Kaal/PrivacyInfo.xcprivacy`](../Kaal/PrivacyInfo.xcprivacy).

## The answer: Data Not Collected

**"Do you or your third-party partners collect data from this app?"**
Answer **No, we do not collect data from this app.**

Apple counts data as collected when it leaves the device in a way that lets
the app's publisher, or a third party whose code is in the app, get at it for
longer than it takes to answer a request. Kaal for iPhone sends what a person
adds only to the Kaal server they choose by typing its address: a server they
run themselves, or one run by someone they trust (a family member, an
employer). The publisher does not run that server, receives nothing from the
app, and the app contains no one else's code: no analytics, advertising,
crash reporting or other SDKs. So nothing is collected in Apple's sense.

The App Store page then shows "Data Not Collected", and the privacy manifest
says the same (`NSPrivacyCollectedDataTypes` is empty, `NSPrivacyTracking` is
false).

**Keep this answer only while it stays true.** If whoever publishes the app
also runs a Kaal server that other people use (for example one with sign-up
open to anyone), the people using that server through the app have their data
collected by the publisher, and the answers below apply instead.

## What the app handles, and where it goes

| Data | Where it goes | Collected? |
|---|---|---|
| Notes, their text and files | The person's own Kaal server | No |
| Medical receipts and the values read from them (amounts, dates, patient, provider, insurer) | The person's own Kaal server | No |
| Photos and files the person picks, pages they scan | The person's own Kaal server | No |
| Speech while dictating | Turned into text on the iPhone where it can do that; otherwise by Apple's speech recognition service, as keyboard dictation is. Only the text goes into the note. | No: the audio never reaches the publisher, and Apple's processing is covered by Apple's own policy |
| Sign-in name and password | To sign in, typed into the server's own page in Apple's sign-in sheet, where the app does not see them. In Settings the app has fields of its own for the password, to change it, make new recovery codes or a token, or delete the account (and, for an account without a password, the sign-in name to confirm deleting it); what is typed there is sent only to that server, for that one request, and is not kept | No |
| The session the server gives the app | The iPhone's keychain, this device only, where the app's own share extension can read it too; sent back only to that server | No |
| The server's address, the account's name, vaults and role | Kept in the app's own settings on the phone (UserDefaults); the address also in the keychain, for the share extension | No |
| What is shared to "Save to Kaal" from another app | The person's own Kaal server; a copy is held on the phone only while the sheet is open | No |
| Device | The server shows the sign-in as "Kaal app on iPhone"; the device's name is never sent | No |

Nothing is used for tracking, nothing is shared with data brokers or
advertisers, and the app makes no request to any address but the one the
person typed in (and to Apple, for the system features above).

## If the publisher runs a Kaal for other people

Only then, declare these, each **linked to the user's identity**, **not used
for tracking**, with the purpose **App Functionality** only:

| App Store data type | Because |
|---|---|
| Health & Fitness → Health | Medical receipts |
| Financial Info → Other Financial Info | Amounts paid and reimbursed |
| Contact Info → Name | The name shown for the account |
| Contact Info → Email Address | When the sign-in name is an email address, or a Google account is connected |
| User Content → Photos or Videos | Photos and scans added to notes and receipts |
| User Content → Other User Content | Notes and files |
| Identifiers → User ID | The account's id on the server |

Audio is still not collected: only the text of a dictated note reaches the
server.

## Privacy manifest

`ios/Kaal/PrivacyInfo.xcprivacy` is included in the app (the `Kaal` folder is
part of the app target as a whole). It declares no tracking, no collected
data, and the one API on Apple's list of APIs that need a reason which the app
uses: UserDefaults, for its own settings (reason `CA92.1`). A new use of an
API on that list (file dates, free disk space, time since the phone started,
the keyboards in use) needs adding there before the next upload, or App Store
Connect refuses the build.

## Permissions the app asks for

Each prompt's text is set in the project (`INFOPLIST_KEY_…` build settings):

| Permission | When | The text iOS shows |
|---|---|---|
| Camera | The first time the person chooses Scan | Kaal uses the camera to scan receipts and pages of notes that you choose to save. |
| Microphone | The first time the person dictates | Kaal listens only while you dictate a note, to turn what you say into text. |
| Speech recognition | The first time the person dictates | Kaal turns what you say into the text of a note. Where this iPhone can do that by itself, the audio never leaves it. |

Photos and files come through Apple's own pickers, which need no permission:
the app sees only what the person picks.
