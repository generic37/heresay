# Upgrading Heresay

What an operator has to **do** when crossing a version boundary. Everything here
was moved out of `docs/CONFIGURING.md`, which describes the house as it is now;
this file describes the steps between one version and the next.

⚠ **Read backwards to the version you are on.** Newest first. If you are
upgrading across several of these, the steps compose — none of them undoes
another — but apply them in the order they appear on disk, oldest first.

**Nothing here is automatic unless it says so.** Where a step is taken for you
(by `make deploy`, or by the daemon on its first boot) it is marked
**automatic**, and the entry is there so you know what changed under you and how
to roll it back.

Markers: ⛔ a hard rule or a thing not to do · ⚠ a caution worth reading twice.

---

## v1.62.15 — one paste into your Caddyfile: `:80` is announcing the server

⚠ **Two minutes, and it is the only step.** Nothing in the daemon changed.

**What is wrong.** Your `:443` site block carries `-Server`, so the page's responses announce
nothing — that has been true and checked. ⛔ **But port 80 does not go through that block.** Given
no explicit `http://` site, Caddy answers `:80` from its own **automatic redirect server**, which
inherits nothing from the block below it, `-Server` included. Measured from off the box on
2026-09-01: the redirect is correct (`308` → `https://…`) and it carries **`Server: Caddy`**.

⚠ **How much this matters, honestly: not much on its own.** It is one header on a response whose
only job is to say "go to https". ⛔ **What it costs is a claim** — this project says *no command
and no response prints a server banner*, and that was true of one door out of two.

**The step.** Add an explicit `http://` block to `/etc/caddy/Caddyfile`, above your site block —
it is in `deploy/install/Caddyfile.example`, with your own domain in place of the placeholder:

```
http://your.domain.example {
	header -Server
	redir https://your.domain.example{uri} 308
}
```

Then `sudo systemctl reload caddy`, and check both doors:

```bash
curl -sSI https://your.domain.example/ | grep -i '^server:'   # expect: nothing
curl -sSI http://your.domain.example/  | grep -i '^server:'   # expect: nothing
```

⚠ **The `308` is deliberate** — it is what Caddy's automatic server was already sending, and it
preserves the request method. ⛔ **Do not drop the redirect while adding the header:** `:80` must
still send browsers to `https`, and it must still answer ACME challenges, which this block leaves
untouched because Caddy handles them ahead of your routes.

⚠ **Nothing does this for you.** `make deploy` rsyncs the repo; it does not touch `/etc/caddy/`,
and it never has. The two `curl`s above are the whole verification: when both print nothing, you are
done.

⚠ **`caddy validate` says two things about this block and neither is a fault** — worth knowing
before they alarm you:
`INFO http.auto_https enabling automatic HTTP->HTTPS redirects {"server_name": "srv0"}` and
`WARN http.auto_https server is listening only on the HTTP port, so no automatic HTTPS will be
applied to this server {"server_name": "srv1", "http_port": 80}`.
⛔ **The WARN is the new block being described accurately**, not a complaint: `srv1` IS an
HTTP-only server, by design, so automatic HTTPS correctly does not apply to it.

---

## v1.61.0 — the staff level's default word is `staff`

⚠ **Nothing to do if your `house/theme.toml` names its own `[ranks] order`** —
which the shipped skeleton has always done, and which any house that renamed its
ranks necessarily does. Your ladder, your level words, unchanged. The rest of
this entry is for a house running on the ENGINE's built-in defaults.

**What changed.** The staff level's shipped display words are now `staff` /
`staff`, singular and plural. `staffer` is gone — one person is staff, several
people are staff — and the built-in ladder's middle rung is named `staff` to
match. Several engine lines were reworded so they read correctly with a mass
noun: `.gag`, `.ungag`, `.jail`, `.call_staff`, `.promote`, `.demote` and
`.examine` no longer put an article in front of a word an operator chose. Those
are cosmetic and need no action.

