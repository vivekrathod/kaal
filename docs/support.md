# Getting help

Kaal is free software made by volunteers. There is no help desk, but
questions and problem reports are welcome on GitHub.

## Asking a question or reporting a problem

Open an issue on the project's GitHub repository, under **Issues**. Say:

- which part you are using: the server, the web app, Kaal for iPhone or Kaal
  for Chrome;
- the versions: the web app shows Kaal's version at the bottom of Settings,
  the iPhone app shows both its own and the server's under Settings, and
  `deploy/docker/kaalctl status` prints the server's;
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
Kaal. Kaal for iPhone needs a Kaal server version 0.24.0 or newer.

## Common problems

### Signing in

- **Forgotten password:** use one of the recovery codes you were given when
  you chose it (the sign-in page offers "Forgot your password?"), or ask
  whoever runs your Kaal for a new setup link.
- **"Too many wrong tries":** after five wrong passwords for one sign-in
  name, each further try has to wait, from one minute up to fifteen.
- **Signed out unexpectedly:** a browser stays signed in for 30 days and the
  iPhone app for a year, unless the session is signed out under Settings, the
  password is changed, or a recovery code is used.

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
  token":** open the extension's Settings and enter Kaal's address and a
  current token. Make the token in the web app under Settings → Tokens.
- **"Can't reach Kaal":** this computer is not on a network that can reach
  your Kaal server.
- **"Chrome wouldn't let Kaal read this page":** the developer tools are open
  on that tab, or it is one of Chrome's own pages.
- **A file on this computer** (`file://`): turn on "Allow access to file URLs"
  on the extension's card at `chrome://extensions`, or add the file in the web
  app.

### The server

- **"port is already allocated" or "address already in use":** something else
  on the machine uses port 80, 443 or 8765. Stop it, or install with the
  "behind a proxy" choice.
- **The HTTPS certificate is not issued:** the domain must already point at the
  machine, and ports 80 and 443 must be reachable from the internet.
- **Kaal says its key is missing:** put back the copy you saved with
  `deploy/docker/kaalctl key-restore`. Without the key nothing in Kaal can be
  read, by anyone; Kaal never makes a new key over existing data.

More answers are in [Installing Kaal](install.md). How Kaal treats your data
is in the [privacy policy](privacy.md).
