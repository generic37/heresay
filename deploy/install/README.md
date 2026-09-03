# Fresh-server install checklist

Stand up a public house on a fresh Ubuntu VPS. Paste each block in order.
Everything here is house-neutral. ⚠ **Three placeholders appear in the blocks you
paste** — `YOUR-DOMAIN`, `YOUR-ADMIN-NAME` and `YOUR-HOUSE-NAME`. Replace each with
your own before running anything; nothing here should be pasted with a value you did
not choose.

⚠ **Nothing in `deploy/` is installed by the repo itself.** It holds the
reference instance's production artifacts as a starting point for your own; this
checklist is what puts each piece where it belongs. The cards under
`deploy/install/` are copied elsewhere during setup, and the live scripts under
`deploy/bin/` run in place from `/opt/heresay/deploy/bin/`.

The kit this installs from:
- `config.example.toml`, `Caddyfile.example` — copy-then-EDIT templates.
- `heresay.service`, the four `*.timer` + their `*.service`,
  `heresay-maintenance.sh` — copy verbatim.
- `fail2ban-heresay.filter.conf`, `fail2ban-heresay.jail.conf` — copy verbatim
  into fail2ban's two directories (§11); the jail is the one file here you should
  edit after copying, to add your own admin address.
- The alerting layer (§12): four `heresay-*.sh` host-ops scripts copied to
  `/usr/local/bin`, four units + the `heresay-notify@.service` template, two
  drop-ins (`onfailure.conf`, `hcping.conf`), and `notify.env.example` — the
  second file here you must edit after copying, because it holds two secrets.
- The three live scripts run in place from `/opt/heresay/deploy/bin/`
  (`heresay-backup.sh`, `heresay-cert-copy.sh`, `heresay-depscan.sh`); nothing
  to install for them.

Rule of the stall floor: never hand-edit anything under `/opt/heresay`. You edit
only the copies under `/etc`.

---

## 1. Prerequisites

- An Ubuntu 24.04 box (Python 3.12 ships as `python3`).
- A DNS **A** (and **AAAA**) record for your domain pointed at the box, and
  ports **80** and **443** reachable — Caddy needs them for the TLS cert.

```bash
sudo apt update
sudo apt install -y python3 python3-venv rsync git fail2ban
```

⚠ **`fail2ban` is in that line on purpose** — §11 configures a jail, and it
needs the package. It is turned on in §7 with everything else.

Caddy, from its official repo (verify current steps at caddyserver.com):

```bash
sudo apt install -y debian-keyring debian-archive-keyring \
  apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

## 2. Get the code onto the box

**First bring-up — clone to `/opt/heresay`:**

```bash
sudo git clone <your-repo-url> /opt/heresay
cd /opt/heresay
sudo python3 -m venv .venv
sudo .venv/bin/pip install --quiet --upgrade pip
sudo .venv/bin/pip install --quiet --require-hashes -r requirements.txt
```

⚠ **`--require-hashes`, and the same pinned file `make deploy` uses.** The
three runtime dependencies and their whole transitive closure are pinned with
sha256 hashes in `requirements.txt`, so nothing resolves from PyPI at install
time. It is all-or-nothing: one missing hash fails the install outright, which
is the point. ⚠ The pip self-upgrade above stays a separate line for that
reason.

**Later updates — the workshop pattern (from your workstation, not the box):**
create git-ignored `deploy/local.env` with `DEPLOY_HOST=<ssh-host>`, then
`make deploy`. It rsyncs the repo to `/opt/heresay`, syncs the venv, and
restarts the daemon (never touching `/etc/heresay` or `/var/lib/heresay`). It
expects `heresay.service` to already exist, so use it only after §7.

## 3. Service user and directories

The unit runs as `User=heresay` / `Group=heresay`; state lives in
`/var/lib/heresay` (that user's, mode 700), config in `/etc/heresay`.

```bash
sudo useradd --system --user-group --home-dir /var/lib/heresay \
  --shell /usr/sbin/nologin heresay