⛔ **The one step, and it is a REORG.** If you have no `theme.toml`, or one with
no `[ranks] order`, your ladder came from the engine and has just changed shape
— which means the rank fingerprint changes and **the reorg collapse runs**. Any
account stored as `staffer` holds a word the new ladder does not have, so it
lands at the bottom and **loses staff access**. The house still opens; the
demotion is logged as a count and shown in `.load_report`.

Two ways out, and take one BEFORE you deploy:

- **Keep your old ladder.** Write it down explicitly, which is where it should
  have been anyway:

  ```toml
  [ranks]
  order = ["resident", "staffer", "owner"]
  staff = "staffer"
  ```

  Nothing collapses, and your screens go on saying `staffer`.

- **Take the new ladder and re-promote.** `.promote` each staff account once
  after the restart. ⚠ The owner is named in `config.toml` and is never
  collapsed, so you always have somebody to run it.

**Optional, and worth the two minutes.** Copy the four `[levels.*]` blocks out of
`skel/house/theme.toml` into your own. Every value in them matches the built-in
default, so they change nothing — they are there so the mechanism is visible in a
file you can read, rather than met for the first time when a screen says
something odd. See CONFIGURING §"The `[levels.*]` blocks".

---

## v1.60.1 — one edit to `permissions.toml`, if v1.60.0's writer touched it

⚠ **Only if `--write-permissions` ran against your file under v1.60.0 or earlier.**
The old writer APPENDED new commands at the end, under a banner, one banner per
release. If you later wrote the same key in its proper place, the file holds that
key **twice** — a TOML duplicate-key error, which refuses the parse, which
**aborts the deploy**. ⚠ That is not hypothetical: it is what v1.60.0 did to the
reference instance's file.

**Open `/etc/heresay/permissions.toml` and look for a block like:**

```
# Added by Heresay 1.60.0 — new command(s), at their built-in defaults.
# Change the value to move one; the line stays either way.
ownerset = "owner"
```

**Delete the whole block** — the two comment lines and the appended value(s) —
keeping whichever copy of each key sits in its proper section. ⚠ The new writer
normalises this for you on the next `--write-permissions` (it strips the banners,
re-homes the commands under them and keeps the FIRST of a duplicated key), but a
file that cannot be PARSED has to be fixed by hand first, because `deploy.sh`
aborts before it gets that far.

**Nothing else.** The skeleton change (`skel/house/` now ships its own help pages,
agreement and reboot script) affects fresh installs only.

---

## v1.60.0 — the back-compat layer is deleted; the house's name moves

⛔ **THREE EDITS, AND MAKE THEM BEFORE YOU DEPLOY.** ⚡ **None of them is
boot-fatal** — the house opens either way — but until they are made it wears the
wrong name and carries a permissions line the engine no longer understands.

Everything v1.54.0 kept as a read-both alias is **gone**. There is no fall-back
read of any old spelling anywhere; where a file still uses one, the engine now
does what it does with any other word it does not know.

**1. `config.toml`, under `[world]` — add the house's name.**

```toml
[world]
instance_name = "Commons"      # or whatever your house is called
```

`instance_name` MOVED out of `house/theme.toml`, so that a fresh install is one
file to edit. ⚠ **Without it your house calls itself `the house`** — on
`.version`, the `.map` title, the `.examine` owner line, the two login notices
and the browser tab. Nothing breaks; it is simply the wrong name, everywhere,
until you set it.

**2. `permissions.toml` — `ownerset` → `ownerset`.**

The hidden `.ownerset` dispatch alias is gone, so a line naming it names no
command. It is skipped with a warning (`no command called 'ownerset' — line
ignored`) and `.ownerset` takes its seeded default, which on a ladder whose top
rung is the owner's is the same rank the line was asking for. ⚠ **If you had
deliberately assigned `.ownerset` to something OTHER than the owner rank,
that assignment is what you lose** — re-state it as `ownerset`.

