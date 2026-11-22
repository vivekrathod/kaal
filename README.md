# Kaal

Kaal is a private vault for notes, documents and medical receipts. It runs as a
small server on a machine you control; you reach it from a web app, a Chrome
extension and a command line. Notes are Markdown, and everything is encrypted
at rest.

- **Server:** `kaal-server`, which is the only process that opens the vault or reads the key.
- **Web app:** served by the server at `/`, for a laptop or a phone.
- **Command line:** `kaal`, on the server itself or pointed at it from another machine.
- **Chrome extension:** saves the page in front of you; install it from the
  [Chrome Web Store](https://chromewebstore.google.com/detail/kaal/jimjfomjjedakpipjfpagdmkecjiehll)
  ([`chrome-extension/`](chrome-extension/README.md)).
- **Encryption:** AES-256-GCM, with one key kept in a file outside the vault.

## Install

On a machine with Docker:

```bash
deploy/docker/install.sh
```

It asks where people will open Kaal (a domain of your own with HTTPS set up
for you, Tailscale or another proxy, or just this machine), starts Kaal, and
prints a link for choosing your password. [`docs/install.md`](docs/install.md)
covers the key, updates and backups.

To look around first, `deploy/demo/run.sh --local` runs a separate Kaal that
holds only made-up notes and receipts
([`deploy/demo/README.md`](deploy/demo/README.md)).

## Security model

- Every note, attachment, piece of extracted or OCR text and the index is encrypted at rest, medical receipts included.
- The index holds the metadata: titles, tags, timestamps, attachment names, sizes and hashes, and medical receipt fields. A copy of each attachment's metadata is kept beside it, also encrypted.
- The key is never stored in the vault or in a backup. It lives in the file named by `KAAL_KEY_FILE`, which Kaal refuses to read unless only its owner can read it. Losing it loses the vault, so keep a copy somewhere off the server.
- Attachments keep their original bytes; the text read from them is stored separately.
- A note or file carries a label, `sensitive` or `public`. It only records what whoever saved it said (`--sensitivity`); nothing is inferred from the text, and it does not change how anything is stored. Without a statement, content is labelled sensitive.
- File permissions are restrictive: vault directories `700`, stored files `600`.
- `kaal show` prints a note with identity numbers and similar values hidden; `--full` shows everything.
- A vault from an older version may hold notes, attachments, extracted text and the index readable. Kaal still reads them, and `kaal encrypt-all` (run on the server) encrypts them in place; `--dry-run` counts them first. It reads each item back before removing the readable copy and can be re-run after an interruption. Until it has been run, such a vault keeps its index readable so that the older version can still open it.

## Running Kaal as a server

`kaal-server` puts vaults behind an authenticated HTTP API, so the CLI, the
Chrome extension and agents on other machines share them. The server is the only
process that opens a vault or reads the key. Every endpoint runs the same code
as the local CLI, and the API is described in [`docs/openapi.json`](docs/openapi.json).

```bash
pip install '.[server]'
export KAAL_HOME=/srv/kaal KAAL_VAULT=/srv/kaal/data
export KAAL_KEY_FILE=/etc/kaal/master.key
kaal init                             # new vault and key, or `kaal import` an export
kaal-server token create mac-cli --scopes read,reveal,write,delete --output mac-cli.token
kaal-server serve                     # 127.0.0.1:8765; publish with `tailscale serve`
```

[`deploy/kaal-server.service`](deploy/kaal-server.service) is a hardened systemd
unit. With Docker, [`deploy/docker/install.sh`](deploy/docker/install.sh) does
all of this in one command (see [`docs/install.md`](docs/install.md)).

**Updating a server.** `deploy/deploy.sh --host USER@SERVER REF` tests and
builds one commit, backs the vault up on the server, installs it, restarts
`kaal-server` and checks its health. If the check fails it restores the version
that was running. `--rollback` restores the previous version by hand,
`--dry-run` shows what would happen, and `--status` shows what is installed. It
expects the layout of the systemd unit above and passwordless `sudo` on the
server.

**Tokens** are shown once (or written to a new mode-600 file with `--output`)
and stored only as SHA-256 hashes. `kaal-server token list` and
`kaal-server token revoke NAME` manage them, and
`kaal-server token scopes NAME --scopes read,reveal,write,delete` changes what
an existing token may do without issuing a new one, so the devices using it
stay signed in. Each token has scopes:

| Scope | Allows |
|---|---|
| `read` | Titles, tags, times and file names in lists and searches; a note's text with identity numbers and similar values hidden; status and a quick verify |
| `reveal` | Plaintext: full note bodies, field values, attachment contents, extracted text, medical receipt fields, the full address a page was saved from |
| `write` | Create, edit, append, attach, capture, extract, trash, restore |
| `delete` | Permanent deletion and purges |
| `admin` | Whole-vault export, and the deep verify that reads every file |

`read` without `reveal` is for a script that needs to know what is there, not
what it says: it sees a note's wording with numbers hidden, so it is not a way
to keep a note's text from someone. A token with `write` but not `reveal` is
answered with the ids and states of what it changed, never the contents.

**People and vaults.** An installation can hold several people, each with a
vault of their own, and a vault can be shared. Someone who belongs to a vault
has a role there, and no token of theirs can do more than the role allows:

| Role | May |
|---|---|
| `owner` | Everything, including whole-vault export |
| `editor` | Read, reveal, write and delete |
| `viewer` | Read and reveal |

```bash
kaal-server account add sam --name "Sam"          # an account and a vault of its own
kaal-server account list
kaal-server vault add Household --owner owner     # another vault
kaal-server vault grant VAULT_ID sam editor       # let sam in, or change the role
kaal-server vault ungrant VAULT_ID sam
kaal-server account disable sam                   # shut out, data kept
kaal-server token create sam-laptop --scopes read,reveal,write --account sam --vault VAULT_ID
```

Accounts and memberships are kept in `accounts.db` in `KAAL_HOME`; new vaults
are folders under `KAAL_HOME/vaults/`. Each new vault is encrypted with a key
of its own, which sits in the vault folder wrapped by the installation's key
file, so one person's files cannot be read as another's even by mixing up
folders. `kaal backup` copies all of it: the first vault as before, and in a
folder named `_installation` inside each backup, the accounts file, the token
file and every other vault. The key file is never copied.

An installation that began with a single vault keeps working untouched: on
first start that vault becomes the vault of an administrator account named
`owner`, nothing in it is moved or re-encrypted, and existing tokens act as
that account. A token that names no account or vault always means those two.

**Signing in.** People sign in to the web app with a sign-in name and a
password. Nobody is given a password: whoever runs the installation makes a
single-use link, and the person chooses their own.

```bash
kaal-server account setup-link sam        # prints a link that works once, for 24 hours
kaal-server account password sam          # or type a password at the server's terminal
kaal-server account rename sam sam@example.com
```

Set `KAAL_PUBLIC_URL` to the address people open and the link is printed
complete. Choosing a password also gives the person eight recovery codes;
each one lets them choose a new password once if they forget theirs. A new
setup link does the same job when the codes are gone.

Passwords are stored as Argon2id hashes. A browser that signs in gets a random
session cookie, of which the server keeps only a hash; signing out, changing
the password or using a recovery code ends sessions on the server. Five wrong
passwords for one sign-in name are free, after which each further try has to
wait, from one minute up to fifteen. Settings shows a person their signed-in
browsers and lets them sign any of them out.

**Signing in with Google (optional).** An installation can also let people
sign in through Google, or any other OpenID Connect service, instead of typing
a password. Nothing is offered until whoever runs the installation sets it up:

1. In the [Google Cloud console](https://console.cloud.google.com/apis/credentials),
   create an OAuth client ID of type "Web application". Under "Authorized
   redirect URIs" add `https://YOUR-KAAL-ADDRESS/v1/sign-in/with/google/callback`.
   Use a Google Cloud project of its own: the name on Google's sign-in page
   belongs to the project, not to the client. The project can stay in
   "Testing": Kaal asks only for name and email, which Google allows for any
   Google account without a warning or a list of test users. Until Google has
   verified the project's branding, its page says "to continue to" your
   server's domain instead of the app name.
2. Give Kaal the client ID and client secret: as an administrator, in the web
   app under Settings → Sign-in services; or on the server, where the command
   asks for the secret without showing it, so that it is never part of a
   command line (`--secret-file PATH` reads it from a file instead):

   ```bash
   kaal-server sign-in add google --client-id ID --address https://YOUR-KAAL-ADDRESS
   kaal-server sign-in list           # also prints the redirect address to register
   kaal-server sign-in remove google
   ```

The sign-in page then shows "Continue with Google". A Google account signs in
as the Kaal account it is connected to, and is connected in one of three ways:
a person who is signed in connects it under Settings (proving their password
first); a person with a setup link presses "Continue with Google" on it
instead of choosing a password; or, where sign-up is open, a Google account
nobody has connected gets a new Kaal account with a vault of its own. An email
address that happens to match a sign-in name connects nothing.

Kaal asks Google only who the person is (`openid email profile`), keeps
Google's id for them and their email address, and asks for no access to their
mail, files or contacts. The client secret is stored encrypted with the
installation's key. The browser makes the trip with a one-time code (the
authorization-code flow with PKCE), every statement's signature, issuer,
audience, expiry and nonce is checked, and a trip can be finished only once,
within ten minutes, by the browser that started it. Another OpenID Connect
service is added the same way with `--issuer https://…` and an id of your own
in place of `google`. Tokens are unaffected: scripts, the command-line client
and the browser extension keep using theirs.

**Apps on a person's own device.** The iPhone app ([`ios/`](ios/README.md))
signs in as the person, not with a token. It opens Kaal's sign-in page in a
browser sheet, the person signs in there however they usually do (password,
Google) and presses Allow, and the browser hands the app a code. The app
exchanges that code for a session (`POST /v1/app-sign-ins`); the code works
once, for five minutes, and only together with a secret that never left the
app, so a code seen by anything else is worthless. The session lasts a year,
is listed with the person's signed-in browsers under Settings, and can be
signed out there. The app sends it in the `X-Kaal-Session` header, never as a
cookie. "Save to Kaal" in other apps' share menus is part of the app and uses
the same session, which the two share through the iPhone's keychain.

In the web app, Settings has more pages for people signed in as
themselves. **Vaults and sharing**: rename your vault, open another, create one,
and (as an owner) let someone into the open vault as viewer, editor or owner,
or take them out. While accounts are by invitation, the people who have an
account are picked from a list; with sign-up open nobody is listed and their
sign-in name is typed. An owner who is also an administrator can **invite
someone new** there: they type the person's name and get a single-use link.
Whoever opens it chooses their sign-in name and password (or continues with
Google), and the vault they were invited into opens for them, with a vault of
their own beside it. **Tokens**: make and revoke tokens for your own scripts and
the browser extension; a token can do no more than your role in its vault, and
its secret is shown once. **People**, for administrators: add a person and get
their setup link, make a new link for someone who lost their password, switch
an account off or on, share the administrator's job, and choose whether
strangers may create accounts. **Sign-in services**, for administrators: offer
Google or another OpenID Connect service, or stop offering one.

An administrator can make a setup link only for someone who has never signed
in: whoever opens a setup link signs in as that person, so a link for an
account in use would let an administrator into its vaults. For someone who has
lost their password and their recovery codes, whoever runs the server makes the
link with `kaal-server account setup-link`.

Accounts are by invitation unless an administrator opens sign-up, in which
case the sign-in page offers "Create an account". Open sign-up is capped at
ten new accounts an hour (`KAAL_SIGNUPS_PER_HOUR`), and a person may own at
most ten vaults (`KAAL_VAULTS_PER_PERSON`). An administrator manages who has
an account, and cannot see into anyone's vault by being one.

A token can never change the password, codes or sessions of the account it
acts for, so a script's token cannot be turned into its owner's sign-in. Making
a token needs the password (or, for an account without one, the sign-in name
typed), and choosing a new password with a recovery code ends every token the
account made.
Tokens remain the way for scripts, the command-line client and the browser
extension to connect.

Whoever runs the installation holds the key file and can read every vault on
it. Accounts keep the people who use an installation apart from each other,
not from its operator.

**Web app.** `kaal-server` serves a small web app at `/`: open the server's
address in a browser on a laptop or phone and sign in with a device token. It
covers medical receipts: add one from a file or the phone's camera, see what
still needs review, check each value against a picture of the receipt (with
where it came from and how far to trust it), mark receipts reviewed or
reimbursed, see totals per person and year, and restore or permanently delete
what is in the Trash. It also covers notes (search, read, write, trash) and
settings: the family list and the provider names Kaal has learnt. Notes are
shown as formatted Markdown; pictures inside a note appear in place, each PDF
shows its first page with the text read from it, and a "Show source" switch
shows the raw note. HTML inside a note is displayed as text, never run.

The notes search looks in titles, tags, the text of each note and the text read
from its files (PDFs, scans, text files). Every word typed must appear
somewhere in the note; put words in "quotes" to find them together. Each result
shows the line it was found in, and names the file when the match is in one.
Medical receipts have their own search on the Receipts screen.

Notes are written in Markdown, with a Preview switch beside the text. Pictures,
PDFs and other files can be added while writing a note or from the note itself
("Add files"). "Insert in text" places a file where the cursor is, as
`![name](attachment:ID)` for a picture or `[name](attachment:ID)` for anything
else. That works in a new note too: its files are stored when the note is
saved, and the text is given their ids then. A picture placed in the text shows
there; a PDF linked on a line of its own shows there as a card with its first
page; files not mentioned in the text are listed under the note.

A picture, PDF or plain-text file can be opened in the browser
(`GET /v1/notes/{id}/attachments/{attachment}/view`); the server checks that
the file really is what its name says. Every other kind of file, such as a
saved web page, is download-only, because opened from Kaal's own address it
could act with your sign-in. Permanent
deletion needs a token with the `delete` scope; a device without it is not
offered the button. It is plain files in
`kaal/web/` with no build step, and it only talks to the API below. The server
tells the browser to run nothing but those files and never to show them inside
another site.

**The browser extension** calls the API from its own pages with its own token.
A browser only allows that when the server names the page's origin, so the
server names Kaal's extension (and nothing else) for `/v1/` addresses. No
cookie is ever allowed across, so a signed-in browser session cannot be used
this way. A build of the extension with a different key has a different id:
list the allowed ids in `KAAL_EXTENSION_IDS` (comma separated), or set it to
`none` to allow no extension.

**Browsers.** A web page served from the same address signs in by sending a
device token once to `POST /v1/session`. The server answers with a cookie that
page scripts cannot read (`HttpOnly`, `Secure`, `SameSite=Strict`, 30 days), and
`DELETE /v1/session` signs out. A request that changes anything and is
authenticated by that cookie must also carry the header `X-Kaal-Request: 1`,
which another website cannot make a browser send, so a forged request is
refused. Tools that send `Authorization: Bearer` are unaffected. Revoking the
token ends its browser sessions too.

For a user interface the API also offers: one receipt with the line of its text
each value came from (`GET /v1/medical/receipts/{id}`, or `kaal medical show`),
a page image of a receipt
(`GET /v1/medical/receipts/{id}/attachments/{attachment}/preview`), and
`status`, `q`, `sort`, `offset` and `limit` on the receipt list, `q` and
`offset` on the note list, with the total returned beside each page.

**Clients.** Point the same `kaal` command at the server with
`~/.config/kaal/config.toml` (or `KAAL_CONFIG`), mode 600:

```toml
url = "https://storage.example.ts.net"
token_file = "~/.config/kaal/mac-cli.token"
```

Every command then runs remotely and prints exactly what it prints locally.
Files you attach are uploaded, and exports are written on your machine.
`KAAL_LOCAL=1` forces local mode. The Chrome extension talks to the server
itself, with a token of its own entered on its settings page.

**Checks and moving data.** `kaal verify [--deep]` compares the index with the
files on disk and reports ids only. `kaal export --output FILE.tar` writes the
vault exactly as stored (the key is not included). `kaal import FILE.tar`
restores into an empty vault and verifies it.

**Leaving, and coming back.**

- **A readable copy.** `kaal export-readable --output FILE.zip` (or, for a
  vault's owner, Settings → Vaults and sharing → Download a copy) writes
  everything in a vault, decrypted: each note as a Markdown file with its files
  beside it, each receipt's original file, and every receipt's values in
  `receipts.csv`. It needs no key and no Kaal, so keep it as safe as the vault.
- **Restoring a backup.** `kaal restore BACKUP_FOLDER`, with Kaal stopped, puts
  the whole installation back the way a `kaal backup` folder has it: the
  vaults, the accounts and the tokens. It first checks that the key opens the
  backup, moves what is there now into a dated `before-restore-…` folder
  instead of deleting it, and checks every restored vault afterwards.
- **Deleting a vault.** An owner deletes the open vault under Settings → Vaults
  and sharing, confirming with their password (or, for an account that signs
  in only through another service, the vault's name). Everything in it goes,
  for everyone who shares it, and tokens made for it stop working. The
  installation's first vault cannot be deleted; backups and older tokens rely
  on it. On the server: `kaal-server vault remove VAULT_ID --yes`.
- **Deleting an account.** Settings → Delete my account, in the web app or
  the iPhone app, shows what would go first: the account, its tokens, and
  every vault only it belongs to. Vaults shared with others stay with them,
  but the only owner of a shared vault must make someone else an owner first,
  and the only administrator cannot leave.
  Administrators remove someone else under Settings → People. On the server:
  `kaal-server account remove LOGIN` says what would go, and `--yes` does it.

Deleted data stays in the daily backups until they are pruned (14 days by
default). To be rid of it sooner, delete the backups made since.

## Threat model

**What encryption protects.** Every note body, attachment and extracted text
is encrypted with AES-256-GCM, so someone holding a copy of the vault files
without the key (a stolen disk, a backup, a snapshot) cannot read them. Losing
the key therefore loses the vault: keep a copy of it somewhere off the server.

**What stays readable.** Not the contents and not the names: titles, tags,
attachment names and medical receipt fields are in the encrypted index. What a
copy of the vault files still shows is its shape: how many notes and attachments
there are, their ids, sizes and file dates, and the size and hash recorded beside
each encrypted attachment. Token names and their scopes are in `tokens.json`,
outside the vault. Backups made before `kaal encrypt-all` was run still hold the
older readable copies until they are pruned, so keep backups as private as the
vault.

**Search and the server's memory.** Searching inside notes means decrypting
them. The running server does that once per note and keeps the text in its own
memory, so that later searches are quick; it rereads a note when the note or
one of its files changes. Nothing of this is written to disk, and it is gone
when the server stops. The key is already in that memory, so this does not
change who can read the vault.

**On the server.** Only `kaal-server`, running as its own user, reads the vault
and the key file. The key is never stored next to the data, and keep a recovery
copy offline. Anyone with root on the server host, or on the hypervisor under
it, can read everything: that is the trust boundary.

**On the network.** The server listens on loopback only. TLS and the choice of
which devices can connect come from the proxy in front of it (for example
`tailscale serve`, reachable from your tailnet only). Each client has its own
revocable token with only the scopes it needs. The CLI refuses plain http to
anything but loopback, and refuses token or config files that others can read.

**What the server never records.** Logs carry the method, route template,
status, token name and duration only, never bodies, query strings, file
contents or tokens. Validation errors name the bad fields without echoing the
values. Uploads go to a private folder that is removed after each request.
A proxy in front of Kaal logs what it is set to: the Docker install's Caddy is
set to keep no record of requests (`deploy/docker/Caddyfile` says why: left
alone, it logs each request that fails, with the visitor's address, the
address asked for and its headers).

**What leaves the server.** Nothing, unless `KAAL_RECEIPT_AI` is set. With it
set, each captured or re-extracted medical receipt's text and page images are
sent to the configured model: Anthropic for `anthropic` and `claude-cli`, the
service at `KAAL_RECEIPT_AI_URL` for `openai`, and for `ollama` a model you run
yourself, on this machine or your own network, so nothing leaves them. A paid
service is used under that account's terms and data settings. Notes and other
attachments are never sent. An API key or a login for the command is a
credential on the server: keep it readable only by the service.

**On the open internet.** Kaal can be put on a public address behind its HTTPS
proxy. What it does about strangers:

- The routes that need no sign-in answer each address only so often
  (`KAAL_SIGN_IN_PER_MINUTE`, 30), and take small requests only. Wrong
  passwords are slowed for the name and the address together, so guessing from
  one place does not lock the real person out from another. Very many
  addresses guessing one name together are slowed to about a try a minute
  between them, yet someone arriving from an address that has not got that
  name wrong always has their first try; recovery codes, and proving a
  password while already signed in, are counted apart. Only a few passwords
  are checked at once (`KAAL_PASSWORD_CHECKS`, 4), and more are told to try
  again.
- The visitor's address is taken from `X-Forwarded-For` only when the request
  comes from a proxy named in `KAAL_TRUSTED_PROXIES` (this machine by default;
  `private` for a proxy in a container; `none` to believe nobody). An IPv6
  visitor is counted by their /56. A proxy of your own must send that header:
  where it does not, Kaal cannot tell visitors apart, holds nothing against
  the one address it sees, and limits them all together instead.
- The session cookie is tied to the one host (`__Host-`), and nothing a
  visitor sends is written to the log as they typed it. With `KAAL_HSTS=1`
  browsers are told to use HTTPS only for the host name, every port of it; the
  installer sets that for a domain of Kaal's own, and it is off elsewhere,
  since a name shared with other services over plain http would lose them.
- Files are read by pdftotext, pdftoppm and tesseract with a time limit, a
  memory limit (`KAAL_FILE_MEMORY_MB`, 1024) and a limit on what they write
  (`KAAL_FILE_OUTPUT_MB`, 256), a few at a time (`KAAL_FILE_JOBS`, 2), with
  nothing of the server's environment, and nothing they complain about is
  kept. A request that cannot have a turn within seconds is told the server is
  busy rather than left waiting; so is one behind a slow change to its vault,
  and a client that stops taking an answer is given up on.
- A vault other than the first may hold `KAAL_VAULT_QUOTA_MB` (10240; 0 for no
  limit), and nothing new is stored, nor a copy of a vault written, once the
  disk has less than `KAAL_FREE_SPACE_MB` (1024) free. A person may own
  `KAAL_VAULTS_PER_PERSON` vaults, hold 50 sessions and 50 tokens (20 new ones
  an hour), send four large files at a time and download one copy of a vault
  at a time. Files being downloaded are held in memory only up to
  `KAAL_DOWNLOAD_MEMORY_MB` (1024) at once, and the text kept in memory for
  searching up to `KAAL_SEARCH_MEMORY_MB` (256) for each vault.
- Letting an app or the browser extension sign in as you needs a sign-in made
  in the last ten minutes, or your password again: what it is given outlasts
  the browser's own sign-in.
- Requests to a sign-in service have one deadline for the whole answer, only a
  few are in progress at once, and a service that is down is not asked again
  by every visitor.

**Not defended against.** A compromised client can do whatever its token allows
until you revoke it, and a token with `admin` can export everything. A
compromised server host exposes everything. The limits above keep one visitor
from getting in the way of the rest; they are not protection against a
determined flood, which belongs to the proxy or the network in front. The
programs that read files run as the same user as the server, so a flaw in one
of them that a crafted file could exploit would reach the key: keep the system
(or the container image) updated. While accounts are by invitation, someone who
owns a vault is shown the names of everyone with an account, to share with
them; with sign-up open they are not, but can find out whether a sign-in name
exists by trying to share with it, slowly. With sign-up open,
set the quota and the hourly sign-up allowance to what the disk can bear. See
[SECURITY.md](SECURITY.md) for reporting a problem.

## How receipts and files are read

- The text of a text or Markdown file is kept beside it as it is.
- A PDF, whether a receipt or a file on an ordinary note, is read from its own text layer (`pdftotext -layout`, which keeps printed columns). A scan with no usable text layer is read by OCR (Tesseract), as is a photo. Nothing else is tried.
- Kaal never guesses the paid amount or date from an unlabelled total, balance, or date-like value. Ambiguous fields stay blank for review.
- The narrow exception is an explicitly **SALE/PAYMENT - APPROVED** card receipt with a labelled **Total Amount**: that is recorded as the completed card payment. A bare `Total` or `Total Amount` is still rejected.
- Kaal records field provenance (`labelled-text`, `ai-grounded`, `ai-unverified`, `ai-amount-due`, `manual`, or `confirmed`). Manual corrections and confirmed values survive re-extraction.
- A receipt cannot be marked reviewed until it has a paid amount and at least one receipt date (service date or payment date). Marking it reviewed confirms non-manual values currently shown in the review screen.

### Reading receipt fields with a model (optional, off by default)

The label rules above only recognise a few fixed layouts. A server can instead
ask a language model which values on a receipt are which. Choose one:

| `KAAL_RECEIPT_AI` | What it uses | Needs |
|---|---|---|
| `off` | Nothing; the label rules alone (the default) | |
| `ollama` | A model you run with [Ollama](https://ollama.com), on this machine or your network. Nothing leaves them. | Ollama with a vision model, for example `ollama pull qwen2.5vl:3b` (about 3 GB) |
| `anthropic` | Claude, through Anthropic's API | An API key; `pip install 'kaal[ai]'` (the container image has it) |
| `openai` | Any service offering OpenAI's chat completions: OpenAI, OpenRouter, LM Studio, vLLM and others | `KAAL_RECEIPT_AI_URL`, `KAAL_RECEIPT_AI_MODEL`, and a key if the service needs one |
| `claude-cli` | The `claude` command line, signed in on the server | The command on `PATH` |

```bash
KAAL_RECEIPT_AI=ollama                    # default: off
KAAL_RECEIPT_AI_MODEL=qwen2.5vl:3b        # defaults: ollama qwen2.5vl:3b, anthropic claude-haiku-5-5, claude-cli haiku
KAAL_RECEIPT_AI_URL=http://127.0.0.1:11434  # ollama and openai; plain http only for this machine or a private network
KAAL_RECEIPT_AI_KEY_FILE=/etc/kaal/model.key  # anthropic and openai; readable only by its owner
KAAL_RECEIPT_AI_DAILY_LIMIT=300           # receipts a day; default 300 for paid services, none for ollama; 0 = no limit
KAAL_RECEIPT_AI_EFFORT=low                # anthropic only; empty for a model that does not take it
KAAL_RECEIPT_AI_TIMEOUT=90                # seconds, default
KAAL_RECEIPT_AI_COMMAND=/path/to/claude   # claude-cli only; default: claude on PATH
```

Without a key file, `anthropic` uses `ANTHROPIC_API_KEY` and `openai` uses
`OPENAI_API_KEY` from the environment. A key never appears in a log, an error or
`kaal status`, which says which model is set up, whether it can be used, and
how many receipts it has read today. When the daily limit is reached, receipts
are still captured and read with the label rules, and the model is asked again
the next day (UTC) or when you choose "Read it again".

With this on, the model is given the receipt's text (its own text layer, or OCR
for a scan) plus an image of the first pages, or the photo itself. It is asked for
nothing but the seven values, in a fixed JSON shape, and given no tools; with
`claude-cli` it also runs with no saved session, no user or project settings and an
empty working directory. So nothing printed on a receipt can make it do anything.

The model only labels; Kaal decides what to trust:

- A value found in the text read from the receipt is `ai-grounded`. Amounts and
  dates are matched by value, so `$1,234.50` grounds `1234.50`.
- A value not found there (a provider name that exists only as a logo, anything
  from a blurry photo) is kept as `ai-unverified`. Unverified amounts and dates
  are left out of report totals until you review the receipt.
- Where a label rule and the model disagree on an amount or a date, the rule's
  value stays; for names the model's value is used. Both are kept under
  `conflicts` for you to see, and reviewing or correcting the field settles it.
- When a receipt lists several payments and no total (two prescriptions on one
  pharmacy receipt), Kaal adds them up, keeps the parts in `field_raw`, and
  marks the total `ai-unverified` because that number is printed nowhere.
- When the document is a bill that shows no payment, the amount it says you owe
  is offered as `ai-amount-due`. Like any unverified amount it stays out of the
  totals until you review the receipt.
- An individual clinician is never accepted as the provider.

**Remembered provider names.** Receipts often print a billing company's legal
name, or a short brand, where you would write the practice's name. When you
type or confirm a provider on a receipt where the model read a different name,
Kaal remembers the pair, and the next receipt printing that name shows your
wording, marked `remembered`, with the printed name kept in `field_raw`. A
provider you typed is never replaced.

```bash
kaal medical providers                          # what Kaal has learnt
kaal medical providers --forget "Printed Name"  # stop replacing it; not relearnt
```

If the model is unreachable, signed out or rate-limited, the receipt is still
captured with whatever the label rules find, and the reason is recorded on it.
`kaal status` shows whether the model is configured and its command exists.
This sends receipt text and page images to the model's provider: see the
threat model.

## Commands

These work on the server itself and, with a client configured, from any other
machine. For use by coding agents, see [`AGENTS.md`](AGENTS.md).

Initialize/status:

```bash
kaal init
kaal status
```

Add a Markdown note:

```bash
kaal add --title "Groceries" --tags home --body $'# Groceries\n\n- milk\n- eggs'
kaal add --title "My ID Info" --tags pii,ids --body $'SSN: <your-ssn>\nDriver License: <your-license>'
```

Label a note (the label is information only; everything is encrypted):

```bash
kaal add --title "Tax note" --body-file /path/to/tax-note.md
kaal add --title "Opening hours" --sensitivity public --body "Mon to Fri, 9 to 5"
```

Or pipe content:

```bash
pbpaste | kaal add --title "Tax note" --tags taxes,pii
```

List or search by title and tag, or search inside notes and their files:

```bash
kaal list
kaal list --tag pii
kaal search passport
kaal library search "boiler pressure"
```

Show a note with identity numbers hidden:

```bash
kaal show "My ID Info"
```

Show everything, only when needed:

```bash
kaal show "My ID Info" --full
```

Copy a field without printing it:

```bash
kaal copy "My ID Info" --field ssn
kaal copy "My ID Info" --field driver_license
```

Field copy works best when notes contain lines like:

```text
SSN: <your-ssn>
Driver License: <your-license>
Passport: <your-passport-number>
DOB: <your-date-of-birth>
```

Append to a note:

```bash
kaal append "My note" --body "New detail here"
```

Attach a file and preserve the original:

```bash
kaal attach "Tax 2025" /path/to/tax-document.pdf --extract
kaal attach "IDs" /path/to/license.jpg --ocr
```

Export an attachment when needed:

```bash
kaal export-attachment "Tax 2025" ATTACHMENT_ID --output /tmp/tax-document.pdf
```

## Saving from Chrome

The Chrome extension sends what you save straight to your Kaal server:

- **One main button.** The popup offers **Save to Kaal** for the page in front
  of you, or **Save receipt** on a site you have saved receipts from before,
  with the other choice as a link underneath. A web page is kept as a PDF; a
  PDF or a linked file is kept as it is.
- **Receipts are checked on the spot.** Saving a receipt opens Chrome's side
  panel beside the page with the web app's review screen: the values Kaal read,
  where each came from, and "Looks right" or "Save for later".
- **Right-click** has two entries, "Save to Kaal" (a page, a link, or selected
  text) and "Save as medical receipt".
- **The number on the icon** is how many receipts are waiting to be checked.
- **No access to websites.** The extension reads the page or file you save
  through the tab itself, at the moment you save, and holds no standing access
  to any site. If a file cannot be read that way, it offers to ask Chrome for
  access to that one site. The sites you have saved receipts from are also learnt from the
  server (`GET /v1/medical/sources`, host names only).

Notes, totals, the Trash and settings are in the web app. Setting the extension
up is described in [`chrome-extension/README.md`](chrome-extension/README.md).

The command line can save a file the same way:

```bash
kaal capture /path/to/file --title "Useful reference"
```

Ordinary notes can also be managed by id. A note must be in the Trash before
it can be deleted for good, and deleting removes only Kaal's own copies, never
the file a capture was made from.

```bash
kaal library list
kaal library search "boiler pressure"     # titles, tags, note text and the text read from files
kaal library show NOTE_ID
kaal library trash NOTE_ID
kaal library restore NOTE_ID
kaal library purge NOTE_ID --yes
```

## Medical receipts

Medical receipts are encrypted like everything else: the original and its
extracted/OCR text are stored encrypted, and the fields read from a receipt
live in the encrypted index.

Capture a downloaded receipt directly:

```bash
kaal medical capture /path/to/receipt.pdf \
  --source-url "https://portal.example/receipt/123" \
  --source-title "Payment confirmed"
kaal medical list --inbox
```

The capture command creates a `medical`, `receipt`, `inbox` record, preserves an
encrypted copy of the original, and extracts PDF text/OCR when possible.

### Receipt values, family members and reimbursement

Each receipt stores dates as `YYYY-MM-DD` and amounts as plain numbers such as
`123.45`, whatever the receipt printed; the printed text is kept beside them in
`field_raw`. Numeric dates are read month first. A date or amount Kaal cannot
read is left empty and flagged, and a correction you type is refused if it
cannot be read. Older records are converted automatically.

```bash
# The people receipts belong to. Names on receipts are matched to this list
# ignoring order, case and punctuation ("DOE, JANE Q" matches "Jane Doe").
kaal medical people --add "Jane Doe" --add "John Doe"
kaal medical people

# Correct a field, then mark the receipt reviewed.
kaal medical update NOTE_ID --fields-json '{"service_date": "3/4/2026", "amount": "40.00"}'
kaal medical review NOTE_ID

# Record a reimbursement (for example an HSA withdrawal), or undo it.
kaal medical reimburse NOTE_ID OTHER_ID --date 2026-05-01 --note "HSA withdrawal 12"
kaal medical reimburse NOTE_ID --undo

# Totals: overall, per person, per year, and what is not yet reimbursed.
kaal medical report --year 2026
kaal medical report --reimbursed no
```

`kaal medical reextract NOTE_ID…` reads stored receipts again, and
`kaal medical reextract --unreviewed` does it for everything still awaiting
review. Values you typed or confirmed are kept, and so is the reimbursement.

On a server, a captured receipt is stored at once and read afterwards: it shows
status `extracting` until the background worker has read it, then
`needs_review`, or `failed` if it could not be read. A re-read is queued the
same way. Work left unfinished by a restart is picked up when the server
starts. Set `KAAL_BACKGROUND_EXTRACTION=0` to read during the capture request
instead.

`kaal medical evaluate` measures how well extraction is doing. It runs on the
server, re-extracts each receipt that has values you typed or confirmed,
compares the two, and prints accuracy per field with receipt ids and outcomes
only, never a value. It changes nothing in the vault, so run it before and
after an extraction change to see the difference.

The report dates a receipt by its service date, or by its payment date when it
has no service date. Receipts with no date are listed under `undated` and
receipts with no amount under `missing_amount`, so nothing is left out
silently. `needs_review` shows how much of the total comes from receipts nobody
has confirmed yet. Only a reviewed receipt can be marked reimbursed.

For the Chrome toolbar and right-click workflow, see
[`chrome-extension/README.md`](chrome-extension/README.md).

Delete a note and its attachments:

```bash
kaal delete NOTE_ID --yes
```

## Publishing

What the App Store and the Chrome Web Store ask for, apart from the accounts,
is in the repository:

- [`docs/`](docs/index.md): a small website for GitHub Pages, with the
  [privacy policy](docs/privacy.md) and [where to get help](docs/support.md)
  that both stores link to.
- [`ios/AppStore/`](ios/AppStore/listing.md): the
  [listing](ios/AppStore/listing.md) (and what the App Store search showed
  about the name), the [privacy answers](ios/AppStore/privacy-labels.md),
  [notes for App Review](ios/AppStore/review-notes.md), why the app needs no
  [export documents](ios/AppStore/export-compliance.md), and the screenshots
  (`make-screenshots.sh` takes them again). The app's privacy manifest is
  `ios/Kaal/PrivacyInfo.xcprivacy`.
- [`chrome-extension/store/`](chrome-extension/store/listing.md): the listing,
  why each permission is needed, the data disclosures, and the screenshots and
  promo tile (`make-screenshots.sh`). `bin/package-extension.py` builds the
  zip to upload, and checks it.
- `bin/demo-data.py`: a Kaal holding only made-up notes and receipts, for the
  screenshots and for the reviewers' demo account.

`tests/test_store_material.py` checks the text against each store's limits,
the pictures against its sizes, the zip against the extension, the privacy
manifest against the code, and those statements in the privacy policy and
the listings that can be checked mechanically (the app's password fields,
the debugger commands the extension sends, what the Docker install's proxy
logs). The rest of the policy is prose: read it again whenever what the
apps store, send or ask for changes.

Only the owner can do the rest:

1. Turn on GitHub Pages for `docs/` (Settings → Pages, "Deploy from a
   branch", folder `/docs`), check that the three pages open (the site has
   not been built anywhere yet), and use their real addresses wherever the
   listings say `OWNER.github.io/REPO`. Turn on private vulnerability
   reporting (Security tab), which the help page points to.
2. Run the demo on a domain (`deploy/demo/run.sh --domain …`, which makes the
   `reviewer` account and fills it with made-up data), and keep it running
   ([`review-notes.md`](ios/AppStore/review-notes.md)). A free cloud machine
   with an sslip.io name will do ([`deploy/demo/README.md`](deploy/demo/README.md)).
3. For the App Store: join the Apple Developer Program, register the bundle
   identifier under that team, create the app in App Store Connect (which is
   when the name is really tested), sign and upload a build from Xcode, fill
   in the listing, privacy and age-rating answers, add contact details, and
   submit. Before that, decide whether the app is offered on iPad (which
   needs iPad screenshots too). Sign in to the https demo server with the
   very build you upload first (step 4 of "Before submitting" in the review
   notes), and go through the notes' steps for the share extension, which is
   part of the app.
4. For the Chrome Web Store: register as a developer (a one-time fee), upload
   the zip, fill in the listing and privacy tabs from
   [`chrome-extension/store/listing.md`](chrome-extension/store/listing.md),
   put the demo's address and the reviewer's password in its test
   instructions, try them with the store's id first (loaded unpacked with the
   item's public key, as that file says), and submit. The server knows the store's copy by its id from 1.0.0-rc2 on;
   an older server's operator allows it by hand (`KAAL_EXTENSION_IDS`).

## Licence

Kaal is released under the [Apache License, Version 2.0](LICENSE). You may use,
change and redistribute it, including commercially, under the terms of that
licence. See [NOTICE](NOTICE) for the copyright line.
