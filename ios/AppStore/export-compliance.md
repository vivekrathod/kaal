# Export compliance (encryption)

App Store Connect asks every build whether the app uses encryption. For Kaal
for iPhone the answer is already given in the app itself:
`ios/Info.plist` sets `ITSAppUsesNonExemptEncryption` to `false`, so uploads
go through without the question and no export documents are needed.

## Why `false` is right

`ITSAppUsesNonExemptEncryption = false` says the app uses no encryption, or
only encryption that is exempt from US export rules. Apple names the common
exempt case in its guide to encryption export rules: encryption that is part
of the operating system, such as HTTPS through `URLSession`.

Everything the app does that involves cryptography, checked in
`ios/Kaal/` (search for `CryptoKit`, `Security`, `URLSession` and
`FileProtection`):

| What | How | Exempt because |
|---|---|---|
| Talking to the Kaal server | `URLSession` over HTTPS (`API.swift`); App Transport Security applies, with plain http allowed only on the local network (`NSAllowsLocalNetworking`) | Encryption built into iOS |
| Keeping the session | The iPhone's keychain (`Keychain.swift`) | Built into iOS |
| Files waiting to be sent, and a file being viewed | iOS Data Protection (`.completeFileProtection…` when writing) | Built into iOS |
| Signing in | `SHA256` from CryptoKit turns a random secret into the challenge sent with the sign-in (PKCE, `AppModel.swift`); `SecRandomCopyBytes` makes the secret | A hash used to prove who is signing in: authentication, not encryption of data |
| Signing in on the server's page | Apple's sign-in sheet (`ASWebAuthenticationSession`), which is Safari | Built into iOS |

The app contains no encryption code of its own: no cipher, no library
bundled for it. The encryption of the vault (AES-256-GCM) happens on the Kaal
server, not in the app.

So in App Store Connect, if it is ever asked by hand: *Does your app use
encryption?* Yes. *Does it qualify for any of the exemptions?* Yes: it uses
only encryption within Apple's operating system, and a hash for
authentication. App Store Connect then asks for no documents. (If it asks
about distributing in France, the same facts apply; answer from them.)

## When to look again

Change the answer, and expect to provide documents, if the app ever:

- bundles a cryptography library, or uses CryptoKit, CommonCrypto or
  `SecKey` to encrypt or decrypt data itself (for example to encrypt notes on
  the phone before sending them);
- talks to anything over a protocol of its own that is encrypted outside
  `URLSession` and the system's TLS.

Adding a share extension or a widget that also uses only `URLSession`, the
keychain and Data Protection changes nothing, but each new target has an
Info.plist of its own: `tests/test_store_material.py` fails if any Swift file
under `ios/` starts using a cipher.

## The server

The App Store question is about the app. Kaal's server software, which does
use AES-256-GCM through the Python `cryptography` package, is published as
source code on GitHub and is not distributed through the App Store. This is a
summary of what the code does, not legal advice; whoever publishes the app is
the one answering Apple's questions.