**3. `house/theme.toml` — delete the `instance_name` line.**

It is ignored now, and warned about at every boot
(`instance_name has MOVED to config.toml (v1.60.0) and is IGNORED here`). This
file ships with the repo, so `make deploy-content` carries the fix.

**What else went, and who it can reach.**

| deleted | if a file still uses it |
|---|---|
| `wizard_only:` room header | ⛔ **unknown header key — the world refuses to load.** Rename it to `staff_only:` |
| `[keeper]` / `keeper_call_seconds` | the section is not read; `[staff] call_seconds` reverts to its default |
| `[moderation] arch` | not read — the house has no owner and **refuses to boot** |
| `[ranks] resident`/`wizard`/`arch` (pre-1.27 theme keys) | not read; the ladder falls back to the built-in default |
| `[ranks] floor` (the pre-v1.54 name for `resident`) | not read — an unknown key, so the anchor falls back to `order[0]` |
| `floor`/`arch`/`wizard` in `permissions.toml` | not a rank → ⛔ **refuses the boot**, naming `--write-permissions` as the fix |
| `{arch}` in `agreement.txt` | left on screen as literal `{arch}` — use `{owner}` |
| `#keeper` mailbox adoption | an orphaned `#keeper` box is no longer moved to `#staff`; its contents are simply not read |
| `level` in a schema-v2 `accounts.json` | not migrated — every account reads as the floor rank, and the owner re-promotes by hand |

⚠ **Commons needed only the three edits above.** Every other row was checked
against the live box and found not to apply: no `wizard_only` room, no `#keeper`
box, no `[keeper]` section, no pre-1.27 theme keys, a schema-v3 `accounts.json`.
Your house may differ — the table is the checklist.

⚡ **`{arch}` → `{owner}` in `agreement.txt` costs NO re-consent.** The version
is the sha256 of the RESOLVED text and both tokens resolve to the same word, so
the hashed bytes do not change and nobody is asked to agree again.

**The backup now covers the whole instance** — `/etc/heresay/` and
`/opt/heresay/house/` as well as `/var/lib/heresay/`. Nothing to do: the script
ships with `make deploy`. ⚠ It **refuses** if any of the three is missing rather
than writing a short tarball, so a failed backup unit after this release means
one of those paths is not where the script expects it.

---

## v1.58.0 — ⛔⛔ back the mail store up BEFORE this deploy

⛔ **ONE-TIME HAZARD, and it is destructive: `report_retention_days` drops
100 → 10 and the purge runs AT STARTUP.** Any report already in the box and older
than ten days is deleted on the first boot after the deploy, **with no capture
file behind it** — the capture is written at FILING, so it exists only for reports
filed from v1.58.0 onward.

**Take the backup first. It costs nothing if the box was empty:**

```
ssh <host> 'sudo tar -czf /root/reports-preflight-$(date -u +%Y%m%d).tar.gz \
  -C /var/lib/heresay mailboxes.json.d && sudo chmod 600 /root/reports-preflight-*.tar.gz'
```

⚠ That tarball is ciphertext: reading it back needs `mail.key` as well — the
ordinary two-input restore, unchanged.

**Then an ordinary `make deploy`.** No unit change, no `/etc` edit, no re-consent.
⚠ If you want the old behaviour, set `[mail] report_retention_days` yourself —
but read the reasoning first: the capture file is the record now, and a longer
number in the box only leaves an accusation readable after the evidence around it
has aged out of the event log.

---

## v1.57.0 — `make deploy` is NOT enough: the report watcher is a host script

⛔ **Three steps, and the second is the one a code deploy cannot do.** The nudge
that fires when a report is filed is an EXTERNAL watcher in `/usr/local/bin`,
outside the `/opt/heresay` rsync.

