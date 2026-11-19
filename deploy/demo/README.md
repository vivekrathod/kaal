# Kaal's demo

A Kaal that holds only made-up data and goes back to it every night: for
Apple's App Review, which needs a server and an account to try the iPhone app
with, and for anyone who wants to look around Kaal before installing it.

```bash
deploy/demo/run.sh --domain demo.example.org
```

That is the whole installation. It builds Kaal, starts it with HTTPS for that
domain, fills it with the made-up data, and prints the sign-in name and
password to give App Review.

## What is in it

Everything belongs to the made-up Madeup family.

- **The account for App Review**: sign-in name `reviewer`, shown as Jane
  Madeup. It is an ordinary account, not an administrator.
- **Its vault**, named "Demo: anything you add is removed every night", with a
  note at the top of the list saying the same. Below it, seven notes: house
  notes with a table and a PDF service report, a garden plan with a picture in
  it, a home insurance summary with its PDF shown as a card, a recipe, a
  shopping list with ticked items, a car service record, and made-up Wi-Fi
  codes. One old note is in the Trash.
- **Nine medical receipts**, PDFs and one phone photo, read by Kaal's own
  rules as any receipt would be: four waiting to be checked (one is a bill
  with nothing paid yet), four reviewed, one of which has been reimbursed, and
  a copy saved twice in the Trash. Some are from last year, so the totals
  have two years and three people.
- **The family**: Jane, John and Robin Madeup, whom receipts are matched to.
- **A second vault**, "Madeup household (shared)", shared with John Madeup,
  who is also let into Jane's vault as a viewer. John has no password, so
  nobody can sign in as him.
- **A token** named "Kaal for Chrome", so the Tokens page has one to show.
- **The administrator**, `owner`, separate from the reviewer. App Review never
  needs it; use it to look at the administrator's pages (People, Sign-in
  services).

The provider names Kaal has learnt (Settings) stay empty: Kaal learns them
only from a model's reading of receipts, which the demo has switched off.

Dates move with the calendar: a receipt is always "nine days ago", so every
night the demo looks the same, only a day later.

## Running it on a small server