sudo install -d -o heresay -g heresay -m 700 /var/lib/heresay
sudo install -d -o root   -g root   -m 755 /etc/heresay
sudo chown -R heresay:heresay /opt/heresay/.venv
```

## 4. Close the box down before you open the doors

⛔ **Allow SSH first.** `ufw` denies inbound by default the moment it is enabled,
and enabling it without an SSH rule from an SSH session locks you out of your own
server. The order below is the whole safety of it.

```bash
sudo ufw allow OpenSSH        # port 22 — do this BEFORE enabling, or you are locked out
sudo ufw allow 80/tcp         # Caddy needs it to obtain and renew the TLS certificate
sudo ufw allow 443/tcp        # the web client and the WebSocket door behind it
sudo ufw allow 2323/tcp       # the raw TLS door, for telnet/tt++ style clients
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw enable
sudo ufw status verbose       # confirm all four rules are listed before you log out
```

⚠ **Nothing else needs to be reachable.** The plaintext console (`2324`) and the
WebSocket (`8765`) bind to loopback only and must stay that way — Caddy reaches
the WebSocket from the same box. If you do not want the raw TLS door at all, drop
the `2323` rule and browser clients still work.

**Automatic security updates.** Unattended upgrades installs patches from the
security pocket on its own, which is the difference between a box that is patched
and a box you keep meaning to patch:

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades    # answer yes
systemctl status unattended-upgrades --no-pager
```

⚠ It applies **security** updates and does not reboot on its own. The monthly
maintenance timer (§12) does the full upgrade and the reboot, on a schedule you
can see coming.

## 5. Copy the templates to their destinations

```bash
SRC=/opt/heresay/deploy/install

# systemd units (verbatim)
sudo cp $SRC/heresay.service              /etc/systemd/system/
sudo cp $SRC/heresay-backup.service       /etc/systemd/system/
sudo cp $SRC/heresay-backup.timer         /etc/systemd/system/
sudo cp $SRC/heresay-cert-copy.service    /etc/systemd/system/
sudo cp $SRC/heresay-cert-copy.timer      /etc/systemd/system/
sudo cp $SRC/heresay-maintenance.service  /etc/systemd/system/
sudo cp $SRC/heresay-maintenance.timer    /etc/systemd/system/
sudo cp $SRC/heresay-depscan.service      /etc/systemd/system/
sudo cp $SRC/heresay-depscan.timer        /etc/systemd/system/

# fail2ban's two files — note the RENAME: fail2ban names a jail and its filter
# by filename, and both must be `heresay` for `filter = heresay` to resolve.
sudo cp $SRC/fail2ban-heresay.filter.conf /etc/fail2ban/filter.d/heresay.conf
sudo cp $SRC/fail2ban-heresay.jail.conf   /etc/fail2ban/jail.d/heresay.conf

# maintenance script (verbatim, to the path its unit names)
sudo install -m 0755 -o root -g root \
  $SRC/heresay-maintenance.sh /usr/local/sbin/heresay-maintenance

# the alerting layer (§12) — scripts to /usr/local/bin, units beside the rest.
# ⚠ /usr/local/bin, NOT /opt/heresay: these are host-ops scripts, and that path
# sits OUTSIDE the deploy rsync target, so `make deploy --delete` can never wipe
# them and a code push never disturbs the alerting.
# ⚠ THAT is also why `make deploy` never UPDATES them: after the first install
# below, the one command that refreshes these when a script changes is
# `make deploy-alerts` (from a workstation, same host machinery as `make deploy`;
# it runs exactly this install loop and nothing else). This loop is the first-time
# install; `make deploy-alerts` is every time after.
for f in heresay-notify heresay-hcping heresay-ceiling-watch heresay-report-watch \
         heresay-healthcheck heresay-boot-notify; do
  sudo install -m 0755 -o root -g root $SRC/$f.sh /usr/local/bin/$f.sh
done
sudo cp $SRC/heresay-notify@.service        /etc/systemd/system/
sudo cp $SRC/heresay-ceiling-watch.service  /etc/systemd/system/
sudo cp $SRC/heresay-report-watch.service   /etc/systemd/system/
sudo cp $SRC/heresay-healthcheck.service    /etc/systemd/system/
sudo cp $SRC/heresay-healthcheck.timer      /etc/systemd/system/
sudo cp $SRC/heresay-boot-notify.service    /etc/systemd/system/

# OnFailure drop-ins — the five units worth being woken up for.
for u in heresay heresay-depscan heresay-backup caddy heresay-cert-copy; do
  sudo install -d /etc/systemd/system/$u.service.d
  sudo cp $SRC/onfailure.conf /etc/systemd/system/$u.service.d/onfailure.conf
done
# ...and the backup's dead-man's-switch ping.
sudo cp $SRC/hcping.conf /etc/systemd/system/heresay-backup.service.d/hcping.conf

# ⛔ The secret file: the EXAMPLE always, the real one only if absent — never
# clobbering values you have already filled in.
sudo cp $SRC/notify.env.example /etc/heresay/notify.env.example
if [ ! -e /etc/heresay/notify.env ]; then
  sudo install -m 0600 -o root -g root \
    $SRC/notify.env.example /etc/heresay/notify.env
fi

# the two EDIT-first templates
sudo install -m 0640 -o root -g heresay \
  $SRC/config.example.toml /etc/heresay/config.toml
sudo cp $SRC/Caddyfile.example /etc/caddy/Caddyfile
```