1. `make deploy` — the engine (the `report_filed` event).
2. **`make deploy-alerts`** — installs `heresay-report-watch.sh` beside the other
   alert scripts.
3. **Enable the unit once, by hand** — `sudo systemctl enable --now
   heresay-report-watch`. Full steps in `deploy/install/README.md` §12.

⚠ Nothing in `/etc` changes and there is no re-consent.

---

## v1.55.1 / v1.53.4 — two deploy actions, both times for the same reason

⚠ **The alert scripts live in `/usr/local/bin`, OUTSIDE the `/opt/heresay`
rsync**, deliberately: they are host-ops and must keep working while the daemon
is the thing that is broken. ⛔ So **plain `make deploy` does not ship them**.

- **v1.53.4** — (1) `make deploy` for the engine, (2) `make deploy-alerts` for
  the ceiling-watch freshness gate (it re-alerted on old trips after a restart).
- **v1.55.1** — (1) `make deploy` for the deploy-exclude fix, (2)
  `make deploy-alerts` **and** `systemctl restart heresay-ceiling-watch`.

⚠ **A rule rather than two notes:** any release that changes something under
`deploy/install/` or the alert scripts needs `make deploy-alerts` as well, and a
watcher whose SCRIPT changed needs its unit restarted.

---

## v1.54.0 — the genericization: levels, `owner`, and vocab renames

⛔ **A config edit is required BEFORE you restart, or the house will not boot.**
This release renames the engine's rank vocabulary and introduces the four-LEVEL
model (`newb` < `resident` < `staff` < `owner`). Do the config step below during
the deploy window — see [`OPERATIONS.md`](OPERATIONS.md) for the deploy sequence.

**1. Name the owner — REQUIRED.** `[moderation] arch` is now `[moderation] owner`
and has **no default**: it ships as the placeholder `owner = "[owner_name]"` and
the daemon **refuses to boot** until it names a real account.
- The old `arch` key is still read as a fall-back, so an **unedited** config keeps
  naming its account. But a config that never set `arch` (relying on the old
  `"Generic"` default) must now set `owner` explicitly.
- Optional new knob: `[moderation] landing_level` (`newb`/`resident`/`staff`,
  default `newb`) chooses which level new accounts land in. `owner` there is
  boot-fatal.

**2. Config section + key aliases (read-both, no action needed).** `[staff]`
replaces `[keeper]` and `staff_call_seconds` replaces `keeper_call_seconds`; both
old spellings are still read. `theme.toml`'s `[ranks] floor` anchor is now
`resident` (with `floor` accepted as an alias), and level display words move to
`[levels.<id>] singular`/`plural` (the old `[ranks] staff_plural` is still read).

**3. Rank-rename churn — usually none, but check.** The reorg-collapse fingerprint
covers `order` + the `resident`/`staff` anchors. **If your ladder's rank NAMES are
unchanged, nothing collapses** — a house whose `theme.toml` names its own rungs
keeps every one of them. ⚠ A house that had adopted the OLD generic default rank
names (`staff`/`owner` as *rungs*) will see those ranks read as unknown and
collapse to the bottom of their band — **re-promote by hand** afterwards. The
startup log and `.load_report` say how many moved.

**4. Renamed tokens (automatic).** Room header `wizard_only:` → `staff_only:`
(old spelling still accepted). The staff letterbox on-disk key `#keeper` → `#staff`
(an orphaned `#keeper` box is read as `#staff` on load). The event type
`wizard_action` → `staff_action` (`.staff_actions` still reads old rows; they age
out within the retention window). The command `.ownerset` → `.ownerset`, with
`.ownerset` kept as an alias.

⚠ **Back up `events.jsonl` and the mail store before the deploy** as usual — the
event-type and box-key renames are forward-only, and the backup is your rollback.

## v1.52.5 — mail and `.revtell` split into one file per box

