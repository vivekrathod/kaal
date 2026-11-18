# App Store listing

What to type into App Store Connect for Kaal for iPhone. Each field's limit is
in its heading, and `tests/test_store_material.py` checks that the text in
each block fits it.

## The name

### What the App Store already has (checked 18 November 2026)

Apple's public iTunes Search API was asked for `kaal` and for `kaal vault`
(`https://itunes.apple.com/search?term=…&entity=software&limit=50`: the
United States store, apps, up to 50 results each; 46 and 48 came back):

- **No app is called just "Kaal".** Three use it as the first word of a longer
  name: "Kaal : Divine Watch Faces" (Graphics & Design), "Kaal: Hindu Panchang
  Calendar" (Book) and "Kaal -Todo Focus & TaskPlanner" (Productivity, the
  same category as this app, released in September 2026). A news app is
  called "KAAL ABC 6 NEWS Minnesota Iowa", after a television station, and
  two calendar apps have the word inside their names ("RahuKaalApp",
  "Muhurta: Rahu Kaal & Muhurat").
- **Nothing is called "Kaal Vault"** or anything close. That search returns
  other vault apps (photo vaults, document vaults, calculator lockers), none
  of them named Kaal.
- **None of the names suggested below** appears in either list.

So the name is not taken outright, but it is crowded: "Kaal" is a common
Hindi and Sanskrit word for time, which is what most of those apps mean by
it.

App Store names are unique across every country and include names reserved by
apps that are not out yet, which the search cannot see, so "Kaal" on its own
may still be refused when the app is created in App Store Connect; that is
the only real test, and only the owner's account can make it. On its own it
would also be hard to find among those apps and the station. Use a longer
name; the name under the icon on the phone stays "Kaal"
(`CFBundleDisplayName`), whatever the store shows.

### App name (30 characters)

```text
Kaal: Notes & Receipts
```

If that is taken, in this order: `Kaal Vault`, `Kaal: Private Notes Vault`,
`Kaal Self-Hosted Notes`.

### Subtitle (30 characters)

```text
Your own server, your own data
```

## Promotional text (170 characters)

Can be changed at any time without a new version.

```text
Write, speak or scan a note, add medical receipts and check them, and see what is still to claim. It all goes to your own Kaal server, and nowhere else.
```

## Description (4000 characters)

```text
Kaal keeps your notes, documents and medical receipts on a server that you, or someone you trust, look after. This app is how you use your Kaal from your iPhone.

You need a Kaal server to use the app. Kaal is free, open-source software: install it with one command on a small rented server or a computer at home, then type its address here and sign in.

TAKE THINGS DOWN QUICKLY
• Write a note in one tap; the first line becomes its title.
• Speak a note and watch the words appear. Where your iPhone can recognise speech by itself, the audio never leaves it.
• Scan pages or receipts with the camera's page scanner, which finds the edges of the paper and takes several pages in one go.
• Add photos or files as a note, or as a receipt.
• No connection? What you add waits on the phone and is sent as soon as your Kaal can be reached.

MEDICAL RECEIPTS
• See at a glance which receipts still need checking.
• Check each value Kaal read (amount, dates, patient, provider, insurer) beside a picture of the receipt, with the line it was read from.
• Mark receipts reviewed, and reimbursed when you have claimed them, for example from an HSA or FSA.
• Totals per person and per year, and how much is still to claim.

NOTES
• Search titles, tags, the text of every note and the text read from its PDFs and scans.
• Notes are Markdown, shown formatted, with pictures in place and files you can open and zoom.
• Edit, add files, move to the Trash and restore.
• Save to Kaal from the share menu of Photos, Safari, Files and other apps.

YOURS, AND PRIVATE
• Everything is stored on your Kaal server, encrypted. Nothing goes to Kaal's authors: no analytics, no ads, no tracking.
• Sign in on your Kaal's own page, with your password, or with Google if your server offers it. The app is not shown your password when you sign in.
• Share a vault with your family as viewer, editor or owner, and see every browser and app that is signed in.
• Leave whenever you like: delete your account, with the vaults that are only yours, from the app's Settings.

Kaal is free and open source, under the Apache License 2.0.
```