## 6. Fill in the two copies (and the cert-copy override)

Edit only these `/etc` copies — the templates in `/opt/heresay` stay pristine.

**Caddyfile** — replace the placeholder domain:

```bash
sudo sed -i 's/your\.domain\.example/YOUR-DOMAIN/g' /etc/caddy/Caddyfile
```

**config.toml** — ⛔ **two values are REQUIRED, and the house refuses to boot until
both are set.** Both ship as bracketed placeholders, and the boot check rejects ANY
value still carrying one:

- **`[moderation] owner`** — the account name that OWNS this house (§8 claims it).
- **`[world] instance_name`** — what the house calls itself on screen.

⚠ **A third is not fatal but should not be left as shipped:** `source_url` ships as
`https://example.invalid/your-heresay-fork`. AGPL §13 obliges a public house to offer
its users the corresponding source, and `.source` prints whatever is in there.

The paths already point at `/opt/heresay/house` and `/var/lib/heresay` and need no
change.

```bash
sudo sed -i 's/^owner = .*/owner = "YOUR-ADMIN-NAME"/'          /etc/heresay/config.toml
sudo sed -i 's|^instance_name = .*|instance_name = "YOUR-HOUSE-NAME"|' /etc/heresay/config.toml
# then set source_url, and review the [ratelimit]/[mail] dials while you are in there:
sudoedit /etc/heresay/config.toml
```

⛔ **Check that both landed** — a `sed` whose pattern matches nothing exits 0 having
changed the file not at all, and the next thing that tells you is the daemon refusing
to start in §7:

```bash
grep -E '^(owner|instance_name|source_url) =' /etc/heresay/config.toml
```

⚠ **No `[bracketed]` value may remain**, anywhere in the file — that is the check the
server runs, not a check on these three keys alone.

**permissions.toml** — who may run what. ⛔ **The daemon never writes it** (the
unit mounts `/etc` read-only, deliberately: a service that can rewrite its own
configuration is one whose configuration is not protected), so it is created and
kept complete by a root command. `make deploy` runs this for you before every
restart; run it by hand on a fresh box, or any time:

```bash
cd /opt/heresay && sudo /opt/heresay/.venv/bin/python -m heresay \
    -c /etc/heresay/config.toml --write-permissions
```

⚠ **Run it from `/opt/heresay`.** The venv holds the three dependencies only —
the `heresay` package itself is not installed into it — so `-m heresay` is found
because the working directory is on the path. That is what
`WorkingDirectory=/opt/heresay` does for the service; without the `cd` you get
`No module named heresay`.

Idempotent: absent, it writes the file complete (merging any older table from
`state.json`); present, it appends only the commands a newer release added, in a
marked block, leaving your values and comments alone. The house starts fine
without it — every command falls back to its built-in default — so this is
housekeeping, never a blocker.