You need a Linux machine with [Docker](https://docs.docker.com/engine/install/)
and its Compose plugin (2.24 or newer), about 1 GB of memory and 2 GB of disk,
and a domain name whose DNS points at the machine, with ports 80 and 443
reachable from the internet. A small virtual server is plenty; a free one with
1 GB of memory does, with swap and smaller limits (see "On a free cloud
machine" below), and an sslip.io name stands in for a domain.

```bash
git clone REPOSITORY-ADDRESS kaal
cd kaal
KAAL_DEMO_PASSWORD=choose-one-for-app-review deploy/demo/run.sh --domain demo.example.org
```

The reviewer's password is `KAAL_DEMO_PASSWORD` when you set it (10 to 128
letters, digits, dots, dashes or underscores, so it can be typed anywhere).
Without it, one is made and printed once. Either way it is kept and stays the
same through every reset and restart, so what you told App Review keeps
working. The administrator's password is always made for you and printed once.

The other ways to reach it, as for a regular installation:

```bash
deploy/demo/run.sh --behind https://demo.example.ts.net   # behind Tailscale or a proxy you run
deploy/demo/run.sh --local                                # http://localhost:8770, to try it
```

Add `--port N` to choose where it listens on the machine (default 8770, on
127.0.0.1 only), `--reset-at HH:MM` to choose the time of the nightly reset
(UTC, default 03:00), and `--image REF` to use a ready-made image instead of
building one. It must be an image with Kaal's `ai` extra, as the regular one
is: the demo draws its pictures with Pillow, which comes with it.

The demo can run beside a regular Kaal on the same machine. It is a separate
Docker Compose project (`kaal-demo`) with its own image name
(`kaal-demo:local`), it has no data volumes at all, and it never touches the
regular installation's. Only one of the two can have ports 80 and 443: if the
regular one uses `--domain`, run the demo `--behind` and point that proxy at
`127.0.0.1:8770`. `KAAL_DEMO_PROJECT=kaal-demo-2` runs a second demo, with
its own settings file.

### Without a domain of your own

A name from [sslip.io](https://sslip.io) points at an IP address with no
setting up: `203-0-113-7.sslip.io` is `203.0.113.7`. Caddy gets a certificate
for it like any other name, so the demo is on HTTPS at once:

```bash
deploy/demo/run.sh --domain 203-0-113-7.sslip.io
```

The address changes if the machine's IP does, and then the reviewers'
instructions must too.

### On a free cloud machine (Oracle Cloud, as an example)

Oracle Cloud's Always Free tier has machines that run the demo at no cost.
The steps below are what it took there; another provider's free or smallest
machine works the same way, with its own names for these things.

1. **Make the machine.** Compute → Instances → Create instance. A free
   machine can be made only in the account's home region. The Ampere (Arm)
   shape, `VM.Standard.A1.Flex`, has plenty of room, but it is often out of
   capacity ("Out of capacity for shape … in availability domain"); try each
   availability domain, or try again later. Otherwise use
   `VM.Standard.E2.1.Micro` (1 GB of memory), which some availability domains
   offer and others do not, and which takes Ubuntu 22.04 but not 24.04: choose
   the availability domain first, then "Canonical Ubuntu 22.04 Minimal", then
   that shape (Change shape → Specialty and previous generation). Let the form
   create a new virtual cloud network with a public subnet, and paste an SSH
   public key of your own.
2. **Give it a public address.** If the form could not, open the instance →
   Networking → its VNIC → IP administration → the private address's ⋯ menu →
   Edit → "Ephemeral public IP".
3. **Let the web in.** In the network's default security list (Networking →
   Virtual cloud networks → the network → Security), add an ingress rule from
   `0.0.0.0/0` for TCP destination ports `80,443`. Oracle's Ubuntu images
   also refuse everything but SSH in their own firewall, so on the machine:

   ```bash
   sudo iptables -I INPUT 5 -p tcp -m state --state NEW --dport 80 -j ACCEPT
   sudo iptables -I INPUT 5 -p tcp -m state --state NEW --dport 443 -j ACCEPT
   sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
   ```

4. **On a 1 GB machine, add swap**, since reading a large photo needs more
   than the machine has to spare:

   ```bash
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

5. **Install Docker** from [Docker's own packages](https://docs.docker.com/engine/install/ubuntu/)
   (`docker-ce` and `docker-compose-plugin`), clone Kaal, and start the demo
   with the published image, which saves building it on a small machine:

   ```bash
   git clone REPOSITORY-ADDRESS kaal && cd kaal
   deploy/demo/run.sh --domain 203-0-113-7.sslip.io --image ghcr.io/OWNER/kaal:VERSION
   ```

6. **On a 1 GB machine, shrink the demo** to fit beside the system and Caddy:
   add these lines to `deploy/demo/kaal-demo.env` and run
   `deploy/demo/run.sh` again.

   ```text
   KAAL_DEMO_MEMORY=700m
   KAAL_DEMO_STORAGE=128m
   ```

Then check it from another machine: open the address, sign in as `reviewer`,
and go through what the reviewers will do (see the store notes below). When
the reviews are over, terminate the instance; its public address goes with it.

## What to give App Review

In App Store Connect, under the version's **App Review Information**, tick
**Sign-in required** and enter:

- **User name**: `reviewer`
- **Password**: the reviewer's password

and in **Notes**, something like:

> Kaal connects to a server that its user runs. Please use our demo server,
> which holds only made-up data. On the first screen, under "Address of your
> Kaal", enter demo.example.org and press Sign in. Kaal's sign-in page opens:
> sign in with the user name and password above, then press Allow.
> The demo goes back to its made-up data every night at 03:00 UTC; anything
> added is removed then and the app is signed out, so sign in again the same
> way if that happens.

Keep the demo running until the review is over. `deploy/demo/run.sh status`
shows whether it is up, and the web app at the same address shows the same
account.

## What to give the Chrome Web Store

The developer dashboard's Test instructions field takes the text in
[`chrome-extension/store/listing.md`](../../chrome-extension/store/listing.md)
("Test instructions"), with the demo's address and the reviewer's password
put in. The sign-in name is `reviewer` there too. The address goes into the
extension's own Settings page, not a browser tab: opening it in a tab shows
the web app's sign-in page, which is the last step of those instructions.

Try them yourself first with a copy that has the store's extension id (the
same section says how: the uploaded package, loaded unpacked with the store
item's public key). The demo lets that id in from Kaal 1.0.0-rc2 on.

## Resetting

Every night at the time chosen with `--reset-at` (03:00 UTC unless you chose
another), the demo empties itself and puts the made-up data back. To do it now:

```bash
deploy/demo/run.sh reset
```

A reset starts from nothing: the demo throws away everything it holds, the
accounts, vaults, tokens and sign-ins included, and makes it all again. So it
removes whatever anyone added or changed, signs everyone out, and puts both
passwords back to the ones in the demo's settings file. It takes about a
minute, during which the demo does not answer, so choose a time when the
people you expect are unlikely to be looking: 03:00 UTC is the evening in
California, and `--reset-at 10:00` is the middle of the night there.

## What a visitor can do, and what puts it right

The reviewer's password is given to App Review and to anyone else you tell, so
count on strangers having it. Whoever has it can do everything that account
can. None of it reaches past the demo, and none of it lasts:

| A visitor can | What puts it right |
|---|---|
| Add, change, trash or delete notes and receipts, add files, change the family names | The nightly reset |
| Change the reviewer's password, with the password or with a recovery code | Put back within a minute, and everyone is signed out, whoever changed it included; the recovery codes are replaced, and every token made with the old password stops working (the one the Tokens page shows is made again) |
| Rename a vault, which removes the notice in its name | The name is put back within a minute |
| Rewrite, trash or delete the "Read me first" note that says this is a demo | The note is put back within a minute, as it was |
| Add a vault, under any name | It is removed within a minute |
| Take John out of a vault, or revoke the token the Tokens page shows | The nightly reset |
| Delete the reviewer's account or one of its vaults, or hand a vault to John | The whole reset happens within a minute instead of at night |
| Fill the demo's storage with files | Kaal refuses a file it has too little room for, and the whole reset happens within a minute of less than 52 MB being free |
| Make tokens (at most 50 at a time) | They work until the next reset, or until a changed password is put back, and reach only what the reviewer can see |
| See which browsers and apps are signed in as `reviewer` (the kind of device and when, nothing more) and sign them out | Signing in again; the nightly reset clears the list |
| Download a copy of a vault | Nothing to put right: it holds only what is in the demo |
| Type wrong passwords on the sign-in page | Kaal slows guessing per address, and whoever has not typed it wrong from where they are always has their try, so one visitor's wrong tries do not hold up another's |
| Type wrong passwords where Kaal asks a signed-in person for theirs again (making a token, new recovery codes, changing the password, deleting) | From the sixth wrong try, those few things make everyone wait: a minute at first, up to a quarter of an hour, and each further wrong try starts the wait again. Signing in and everything else carry on. The nightly reset clears it |

A visitor cannot open the administrator's pages, make an account, read
anything outside the demo's container, or touch another Kaal on the machine.

What is left: anything a visitor types can be read by every other visitor
until the reset, so the notice asks people to put nothing real in. And someone
who keeps at it can spoil the demo for others: by trashing the notes just
before a reviewer looks, by deleting the account again and again so that the
demo keeps starting over, by signing in so often that earlier sign-ins are
ended (an account keeps its newest 50), or by sending requests so fast that
the account's allowance for the minute is used up and others are told to wait.
What a regular Kaal counts for each person (tokens an hour, files on their way
out, giving the password again) the demo does not hold against the one account
everyone shares (`KAAL_SHARED_ACCOUNTS`), so one visitor cannot use those up
for the rest.
If that happens during a review, give the reviewer account a new password and
tell App Review:

```bash
KAAL_DEMO_PASSWORD=a-new-one-for-app-review deploy/demo/run.sh
```

That starts the demo again with the made-up data and the new password.

## Other commands

```bash
deploy/demo/run.sh status      # is it running, and where
deploy/demo/run.sh logs        # what it has said (follow with -f)
deploy/demo/run.sh stop        # stop it; start brings it back with the made-up data
deploy/demo/run.sh start
deploy/demo/run.sh remove      # remove its containers, certificate and image
```

To update the demo: `git pull`, then `deploy/demo/run.sh` again. It builds
the new Kaal and makes the container again when the demo's own script has
changed.

Running `deploy/demo/run.sh` again, with or without options, keeps the
address, port and passwords chosen before and applies what is new.

## Why it is safe to put on the internet

- **The same hardening as a regular installation**, from the same Compose
  file: the container is read-only, has no Linux capabilities and cannot gain
  privileges, its logs are kept small, and Kaal can be reached directly only
  from the machine itself (127.0.0.1), with Caddy (HTTPS) or your own proxy in
  front. Kaal's own protections are all on: sign-in is limited per address,
  wrong passwords are slowed, and on a domain of the demo's own browsers are
  told to use HTTPS only. Behind a proxy of your own, the proxy must pass on
  who is asking (the `X-Forwarded-For` header, which Tailscale and Caddy
  send), or Kaal cannot tell one visitor from another.
- **Nothing on disk.** The vaults, the accounts and the key are held in memory
  inside the container and are gone when it stops. The demo refuses to start
  unless those folders really are in memory, so it can never empty a volume
  that holds anyone's real data.
- **Limits for strangers**: files up to 10 MiB, at most 256 MB stored in all
  (a file takes about a third more room than its size once encrypted), at
  most 1 GB of memory and 256 processes. Once less than 52 MB of that storage
  is free (Kaal itself stores nothing new under 32 MB, and the rest is room
  for two files of the largest size), the demo starts again with the made-up
  data, as it does if the memory runs out. Anything stored is gone by the
  next morning. What Kaal holds in memory is cut to fit: 128 MB of files on
  their way to people, and 64 MB of text for searching in each vault. A
  program reading a file may write 32 MB at most, where a regular
  installation allows as much as the demo's whole storage.
- **One account for everyone**: whoever looks at the demo shares the
  reviewer's allowance of requests, so it is 1,800 a minute, three times a
  regular installation's.
- **No new accounts**: sign-up is closed, and the limit on new accounts is
  zero even if an administrator opened it. A person may own at most three
  vaults.
- **Nothing leaves the machine**: reading receipts with a model is off, and a
  regular installation's extra settings (`deploy/docker/kaal.env`) are never
  read.

To change a limit, add a line to the demo's settings file,
`deploy/demo/kaal-demo.env`, and run `deploy/demo/run.sh` again:

| Setting | Default |
|---|---|
| `KAAL_DEMO_MAX_UPLOAD_BYTES` | `10485760` (10 MiB) |
| `KAAL_DEMO_STORAGE` | `256m` |
| `KAAL_DEMO_FREE_SPACE_MB` | `32` (keep it at least twice the largest file) |
| `KAAL_DEMO_MEMORY` | `1g` |

That file also holds both passwords; it is readable only by you and is never
committed.

## How it works

`compose.yaml` here extends the regular `deploy/docker/compose.yaml` and
changes only what is listed above. Inside the container, `demo.py` runs in
place of Kaal's usual start: it starts `kaal-server`, makes the administrator's
password with a setup link from `kaal-server` itself, and then puts everything
else in through Kaal's API, signed in as the people in it, exactly as the web
app and the iPhone app would. The receipts and their PDFs and pictures are
drawn by `demo.py` on the spot. A reset stops Kaal, empties the memory it
used, and does it all again. Between resets `demo.py` looks at the reviewer's
account once a minute, as described above.


The tests for all of this are in `tests/test_demo.py`. The last one builds and
runs the real demo with Docker under the project name `kaal-test-demo`, and
removes it afterwards; it takes a few minutes, so it runs only with
`KAAL_DEMO_DOCKER=1`.