**Automatic, on the first boot.** An existing `mailboxes.json` is split into
`mailboxes.json.d/` (one file per mailbox, named for the hex-encoded mailbox key)
and the original is **renamed** to `mailboxes.json.pre-split`. `revtells.json`
goes the same way, into `revtells.json.d/` with the same `.pre-split` rename.

- **Nothing is deleted**, and nothing is re-encrypted — the ciphertext is carried
  across byte for byte with its timestamp and its `read` flag.
- **To roll back**, put the `.pre-split` file back under its original name and
  remove the `.d/` directory.
- The directory is `0700` and each file `0600`.
- A schema v1 file migrates as it is split, its messages treated as **unread**.
- `mail_file` and `revtell_file` in `[storage]` are unchanged: they now name the
  store rather than a single file, and the directory is derived from the value,
  so there is no second key to set.

⚠ **If both shapes exist, the split boxes win.** A legacy file sitting beside a
populated `.d/` directory means the migration already ran and something put the
old one back — a restore, an rsync, a hand copy. It is left alone and named in a
warning on every boot; re-importing it would resurrect deleted mail.

## v1.49.0 — two steps a code deploy does NOT do

*(Moved here from the install checklist on 2026-08-09: a first install gets both
of these from the shipped files, so only an existing house needs the steps.)*

`make deploy` ships code. It does **not** touch the installed systemd unit, and
it has never touched your Caddyfile. v1.49.0 changes both, so both need a hand.

### The unit — resource ceilings and hardening

```bash
sudo cp /opt/heresay/deploy/install/heresay.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart heresay
systemctl status heresay && journalctl -u heresay -n 20   # ⛔ CONFIRM IT STARTED
```

⛔ **`SystemCallFilter=@system-service` is the one line here that can stop a
working daemon from starting.** Python with argon2-cffi and cryptography is
normally fine under it, but that depends on this box's libc, kernel and wheels,
and it could not be verified from the development machine. **If the daemon fails
to start, comment out `SystemCallFilter` and `SystemCallErrorNumber` and
`daemon-reload` again** — nothing else in the unit depends on them, and a daemon
running without a syscall filter beats one that will not run.

⚠ **`MemoryMax=1G` is a tunable, and too low is worse than absent.** argon2id is
memory-hard on purpose — 64 MiB × 4 lanes, transient, per hash in flight — so a
burst of logins really does allocate hundreds of megabytes for a moment. Size it
to your box. Being OOM-killed mid-conversation is a worse outcome than no cap.

Measured with `systemd-analyze security heresay`:

| | exposure |
|---|---|
| before (v1.48.0) | **6.4 MEDIUM** 😐 |
| after (v1.49.0) | **1.7 OK** 🙂 |

Run it yourself after installing; the score is a useful thing to keep an eye on.

### Caddy — security headers and a body cap

The repo ships `deploy/install/Caddyfile.example`; **your live
`/etc/caddy/Caddyfile` is yours**, so copy the new `header { … }` and
`request_body { … }` blocks into it rather than overwriting the file.

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
```

⛔ **Then open the web client and confirm it still loads and connects.** A
Content-Security-Policy is the one part of this deployment that can white-screen
the page, and the browser console is where it says so.

⚠ **If the page loads but never connects, the WebSocket is being blocked by
`connect-src 'self'`.** Swap in the commented fallback line with your real
domain — `connect-src 'self' wss://your-domain.example`. (`'self'` covers
same-origin `wss://` under CSP Level 3 and every current browser implements it,
which is why it is the default; this is the escape hatch, not the expectation.)

💡 You can rehearse the whole thing on localhost first: `deploy/Caddyfile.dev`
carries the identical policy, so `caddy run --config deploy/Caddyfile.dev
--adapter caddyfile` and a browser will tell you whether the CSP is right before
it is anywhere near the live site.

## v1.48.0 — `mail.key` is no longer in the backup tarball