**forbidden_names.txt** — names you will not have registered. Beside
`config.toml` and read-only to the daemon, exactly like the file above, but this
one is **hand-written and entirely optional**: no command creates it, and absent
means you simply have no list of your own.

```bash
sudoedit /etc/heresay/forbidden_names.txt
```

One name per line, `#` comments, case-insensitive. It **adds** to the rank names
the engine already reserves and can never unreserve one. Edits apply live within
a few seconds — no restart. If somebody already holds a name you add they keep
it; the log says how many accounts, never which.

**The graceful countdown** needs nothing installed — the shipped
`heresay.service` already carries the three directives it depends on. ⚠ One of
them, `RestrictAddressFamilies=…AF_UNIX`, fails *quietly* if it is ever lost:
the control socket cannot bind, the house runs perfectly, and the countdown
simply never happens. The startup log says `control socket NOT started` when
that is what has gone wrong.

To warn the house and take it down, on the box:

```bash
cd /opt/heresay && sudo -u heresay /opt/heresay/.venv/bin/python -m heresay \
    -c /etc/heresay/config.toml --shutdown 30      # or --shutdown cancel
```

**notify.env** — ⛔ **the alerting layer does nothing until you fill this in**,
and it is the one file in the kit that holds real secrets. §5 seeded it from the
example if it was absent; open it and replace both placeholders:

```bash
# a long, unguessable ntfy topic — it is the ONLY auth on public ntfy.sh
echo "heresay-$(head -c 9 /dev/urandom | base32 | tr '[:upper:]' '[:lower:]' | tr -d '=')"
sudoedit /etc/heresay/notify.env
sudo chmod 600 /etc/heresay/notify.env
```

Then subscribe your phone to that topic in the ntfy app, and test:

```bash
sudo /usr/local/bin/heresay-notify.sh high "Heresay: test" "if this buzzes, alerting works"
```

⚠ Until both values are real the scripts skip quietly and say so on stderr —
they never fail their caller. See §12 for what fires when, and for the
Healthchecks half.

**cert-copy override** — the daily cert-copy job reads the real domain from
here, so a future deploy never clobbers it:

```bash
echo 'DOMAIN=YOUR-DOMAIN' \
  | sudo tee /etc/heresay/cert-copy.env >/dev/null
sudo chmod 640 /etc/heresay/cert-copy.env
sudo chown root:heresay /etc/heresay/cert-copy.env
```

## 7. Turn it on

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now heresay
sudo systemctl enable --now \
  heresay-backup.timer heresay-cert-copy.timer heresay-maintenance.timer \
  heresay-depscan.timer
sudo systemctl enable --now fail2ban
sudo systemctl enable --now heresay-ceiling-watch.service \
  heresay-report-watch.service heresay-healthcheck.timer
