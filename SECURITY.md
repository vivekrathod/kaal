# Security

## Reporting a problem

Please report a security problem privately, not in a public issue: on the
project's GitHub page choose **Security**, then **Report a vulnerability**.
Say what you found, how to reproduce it with made-up data, and which version
(`kaal-server --version`, or `/v1/health`). You will get an answer, and credit
in the release notes if you would like it.

Fixes go into the newest release. There are no long-lived older branches:
update to get them (`deploy/docker/kaalctl update`).

## What Kaal is meant to withstand

The README's [threat model](README.md#threat-model) says what encryption
protects, what stays readable, and what the server records. In short:

- **A copy of the stored files** (a stolen disk, a backup, a snapshot) reads
  as nothing without the key, which is never stored beside the data.
- **Strangers on the network** reach only the sign-in routes, which are
  limited per address, take small requests, and slow down password guessing
  without letting it lock the real person out.
- **Someone signed in** is held to the vaults they belong to and their role in
  each; a token is held to its scopes and to the role of whoever made it. An
  administrator manages people and settings but cannot read a vault they do
  not belong to, and cannot make themselves a way into an account in use.
- **A file someone saved** is read by pdftotext, pdftoppm and tesseract with a
  time limit, a memory limit and a bare environment, a few at a time.

## What it does not withstand

- **Whoever controls the server**, or the machine under it, can read
  everything: the key is in the server's memory while it runs. This is not
  end-to-end encryption, by design; the server reads receipts and searches
  notes for you.
- **A flaw in a file-reading program.** They run as the same user as the
  server. A crafted file that exploited one could reach the key. Keep the
  system or the container image updated.
- **A determined flood.** The limits keep one visitor from getting in the way
  of the rest. Absorbing a large attack belongs to the proxy or network in
  front.
- **A stolen device that is signed in**, until its session or token is ended
  (Settings, in the web app, or `kaal-server token revoke`).
- **The `read` scope as a privacy boundary for a note's wording.** It hides
  receipt values, identity numbers and full saved-from addresses, not the text
  of notes.
- **Finding out that a sign-in name exists.** Someone who owns a vault learns
  it by sharing with that name; this is limited to a few tries a minute.

## For people running Kaal

- Keep a copy of the key off the server, and backups somewhere else.
- Serve it over HTTPS only (the installer's choices all do).
- Leave sign-up closed unless you mean strangers to have accounts; if you open
  it, set `KAAL_VAULT_QUOTA_MB` and `KAAL_SIGNUPS_PER_HOUR` first.
- Give scripts tokens with only the scopes they need, and revoke the ones you
  no longer use.