⛔ **Action required, once, by hand.** The tarball used to contain `mail.key`,
which meant the ciphertext and the key that opens it travelled together in the
one file that leaves the machine. It does not any more.

The key needs **one deliberate copy, made by hand, once** — it never rotates —
stored somewhere the tarballs are not. The exact step is in
`deploy/install/README.md` under "The one-time `mail.key` backup".

⚠ **Until you make that copy the key exists in exactly one place: the server.**

⚠ **A restore now takes two inputs, and getting it wrong is silent:** unpack the
tarball alone and every account, ban and setting returns, with not one readable
message.

⚠ **Your older tarballs still contain the key.** Treat them as secrets, or
destroy them.

## v1.46.0 — three unit directives the countdown needs

**Already in the shipped `heresay.service`. If you installed an older copy,
update it**, or the shutdown countdown will not work — and two of the three fail
silently.

| directive | value | why |
|---|---|---|
| `Restart=` | `on-failure` | so `--shutdown` actually STAYS down. Under `always` systemd brings it straight back |
| `TimeoutStopSec=` | `300` | so a `systemctl stop` landing mid-countdown is waited for, not killed |
| `RestrictAddressFamilies=` | must include `AF_UNIX` | without it the control socket cannot bind — the house runs, but the countdown silently does not |

## v1.44.0 — the reserved-name list stopped being hardcoded

Two names used to be reserved from registration by the engine itself. They are
not any more: **every rank name in your `[ranks] order` is reserved**, which
covers them on a house that uses those words, and a house that calls its ranks
something else has no reason to reserve another house's vocabulary.

⚠ **If you renamed your ranks and still want the old words held**, list them in
`forbidden_names.txt`. Nothing in that file can *unreserve* a rank name.

## v1.43.0 — `permissions.toml` values must be rank names

⛔ **A value that is not a rank in your `[ranks] order` refuses the boot.**

v1.38.0 also accepted the role words `newb`/`floor`/`staff`/`arch`; v1.43.0 does
not. Two vocabularies for one field made the file impossible to read straight —
one of them was a word that appears nowhere in your `theme.toml`. The roles still
exist inside the engine as the built-in defaults; they are turned into your rank
names when the file is written, so you never see one.

**The migration is `--write-permissions`, which your deploy already runs.** It
rewrites any leftover role word as the rank name it resolves to, preserving every
comment, value, and line ordering — a canonicalization, not a reset, and
idempotent. `deploy.sh` runs it **before** the restart and aborts the deploy if
it fails, so a file the CLI cannot make sense of surfaces there, with the old
code still serving.

⚠ **The trade:** rank names must now track `[ranks]`. Renaming a rank that a
permissions line uses means editing `permissions.toml` in the same reboot.

## v1.41.0 — `newb` is derived, not configured

**No action needed.** A `newb = "..."` line in `theme.toml` is warned about and
ignored; the bottom rank is `order[0]`. Remove the line when convenient.

Deriving it means nothing can sit *below* the newb rank, which is what stopped a
ladder change from accidentally *raising* somebody.

## v1.38.0 — rank zones, and the reorg collapse

**Upgrading itself does not move anyone.** The collapse only runs when the
ladder's *shape* actually changed: a fingerprint of `order` plus the three anchors
is stored beside the accounts and compared at every boot, and the first load after
upgrading has no stored fingerprint — it records the shape and collapses nobody.

⚠ **After that, changing the shape of `[ranks]` re-places every account** at the
bottom of its tier. See the reorg-collapse warning in `docs/CONFIGURING.md` §3
before you edit the ladder.

## v1.37.0 — the command→rank table moved to `permissions.toml`

**Upgrading from v1.28–v1.36?** The table used to live inside `state.json`.
`--write-permissions` (which your deploy runs) writes `permissions.toml` from it
— **your assignments come across exactly** — and the daemon drops the old block
on its next save. Until you run it, the house keeps using the block and keeps
writing it back, so nothing is lost by deploying first and running it later.