## Keywords (100 characters)

Comma-separated, no spaces. The name and the category are searched already,
so they are not repeated here.

```text
self-hosted,receipts,medical,HSA,FSA,reimbursement,vault,encrypted,scanner,dictation,private
```

## Category

- **Primary:** Productivity (the same as `LSApplicationCategoryType` in the
  project).
- **Secondary:** Utilities.

Not Medical: the app keeps receipts, it gives no medical information or
advice, and Medical apps are reviewed as health services.

## Age rating

Answers to App Store Connect's age rating questions. The result is 4+.

| Question | Answer | Why |
|---|---|---|
| Parental controls | No | |
| Age assurance | No | |
| Unrestricted web access | No | The app opens only the sign-in page of the Kaal server the person names, in Apple's sign-in sheet. Links in notes open in Safari, outside the app. |
| User-generated content | No | Notes are private to the vault they are in. People share a vault only with people they invite, as with a shared folder; nothing is published or shown to strangers, and there is no way to contact anyone. |
| Messaging and chat | No | |
| Advertising | No | |
| Medical or treatment information | None | The app stores the person's own receipts and gives no medical information. |
| Health or wellness topics | No | |
| Every content question (violence, sexual content, profanity, drugs, horror, gambling, contests, and the rest) | None | |

## Price and availability

Free, with no in-app purchases, in every country. Kaal's server software is
free too, so there is nothing to sell.

## Addresses

| Field | Address |
|---|---|
| Support URL | `https://OWNER.github.io/REPO/support.html` |
| Marketing URL (optional) | `https://OWNER.github.io/REPO/` |
| Privacy Policy URL | `https://OWNER.github.io/REPO/privacy.html` |

`OWNER` is the GitHub account or organisation that holds the repository and
`REPO` is the repository's name, as in `https://github.com/OWNER/REPO`. These
pages are `docs/support.md`, `docs/index.md` and `docs/privacy.md`, published
by GitHub Pages: turn it on under the repository's Settings → Pages ("Deploy
from a branch", the default branch, folder `/docs`), wait for the first build,
and open each address in a browser before pasting it in. With a custom domain
set up for Pages, use that domain instead of `OWNER.github.io/REPO`.

## Copyright

```text
2026 OWNER
```

The name of whoever publishes the app (a person or an organisation), as it
appears on the developer account.

## Screenshots

The 6.9-inch iPhone display size, 1320 x 2868, portrait, in
[`screenshots/`](screenshots/); App Store Connect scales them down for smaller
iPhones. They show only made-up data. Upload them in this order:

1. `1-add.png`: the Add screen just after writing a note.
2. `2-receipts.png`: receipts waiting to be checked.
3. `3-review.png`: checking a receipt beside a picture of it.
4. `4-note.png`: a note with a picture in it.
5. `5-totals.png`: totals, and what is still to claim.
6. `6-search.png`: searching notes and the text read from their files.

To take them again after the screens change, run
`ios/AppStore/make-screenshots.sh` on a Mac with Xcode. It makes a simulator of
its own, starts a throwaway Kaal holding made-up notes and receipts
(`bin/demo-data.py`), signs the app in, takes the pictures, and deletes the
simulator and the throwaway Kaal again.

The project also builds for iPad (`TARGETED_DEVICE_FAMILY = "1,2"`). An app
offered on iPad needs 13-inch iPad screenshots (2064 x 2752) as well, which
the script does not take. Either set `TARGETED_DEVICE_FAMILY = 1` in the
project before uploading, so the app is offered on iPhone only, or take iPad
screenshots by hand on an iPad Pro 13-inch simulator.