sudo systemctl enable heresay-boot-notify.service      # NOT --now: see §12
sudo systemctl reload caddy
```

⚠ **Put your own address in the jail before that `enable`** — see §11. Shipped,
`ignoreip` is loopback only, which protects the console and nobody else.

⛔ **`SystemCallFilter=@system-service` is the one line in the unit that can stop a
working daemon from starting.** Python with argon2-cffi and cryptography is
normally fine under it, but that depends on this box's libc, kernel and wheels.
**If the daemon fails to start, comment out `SystemCallFilter` and
`SystemCallErrorNumber` in `/etc/systemd/system/heresay.service` and
`daemon-reload` again** — nothing else in the unit depends on them, and a daemon
running without a syscall filter beats one that will not run.

⚠ **`MemoryMax=1G` is a tunable, and too low is worse than absent.** argon2id is
memory-hard on purpose — 64 MiB × 4 lanes, transient, per hash in flight — so a
burst of logins really does allocate hundreds of megabytes for a moment. Size it
to your box. Being OOM-killed mid-conversation is a worse outcome than no cap.

The unit ships hardened; `systemd-analyze security heresay` scores it **1.7 OK**.
Run it yourself after installing — the score is a useful thing to keep an eye on.

- **Caddy** fetches and auto-renews the Let's Encrypt cert for your domain on
  its own (given DNS + open 80/443), serves `web/`, and proxies `/ws`.
- **cert-copy** hands that cert to the raw TLS door on `:2323`. On first boot
  the cert doesn't exist yet, so heresay logs "TLS listener skipped" and only
  the plaintext + WebSocket doors come up. Once Caddy has the cert, run the
  copy once — it installs the cert and restarts heresay, and the daily timer
  keeps it fresh thereafter:

```bash
sudo systemctl start heresay-cert-copy.service   # after Caddy has the cert
```

⚠️ Never `systemctl start heresay-maintenance.service`, and never run
`/usr/local/sbin/heresay-maintenance` without `--no-reboot` — it reboots the
box. Enabling its **timer** (above) is all you do.

## 8. Claim the owner account

The name in `[moderation] owner` can be registered **only from the localhost
listener** while no such account exists — this prevents anyone from capturing the
owner on a fresh or wiped install. From the box itself:

```bash
telnet 127.0.0.1 2324        # or: ncat 127.0.0.1 2324
# Name (or 'quit'): YOUR-ADMIN-NAME
# No one uses 'YOUR-ADMIN-NAME'. Create it? (yes/no) yes
# choose a password — this account now holds the OWNER level at login.
```

## 9. Verify

```bash
sudo journalctl -u heresay -n 20 --no-pager
```
Look for `world: N room(s) loaded from /opt/heresay/house/world; entry <id>; jail <id>`
— `entry` is whatever you set in `[world] entry_room`, and on the shipped skeleton that
is `void`, its only room; `jail` reads **`none`** unless you set `[world] jail_room`,
which the template deliberately leaves unset. Then `echoes: N line(s) loaded`, which
reads `0 line(s)` on the skeleton because it ships no `world/echoes.txt` — ⚠ neither
of those is a fault.

```bash
systemctl list-timers | grep heresay      # five timers scheduled
sudo fail2ban-client status heresay       # the jail is up and watching the file
systemctl is-active heresay-ceiling-watch # the alert watcher is following the log
systemctl is-active heresay-report-watch  # the report watcher, likewise
```

Then connect for real: open `https://YOUR-DOMAIN/` in a browser, and/or
`telnet 127.0.0.1 2324` from the box. Two doors, one room.

⛔ **Confirm the web client loads AND connects.** A Content-Security-Policy is the
one part of this deployment that can white-screen the page, and the browser
console is where it says so.

⚠ **If the page loads but never connects, the WebSocket is being blocked by
`connect-src 'self'`.** Swap in the commented fallback line in your Caddyfile with
your real domain — `connect-src 'self' wss://your-domain.example`. (`'self'`
covers same-origin `wss://` under CSP Level 3 and every current browser implements
it, which is why it is the default; this is the escape hatch, not the
expectation.)

💡 You can rehearse the whole thing on localhost before it is anywhere near the
live site: `deploy/Caddyfile.dev` carries the identical policy, so `caddy run
--config deploy/Caddyfile.dev --adapter caddyfile` and a browser will tell you
whether the CSP is right.

**Backups.** `heresay-backup.timer` writes a nightly local snapshot to
`/var/backups/heresay` (newest 14 kept). That is on-box only — **taking a copy
offsite is your job.** The reference pattern is `make fetch-backup` from a
workstation (scp the newest tarball into the repo's gitignored `backups/`, mode
0700); schedule it however your workstation allows.

## 10. ⛔ The one-time `mail.key` backup — do this once, by hand

**The nightly tarball does not contain `mail.key`, and that is deliberate.**
Mail and `.revtell` buffers are encrypted with it; a tarball carrying both the
ciphertext and the key that opens it is not an encrypted backup, it is a
plaintext one with extra steps — and that tarball is the thing that leaves the
machine. So the key is excluded.

The key is generated on first run and **never rotates**, so it does not want a
nightly copy. It wants one careful copy, made once, kept somewhere the tarballs
are not:

```bash
sudo cat /var/lib/heresay/mail.key
```