That handover also fixes a real bug: while the table lived in `state.json`, an
edit made with the daemon running was silently overwritten the next time
anything else wrote that file. Nothing can eat an edit now, because the daemon
does not write this one at all.

## v1.34.1 — existing mail is announced once more

**Automatic.** Mail written before the `read` flag existed carries no record of
whether it was read, so it is migrated **unread** rather than guessed at: the
first login after the upgrade tells everyone still holding mail about it one more
time, and reading it clears it for good.

Guessing "read" would have silently hidden genuinely unread mail from the person
it was addressed to; guessing "unread" costs one notice, which reading clears.

Nothing is deleted and nothing is re-encrypted — the file gains a `read: false`
per message and is stamped version 2 on load.

## v1.30.1 — fail2ban reads the event log, not the journal

⛔ **Action required on existing installs.** The auth-failure line is no longer
in the journal, so the jail must read `events.jsonl` instead.
`deploy/install/README.md` carries the updated filter, and the repo ships it.

⚠ **An un-updated jail keeps running and silently stops matching** — which looks
exactly like "nothing is attacking us".

Replace both files, and ⛔ **remove the old `heresay-auth` pair if you have it** —
leaving it behind means fail2ban loads a stale jail that matches nothing, beside
the working one:

```bash
SRC=/opt/heresay/deploy/install
sudo cp $SRC/fail2ban-heresay.filter.conf /etc/fail2ban/filter.d/heresay.conf
sudo cp $SRC/fail2ban-heresay.jail.conf   /etc/fail2ban/jail.d/heresay.conf
sudo rm -f /etc/fail2ban/filter.d/heresay-auth.conf \
           /etc/fail2ban/jail.d/heresay-auth.conf
sudo systemctl restart fail2ban
```

⚠ Two more things that fail silently in the same place: the jail needs
`backend = polling` (the distribution default reads the journal, where the domain
facts no longer are), and its `port =` line should leave **22 absent**, so a ban
can never take away your own SSH.

## v1.25.0 / v1.17.0 — `defaults.txt` is gone

Its three keys — `dim`, `time` and `helpstyle` — are `.ownerset` house
settings now, stored in `state.json` and changed in-world without a restart. The
built-in values are what the shipped file used to say (`off`, `off`, `long`), so
out of the box nothing changed.

`[storage] defaults_file` is ignored rather than rejected: an old config carrying
it still loads.

## v1.17.0 — the content root, the `[tls]` rename, and two retired headers

- **`house_dir` is the content root.** Set it and every content file is found
  automatically. ⚠ It defaults **relative to the config file**, so an
  `/etc`-based deployment that omits it looks for `/etc/heresay/house` and will
  not find your world — set it explicitly in production. An *absolute* per-file
  override still wins outright, which is what lets an older config full of
  absolute content paths keep working with nothing added but `house_dir` itself.
- **`[tls]` was renamed** to the section documented in `docs/CONFIGURING.md` §4.
- **The `entry:` and `jail:` room-file headers are retired.** Which room is the
  front door is a property of the deployment, not of a room file, so they are
  named in `config.toml [world]` now: `entry_room` (required) and `jail_room`
  (optional). A leftover header is accepted, ignored, and warned about at
  startup — never fatal.
- ⛔ **`entry_room` is required, and there is no fallback.** An unset
  `entry_room`, one naming a room that does not exist, or a world directory with
  no room files, refuses the boot. (The hardcoded `void` room a house used to
  fall back into is gone: it turned a config mistake into a house that *looked*
  open and was not.)

Older config keys that are simply ignored rather than rejected: `rooms_file`,
`defaults_file`, and `start_room`. ⛔ **`start_room` is not honoured as
`entry_room`** — it has been ignored for many releases, and reviving a stale key
as authoritative would be worse than failing loudly.