Paste it into your password manager (or write it to offline media) under a name
you will find in five years — `heresay mail.key — YOUR-DOMAIN`. It is 44
characters. Then verify you can read it back before you close the window.

⚠ **Until you do this, the key exists in exactly one place: this server.** Every
nightly tarball from now on holds mail that nobody — including you — can read
back without it. Losing the disk loses the mail, however many backups you have.

### ⚠ Restoring takes TWO inputs

A restore is the tarball **and** the separately-stored key:

```bash
systemctl stop heresay
tar -xzf heresay-YYYYMMDD.tar.gz -C /var/lib --strip-components=0
printf '%s' 'THE-KEY-FROM-YOUR-PASSWORD-MANAGER' > /var/lib/heresay/mail.key
chmod 600 /var/lib/heresay/mail.key
chown heresay:heresay /var/lib/heresay/mail.key
systemctl start heresay
```

Unpack the tarball alone and the house comes back with every account, ban and
setting intact — and not one readable message. That failure is silent: `.mail`
simply shows nothing where mail used to be.

**Which is why the annual restore test is now load-bearing rather than a
formality.** Restore into a scratch VM, log in as a test account, and read a
message. A backup you have never restored is a guess; a two-part backup you have
never restored is two guesses.

## 11. Hardening against automated attacks

Heresay already defends the auth path in-process: argon2id hashing, a per-IP
exponential login backoff, a **server-wide auth-failure ceiling** (cooldown +
creation-pause when failures spike across many IPs), and an ~10k **common-
password blocklist** at every password-set point. The recommended production
practice is to stop volumetric/distributed abuse *below the app* as well, at the
firewall and the web edge — defense in depth.

### fail2ban — ban offending IPs at the firewall

Both files ship in the repo (`deploy/install/fail2ban-heresay.{filter,jail}.conf`),
§1 installs the package and §5 copies them — so if you followed the checklist
this is already on. What follows is what the two files are, the two ways this
goes wrong, and how to check it is really working.

The daemon records every failed authentication (wrong password, bad recovery
code, dead resume token) in its **event log**, `/var/lib/heresay/events.jsonl` —
one JSON object per line, in this exact shape:

```json
{"ts":1785531749.27,"type":"auth_fail","ip":"203.0.113.9","phase":"login"}
```

⚠ **Domain facts live in that file and not in the journal**, which is why the
jail reads it directly (see CONFIGURING's "The event log").

**Then edit the jail** — `ignoreip` ships as loopback only:

```bash
sudoedit /etc/fail2ban/jail.d/heresay.conf     # add your own admin address
sudo systemctl restart fail2ban
```

⛔ **Two things make the difference between a working jail and a decorative one**,
and both are already baked into the shipped file — they are called out here
because both fail SILENTLY, and because anyone hand-writing a jail on the next
box will otherwise repeat them:

1. **`backend = polling`.** Ubuntu's default is `backend = systemd`, which reads
   the journal rather than the file. With the default, fail2ban starts fine,
   reports the jail as up, and never fires. There is no error to find.
2. **`port = 2323,http,https`.** The ban is scoped to the doors, and **22 is
   deliberately absent**, so a ban can never take away your SSH — which is what
   makes it safe to run this on a box you administer remotely. The other half of
   that safety is `ignoreip`, which is why it is worth the one edit above.

**Verify the filter against your real log** rather than trusting it — this is the
step that would have caught both mistakes above:

```bash
sudo fail2ban-regex /var/lib/heresay/events.jsonl \
     /etc/fail2ban/filter.d/heresay.conf
```

Look for a matched count equal to the number of `auth_fail` lines in the file
(`grep -c auth_fail /var/lib/heresay/events.jsonl`), and — the part people skip —
for **"Date template hits"** to be non-zero. A filter that matches lines but
cannot read their timestamps treats every hit as "now", and `findtime` stops
meaning anything. On the live box: 11 lines, 11 matched, dates parsed.

```bash
sudo fail2ban-client status heresay        # currently banned, total failed
```

The dials in the shipped jail: **6** failed auths from one address within
**10 minutes** earns a **1-hour** firewall ban. Raise `bantime` for repeat
offenders; the file is yours once it is in `/etc`.

fail2ban must be able to read the event log: it runs as root by default, which
can, but the file is `0600` and owned by the service user, so a non-root
fail2ban needs group access granted deliberately. The engine trims this file in
place (see `event_retention_days`) rather than rotating it, so there is no
logrotate interaction to configure.

(fail2ban catches the per-IP case; the app's own global auth-failure ceiling is
what covers a botnet spread thin across many IPs, each under `maxretry`.)

### Dependency CVE scan — monthly, and it tells you by failing

`heresay-depscan.timer` runs `deploy/bin/heresay-depscan.sh` monthly. It freezes
the daemon's own venv and runs `pip-audit` over the result.

⛔ **It audits what is INSTALLED, not what is pinned.** `make audit` on a
workstation checks `requirements.txt` — the file you intend to install. This
checks what is actually running on this box. The two agree until they don't, and
that is the moment worth hearing about.

⚠ **A finding is a FAILED UNIT.** pip-audit exits non-zero, the oneshot unit
inherits it, and the box says so somewhere an operator already looks:

```bash
systemctl --failed                          # heresay-depscan shows up here
journalctl -u heresay-depscan --no-pager    # ...and this says what it found
sudo systemctl start heresay-depscan        # run one now, by hand
```

Nothing here mails anybody and nothing here upgrades anything: an automatic
dependency bump on a live host is a bigger decision than a timer may take. The
audit tooling installs into its own venv (`/opt/heresay/.audit-venv`), never the
daemon's — the production venv holds the three runtime dependencies and nothing
else, and this keeps that true. That directory is excluded from the deploy rsync
so a push does not delete it.

### Caddy — rate-limit the web door

Cap connection/request rate at the reverse proxy so the WebSocket/HTTP front
can't be flooded. Caddy's rate limiting is provided by the `rate_limit` handler
(a plugin in current builds) — **verify the current syntax at
caddyserver.com**, as it has changed across versions. The shape is roughly:

```caddyfile
# inside the site block, gated to the /ws path — illustrative, confirm syntax
@ws path /ws
rate_limit @ws {
    zone ws { key {remote_host}  events 30  window 1m }
}
```

If your Caddy build lacks the plugin, a small `events`/`window` at the edge via
whatever mechanism it currently documents is enough; the goal is only to blunt
raw volume before it reaches the daemon.

### Why this is proportionate

These edge layers, together with the app's per-IP backoff, the global auth-
failure ceiling, and the common-password blocklist, are defense in depth. The
worst case if all of it were bypassed is small: one hobby account compromised —
mail is encrypted at rest, and room conversation is never stored at all. Guard
accordingly; don't over-fortify a house that keeps nothing.

---

## 12. Alerting — a phone buzz when something breaks

Everything above defends the house; this is how you find out that something
did not. Two services, both free, and neither of them holding anything but the
fact that a thing happened:

- **ntfy** for pushes — a topic you subscribe to in an app on your phone.
- **Healthchecks.io** for the backup **dead-man's-switch** — the one alert that
  has to fire when *nothing* happens.

⚠ **The scripts live in `/usr/local/bin`, not `/opt/heresay`, and that is the
one design decision here worth stating.** `make deploy` rsyncs `/opt/heresay`
with `--delete --delete-excluded`; anything of yours that lives there and is not
in the repo is removed on the next push. Alerting is host-ops rather than
engine, so it sits outside the rsync target entirely — a code deploy cannot
disturb it, and it keeps working while the daemon is the thing that is broken.
(The trade: `make deploy` does not update these either. ⛔ **`make deploy-alerts` is
what refreshes them** — it covers the scripts, the units and both drop-ins, and it is
the same command §5 names. ⚠ **Do NOT re-run §5's copy block for this**: that block
also copies `config.example.toml` over `/etc/heresay/config.toml`, which would discard
the values you filled in at §6.)

### The one file you must fill in

`/etc/heresay/notify.env`, 0600 (§6). ⛔ **Both values are secrets in the only
sense that matters — knowing one IS the permission.** The ntfy topic is the sole
auth on public ntfy.sh, so anyone who learns it reads every alert you send and
can post alerts that look like yours; the Healthchecks URL lets anyone ping your
dead-man's-switch, which means silencing the alarm that says your backups
stopped. The repo ships `notify.env.example` with `CHANGE-ME` placeholders and
nothing else, and §5's install **refuses to overwrite** an existing
`notify.env` — the same discipline as the fail2ban jail, which ships
loopback-only and never a real admin address.

### What fires, and when

| alert | trigger | how |
|---|---|---|
| **`<unit>` FAILED** | any of `heresay`, `heresay-depscan`, `heresay-backup`, `caddy`, `heresay-cert-copy` enters a failed state | systemd `OnFailure=` drop-in → `heresay-notify@%n.service` |
| **auth spray** | the server-wide auth-failure ceiling trips | `heresay-ceiling-watch.service` follows `events.jsonl` |
| **a report was filed** | somebody ran `.report` | `heresay-report-watch.service` follows `events.jsonl`. ⛔ **The nudge is a doorbell, never the content**: no reporter, no subject, no words, no count — the `report_filed` event has no such field to leak. Read it in-world with `.read_report`. |
| **disk ≥ 85%** | the filesystem holding `/var/lib/heresay` | `heresay-healthcheck.timer`, daily |
| **cert expires in ≤ 14d** | the cert the `:2323` door is actually serving | same timer |
| **host rebooted** | boot | `heresay-boot-notify.service` |
| **backup silence** | a nightly tarball was NOT written | Healthchecks.io notices the missing ping |

**Five drop-ins, one notifier.** `heresay-notify@.service` is a template taking
the failed unit's name as `%i`, so each of the five units needs only a two-line
`onfailure.conf` in its `.d/` directory. ⚠ **Drop-ins, not edits**: `caddy` is
not ours at all and the others are shipped by this repo and replaced by the next
deploy — a hand-edited unit is a change that vanishes the moment its file is
overwritten.

⛔ **The depscan alert is the reason that list includes a unit that "fails" on
purpose.** `heresay-depscan` exits non-zero when pip-audit finds a CVE (§11), so
its OnFailure drop-in turns a monthly scan nobody would remember to read into a
push notification.

**The ceiling watcher keys on the structured event, not on log prose.** The trip
is a fact in the engine's own store, so the watcher matches
`"type":"auth_ceiling","phase":"tripped"` in `/var/lib/heresay/events.jsonl` —
a stable contract that a reworded log line cannot break in silence. It debounces
for 30 minutes: a sustained distributed run is one buzz, not forty. ⚠ It uses
`tail -F` rather than `-f` because the engine TRIMS that file in place, which
replaces the inode; `-f` would go on watching a file nobody writes to any more
and report nothing, forever.

**The backup ping is the inverse of every other alert here.** `hcping.conf` adds
an `ExecStartPost=` to `heresay-backup.service`, which systemd runs **only if
the backup succeeded** — so the ping means "a tarball was written", not "the
timer fired". Create a check at healthchecks.io with a period of a day and a
grace of a few hours, put its ping URL in `notify.env`, and the alarm you get is
for the failure mode nothing else can catch: a box that is off, or a timer
somebody disabled, neither of which can send you anything.

### Verify the whole chain

```bash
sudo /usr/local/bin/heresay-notify.sh high "Heresay: test" "alerting works"
sudo systemctl start heresay-healthcheck.service    # silent = healthy
sudo systemctl start heresay-backup.service         # then: check turns green
systemctl status heresay-ceiling-watch              # active (running)
systemctl status heresay-report-watch               # active (running)
```

To rehearse a failure alert without breaking anything, start the template
directly with a made-up instance name:

```bash
sudo systemctl start heresay-notify@heresay.service
```

⚠ **`heresay-boot-notify` is `enable`d but never `--now`** — starting it by hand
would announce a boot that did not happen. It earns its place because of the
monthly maintenance reboot (§7): an unattended restart that comes back is
worth one line, and one that does not come back announces itself by this message
never arriving.
