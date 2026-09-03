# Heresay — operations

A practical "something's wrong, where do I look" reference for the live server.
Paths assume the standard deploy: the daemon under systemd as `heresay`, state in
`/var/lib/heresay`, code in `/opt/heresay`, the web edge via Caddy. Run as root on
the host. `commons.example.net` stands in for your own domain throughout, as it
does in the install checklist.

## Triage order

When something feels off, work down this list:

0. **Did I get an alert?** → the push on your phone names what broke; §1 says what
   each one means. Most of the time this is the whole of triage.
1. **Daemon up?** → `systemctl status heresay`
2. **Abuse / attack?** → `events.jsonl` + `fail2ban-client status heresay`
3. **Site reachable?** → Caddy + `curl`
4. **Monthly dependency alert?** → `systemctl --failed` (it also pushes — §1)

---

## 1. Alerts — how you find out

You should not have to go looking. Two channels, both reaching the phone:

- **ntfy** — a push to the phone app, for anything the box can notice itself.
- **Healthchecks.io** — push **and email**, for the one thing the box *cannot*
  report: the backup not running. It expects a ping each night and raises the
  alarm on the silence, so a host that is off or a timer somebody disabled still
  reaches you.

What fires:

| you get | because |
|---|---|
| `Heresay: <unit> FAILED` | `heresay`, `heresay-depscan`, `heresay-backup`, `caddy` or `heresay-cert-copy` entered a failed state (systemd `OnFailure` drop-in on each of the five) |
| `Heresay: auth spray` | the server-wide auth-failure ceiling tripped — `heresay-ceiling-watch.service` follows `events.jsonl` and pushes once, then holds off 30 minutes |
| `Heresay: new report` | somebody filed a `.report` — `heresay-report-watch.service` follows `events.jsonl` and pushes one nudge. ⛔ **A doorbell, never the content:** no reporter, no accused, not one word of the text — and it could not carry them, because `report_filed` is `{ts, type}`. Read it in-world with `.read_report` |
| `Heresay: disk/state` | the state filesystem is ≥ 85% full **or** the state dir grew past its size cap — `heresay-healthcheck.timer`, daily; the message names `accounts.json` and `ban_history.json` sizes so you can see whether the store is the cause |
| `Heresay: cert expires in Nd` | the cert the `:2323` door is actually serving expires in ≤ 14 days — same timer |
| `Heresay: <host> rebooted` | the host booted, and **both services are up** — the body reads `heresay active, caddy active`. `heresay-boot-notify.service` |
| `Heresay: <host> rebooted — SERVICE DOWN` | the host booted and one of the two did NOT come back; the body names the states, and the priority is `max` rather than `high`. ⚠ It waits up to 30s first, so a slow start is not reported as a failure |
| a Healthchecks alert | **no** backup ping arrived in the expected window |

The alert names the unit, so the next step is almost always
`systemctl status <unit>` and `journalctl -u <unit> -n 50`.

**Is the alerting itself alive?**

```
systemctl is-active heresay-ceiling-watch        # active — it is following the log
systemctl is-active heresay-report-watch         # active — it is following the log
systemctl list-timers | grep heresay             # backup, maintenance, cert-copy, depscan, healthcheck
/usr/local/bin/heresay-notify.sh high "Heresay: test" "if this buzzes, alerting works"
```

The test push is the only end-to-end proof; the rest tells you the machinery is
loaded. Healthchecks' own dashboard shows when the last backup ping landed.

**The secrets** live in `/etc/heresay/notify.env`, root-only (0600):
`NTFY_URL` and `HC_BACKUP_URL`. ⚠ The ntfy topic **is** the authentication —
anyone who knows it can read your alerts and post fake ones — so treat that file
the way you treat `mail.key`. Both scripts skip quietly if it is missing or
empty, so alerting failing off is silent: that is what the test push is for.

⚠ **Priority matters.** ntfy only buzzes a phone at `high` or `max`; a `default`
priority arrives silently in the app. Everything shipped here uses `high`, and
unit failures use `max` — if you add an alert of your own, set the priority
explicitly or you will not hear it.

Full detail — what installs where, and the two systemd gotchas — is
`deploy/install/README.md` §12.

### The disk check also watches the state store (audit-3 F3)

`heresay-healthcheck` (daily) already warns when the state filesystem is ≥ 85%
full. Since v1.53.1 the same disk check **also reports the state-dir size and
names `accounts.json` and `ban_history.json` by size**, and adds a second trigger
— **`STATE_WARN_MB` (500 MB)** — that fires when the store itself grows large
*before* the whole disk fills. That is the answer to F3: `accounts.json` and
`ban_history.json` grow unbounded in principle, and a size cap on a durable record
(who has an account; every ban ever placed) would trade real function to bound a
number that is fine almost always — so the box **watches** instead of capping. A
signup flood or a runaway ban history now reads as "the store grew", not a bare
"disk filling". Tunables: `DISK_WARN` (85), `STATE_WARN_MB` (500), `STATE_DIR`.

⚠ **No new unit to install** — `heresay-healthcheck.timer` already runs it daily;
this is a change to the existing script. ⛔ **But `make deploy` does NOT pick it
up:** the alert scripts install to `/usr/local/bin`, deliberately *outside* the
`/opt/heresay` rsync target (so a code push can never disturb the alerting). The
one command that refreshes them is:

```bash
make deploy-alerts        # refresh the alerting scripts AND their units
```

Run it whenever an alert script **or an alerting unit** changed — and only then.
It carries the current `heresay-notify` / `hcping` / `ceiling-watch` /
`report-watch` / `healthcheck` scripts to the box and `install`s them into
`/usr/local/bin` at mode 0755, and the alerting **units** — `notify@`,
`boot-notify`, `ceiling-watch`, `report-watch`, `healthcheck.{service,timer}`,
`depscan.{service,timer}` — plus the `OnFailure` drop-ins and the backup's
`hcping.conf` into `/etc/systemd/system/` at 0644, over the same host machinery
as `make deploy`. Idempotent, and **it writes only what differs**: every file is
compared first and the run prints `new`, `changed` or `unchanged` per path.

⚠ **It does `daemon-reload` when something changed**, and restarts
`heresay-ceiling-watch` / `heresay-report-watch` **only if their own script or
unit changed and they are already running** — those two follow the log and hold
their script in memory, so nothing else needs it. ⛔ **It never enables or starts
anything**: a stopped watcher stays stopped. ⛔ **And it does not touch the
daemon** — no `heresay` restart, no `--write-permissions`, nothing written under
`/opt/heresay` or `/var/lib/heresay`.

⛔ **`heresay.service` is NOT in that set, and NO deploy path carries it.** The
daemon's own unit is installed **by hand** at first install and changed **by hand**
afterwards — `make deploy` writes to `/opt/heresay` and never to
`/etc/systemd/system`, and `make deploy-alerts` refuses the name outright.
⚠ **That is a choice, not a gap the guards forgot to close:** the unit is how the
daemon is supervised — its restart policy, its resource ceilings, its sandboxing —
so changing it should cost an operator's attention rather than arrive inside a
refresh that advertises itself as touching nothing.

**Changing it is three steps, in this order:**

```bash
scp deploy/install/heresay.service <your-host>:/tmp/
ssh <your-host> 'sudo install -m 0644 -o root -g root /tmp/heresay.service \
                   /etc/systemd/system/heresay.service && sudo systemctl daemon-reload'
make deploy        # warns the house, then restarts into the new unit
```

⚠ **`daemon-reload` alone does not move a running daemon onto the new unit** — the
restart is what does, and `make deploy`'s countdown is what warns the house first.
⛔ **Do not `systemctl restart heresay` by hand to pick it up**: that drops
everyone mid-conversation with no warning, which is the whole thing the countdown
exists to prevent.

⚠ **The units that DO something rather than say something** — backup, cert-copy,
maintenance — are not alerting and are not carried here either; they are
hand-installed the same way.

(Under the hood it is the same `sudo install … ` the first-time install does —
see `deploy/install/README.md` §5.) Then prove it:

```bash
ssh <your-host> 'sudo systemctl start heresay-healthcheck.service'   # silent = healthy
ssh <your-host> 'journalctl -u heresay-healthcheck -n 5'
```

⛔ **`make deploy-alerts` ships the UNIT; it does not ENABLE it.** For the
two watchers that *follow* the log — `heresay-ceiling-watch` and
`heresay-report-watch` — the service file now arrives with every alerts refresh,
but **enabling it is still a manual step**, deliberately: a refresh that quietly
started a unit would change what the box does without being asked.
A box that crossed the v1.57.0 boundary by a plain `make deploy` has the report
watcher's script on disk and its unit not running, which looks exactly like a
house where nobody has filed a report:

```bash
ssh <your-host> 'sudo systemctl enable --now heresay-report-watch.service'
ssh <your-host> 'systemctl is-active heresay-report-watch'    # active
```

⚠ **Nothing is lost while it is off** — the report itself is in the box and its
capture file is on disk either way. What is lost is the nudge, so a report can
sit unread for a week, which is the whole thing v1.57.0 existed to stop.

## 2. The daemon itself

- `systemctl status heresay` — running, uptime, memory vs the 1 GB cap, recent
  restarts. A crash auto-restarts (`Restart=on-failure`).
- `journalctl -u heresay -n 50 --no-pager` — the process log: startup counts and
  tracebacks. **Facts only, never message content** (the covenant), so a traceback
  here is safe to read and share.
- `journalctl -u heresay -f` — follow it live.
- Keeps restarting? `journalctl -u heresay -n 200` for the repeating traceback.

## 3. The security record — `/var/lib/heresay/events.jsonl`

The structured log of domain facts: connections, auth failures, refusals, bans,
staff actions. Holds event types, IP addresses, and account names — **never a word
anyone said.** This is your main forensic source.

- Live: `tail -f /var/lib/heresay/events.jsonl`
- Failed logins: `grep '"auth_fail"' /var/lib/heresay/events.jsonl | tail`
- Refusals / bans: `grep -E '"refused"|"ban' /var/lib/heresay/events.jsonl | tail`
- The door going into throttle: `grep '"auth_ceiling"' …` — `"phase":"tripped"`
  when the server-wide ceiling engaged, `"cleared"` when it lapsed. This is the
  line the phone alert comes from, and it carries no address (the ceiling is
  server-wide, so there is no single peer it is about).
- Retention is bounded by `event_retention_days` in config; old facts age out.

⛔ **Preserving the record around a report — do this FIRST, before anything else.** The trim is
silent and the window is short (10 days by default), so the facts that would let anyone check a
report can be gone before it has been handled. One command, and it costs nothing if it turns out to
be nothing:

```bash
mkdir -p ~/heresay-preserve
cp /var/lib/heresay/events.jsonl ~/heresay-preserve/events-$(date +%F).jsonl
```

⚠ **If mail or a report is implicated, set that night's backup tarball aside too** — the report
capture files and the mail store live in it, and the nightly rotation will otherwise take it. ⚠ The
tarball is ciphertext for the mail: reading it back needs `mail.key`, which is the ordinary
two-input restore.

⚠ This is worth doing for ANY report you may have to answer for later, not only a serious one. The
capture file the daemon writes at filing (`<state_dir>/reports/`) already holds the report and the
event rows about both accounts — this preserves the wider record around it.
- In-world (as owner), the same facts as counts: `.security` (failed logins,
  timeouts, **auth ceiling trips**, password resets, takeovers, self-deletions,
  rejected frames — counts only, no names or addresses), `.staff_actions`,
  `.load_report`.

## 4. Abuse or an attacker

- `fail2ban-client status heresay` — currently banned, totals.
- `fail2ban-client status` — all jails (heresay + the default sshd one).
- Ban by hand (firewall): `fail2ban-client set heresay banip <ip>`
- Unban: `fail2ban-client set heresay unbanip <ip>`
- In-world (as owner): `.ban <name>` (the account), `.ban_ip <name>` (their address —
  shows who else it catches first), `.gag` / `.jail` / `.boot` for individuals.
- **Emergency levers (in-world, owner):** `.logins off` stops new logins (people
  already in stay); `.logoutall` clears the whole house. Reopen with `.logins on`.
- Automatic, no action needed: per-IP exponential backoff, per-IP connection caps,
  and a server-wide auth-failure ceiling all throttle password-guessing on their own.

## 5. The web edge (can't connect / TLS / headers)

- `systemctl status caddy` + `journalctl -u caddy -n 50` — Caddy terminates TLS and
  proxies the web door (`/ws`).
- `curl -sI https://YOUR-DOMAIN/` — is it up, are the security headers
  present (`content-security-policy`, `strict-transport-security`).
- Raw TLS door: `openssl s_client -connect YOUR-DOMAIN:2323`
- Cert auto-renew: `systemctl status heresay-cert-copy.timer` (copies Caddy's
  Let's Encrypt cert to the daemon's own TLS door daily).

## 6. Dependency vulnerabilities (automated)

The monthly `heresay-depscan` timer runs `pip-audit` and **fails loudly** on a find.
It audits what is INSTALLED in the daemon's venv, not what `requirements.txt`
pins — the two agree until they do not, and that is the moment worth hearing about.

- **It pushes to your phone** (`Heresay: heresay-depscan FAILED`): the unit fails
  on a finding, and its `OnFailure` drop-in turns that into an alert. It also
  still shows in `systemctl --failed`.
- Then `journalctl -u heresay-depscan -n 30` names the package, the CVE, and the
  fixed version.
- Run it now: `systemctl start heresay-depscan.service`, then check the journal.
- Fix: bump the pinned version (and its `--hash`) in `requirements.txt`, redeploy.
- When it next runs: `systemctl list-timers | grep heresay`.

## 7. Resources

- Memory against the 1 GB `MemoryMax`: shown in `systemctl status heresay`; an
  OOM-kill appears in the journal.
- Disk: `df -h`; watch `/var/lib/heresay` (the encrypted mail/revtell box
  directories and the events log live there).

## 8. Graceful restart / shutdown (with warning to users)

From the box, over the control socket (uses the daemon's own config):

- Announce: `/opt/heresay/.venv/bin/python -m heresay -c /etc/heresay/config.toml --announce "back in a few"`
- Timed shutdown: `... --shutdown <minutes>` — counts down with warnings, then exits
  and stays down. `... --shutdown cancel` calls it off.
- A plain `systemctl restart heresay` or `reboot` is fine too; users simply
  reconnect (the daemon holds presence briefly and remembered browsers resume).

## 9. A suspected *code* bug or vulnerability

Not something you fix at the shell. Take it to your development environment and run
the usual loop: reproduce → write a failing test → fix on a branch → verify (suite
green) → merge → deploy. The failing test comes first for a reason — it is what
tells you the fix worked, and what stops the bug coming back later.

Before assuming it is new: some low-severity items are **known and deliberately
accepted**, and the reasoning for each lives beside the code that implements it,
with a test pinning the ones that must not regress. If the behaviour you are
looking at is explained there as a choice, it is a choice; if nothing explains it,
treat it as new.

## 10. Operational logging

The only logging is operational — never message content, never mail bodies,
never command arguments — and since **v1.30.0** it lives in two places with a
hard line between them.

**Domain facts → the engine's own event log.** `<state_dir>/events.jsonl`,
`0600`, one JSON object per line, **twelve closed types**: connections
(open/close/resume + IP), logins (name + address, the one type with both), auth
failures, login timeouts, refusals with a reason,
flood disconnects, session takeovers, account-security events (recovery reset,
codes regenerated, and re-consent — the last recorded without a name),
self-deletions, staff calls, staff mail, and staff actions
(actor, action, target). The engine writes it, trims it and reads it back, so any
subset can be surfaced in-world. Three views do:
**`.staff_actions`** (owner) lists the staff actions newest-first;
**`.security`** (owner) shows six counts over the last day and nothing else
— no names, no addresses, no times; **`.staff_calls`** (staff) tallies who has
called or mailed staff, busiest first. Portable by design: no systemd, no
`journalctl`, same behaviour wherever
the engine runs. Retention is `[world] event_retention_days` (**10**), one window
for every type, trimmed in-process on append and at startup — nothing to install.
The two counting views have their own windows (`security_window_hours`,
`staff_calls_window_hours`, 24 each), each capped at the retention.

**Process output → stdout / journald.** The startup lines (listeners, rooms,
help topics), malformed-config warnings, uncaught tracebacks, systemd lifecycle.
Things you read while the process is misbehaving. The load half of that — the
counts, the one-way passages and every warning — is *also* captured into memory
at boot and shown by `.load_report`; the journal still receives all of it, so
that capture is additional rather than a redirection, and nothing about it is
written to disk.

**No fact is written to both**, and since v1.30.1 the journal holds **no domain
fact at all** — no resident's name, no connecting address. That is the whole
point of the split: a second copy would drift the first time someone edited one
side, and a name in two places with two retention windows is a name kept twice.

**Journal retention (production):** journald on the server keeps entries for
**7 days** only — a drop-in at `/etc/systemd/journald.conf.d/retention.conf`
(`[Journal]` / `MaxRetentionSec=7day`), applied with `systemctl restart
systemd-journald`. Not part of `make deploy` (it's host config, not repo code);
trim existing history once with `journalctl --vacuum-time=7d`. Note that since
v1.30.0 — completed in v1.30.1 — the journal no longer holds connection IPs,
account names or staff-action lines: those are all in the event log and fade on
**its** window instead.

## 11. Monthly maintenance (reference instance's server)

*This and the next section document how the **reference instance** (Commons,
on one small VPS) is operated — a worked example, not an engine requirement. The
engine needs none of it; the mechanics generalize, and the scripts ship generic
under `deploy/install/`.*

`deploy/install/heresay-maintenance.sh` — installed to
`/usr/local/sbin/heresay-maintenance` (root, 0755) and fired by
`heresay-maintenance.timer` on the **1st of each month at 09:10 UTC**
(≈4-5am US Eastern, the quietest hour the rooms have). `Persistent=true`, so a
box that was off at the time runs it once when it comes back.

Each run, in order:

1. **Patch** — `apt-get update`, `full-upgrade`, `autoremove --purge`.
2. **Prune** — `journalctl --vacuum-time=7d`, belt to the retention drop-in's
   braces above; a line reporting the **event log's** size and event count (it
   trims itself on `event_retention_days`, so this is a health check, not a
   prune — a count and a size, never a line out of it); and a **backup health
   check**: how many tarballs are in
   `/var/backups/heresay`, and how old the newest is. Anything older than 48h,
   an empty directory, or a missing directory is flagged `[WARNING]` in the
   report.
3. **Report** — one short block appended to **`/var/log/heresay-maintenance.log`**
   (0640, root): date, packages upgraded/removed counts, journal vacuum result,
   backup freshness, `df -h /` one-liner, uptime, and whether a reboot follows.
   **Facts only** — no room text passes through the script, and nothing it
   writes could contain any. The log self-trims to the last ~24 runs.
4. **Reboot** — always, unconditionally, for a clean kernel and a clean slate.
   Nothing is lost: by covenant the world holds nothing that survives a restart
   (rooms empty; locks, gags, topics and `.rev` all evaporate), and heresay and
   caddy both come back on their own.

```bash
systemctl list-timers | grep heresay-maintenance  # when it next runs
tail -40 /var/log/heresay-maintenance.log         # what the last runs did
/usr/local/sbin/heresay-maintenance --no-reboot   # a trial run, no reboot
```

`--no-reboot` runs steps 1-3 and stops — the only way to exercise it without
bouncing the box.

Server-side maintenance only *checks* that backups are being made and are
fresh — it cannot get them off the machine, and a backup that lives only on the
box it backs up is not a backup. Getting them off is the weekly pull below.

## 12. Weekly backup pull (reference instance, off-server copy)

*Also the reference instance's operations — how the house's encrypted state is
pulled off its VPS to a second machine. Your own deployment will want an
equivalent, however you arrange it.*

The pull runs itself. A Windows scheduled task,
**`CommonsBackupPull`**, runs **daily at 3:00 AM local** as the current user
(no elevation) and invokes the WSL wrapper:

```
wsl.exe -d Ubuntu-24.04 -- bash -lc "~/heresay/scripts/fetch-backup-cron.sh"
```

⚠ **The key has to work from a bare environment.** The task runs with no agent
and no profile, so the key at `~/.ssh/id_ed25519_talker` is passphrase-free and
named by `~/.ssh/config`. Test it the way the task runs it:

```
wsl.exe -d Ubuntu-24.04 -- bash -lc "ssh -o BatchMode=yes commons true"
```

The scheduling lives on the Windows side because WSL has no always-running init
to hold a cron, while Task Scheduler survives reboots — and, crucially, has
**"run as soon as possible after a scheduled start is missed"**
(`-StartWhenAvailable`). A PC that was off at 3 AM catches up when it is next
available, which is the failure mode an offsite copy has to survive. It is also
set to run only when a network is available, and to skip a new run if one is
somehow still going. ⛔ **There is no boot trigger, deliberately** —
`StartWhenAvailable` covers the same case without firing on every boot.

`scripts/fetch-backup-cron.sh` does the work. It asks the server for the newest
tarball, and **if that file is already here and opens, it stops** — so it is safe
to run as often as you like, and running it twice in a row does the work once.
Otherwise it copies, checks the copy opens, prunes the repo's `backups/` to the
newest **8** tarballs, and appends one line to **`backups/fetch.log`** (trimmed to
50). It resolves that directory **relative to the repo**, so renaming the local
checkout never breaks it. `backups/` is gitignored.

⚠ **Only `heresay-*.tar.gz` is counted or pruned.** The two `talker-*.tar.gz` from
before the v1.15.0 rename are outside every glob: not counted toward the 8, never
deleted.

**Four outcomes, one log line each. The first two are silent:**

| line | meaning |
|---|---|
| `ok already-current` | the newest server tarball is here and opens. Nothing to do. |
| `ok fetched` | it was not here; it is now, and it opens. |
| `FAILED fetch` | could not reach the server, or could not copy. **Pushes to ntfy.** |
| `FAILED verify` | it arrived empty or truncated — the part-file is deleted. **Pushes to ntfy.** |

⛔ **The ntfy push is the ONLY notification** — there is no Healthchecks check for
this pull. The message names the tarball it could not get and how old the newest
local copy now is.

⚠ **The topic comes from `~/.config/heresay/notify.env`** (`NTFY_TOPIC=`, 0600,
untracked). If that file is missing or the topic is empty the script exits
non-zero and says so on stderr, **even on a run that fetched successfully.**

```bash
tail ~/heresay/backups/fetch.log    # did it run, and what happened
# 2026-08-31 23:54:46 EDT  ok fetched  heresay-20260831.tar.gz  (retained 5)
# 2026-08-31 23:55:02 EDT  ok already-current  heresay-20260831.tar.gz  (retained 5)
# 2026-08-31 23:55:19 EDT  FAILED fetch  cannot reach commons (ssh exit 255)  (retained 5)
# 2026-08-31 23:55:31 EDT  FAILED verify  heresay-20260831.tar.gz arrived empty or truncated  (retained 5)

# from Windows (or via powershell.exe / schtasks.exe from WSL):
schtasks /Query /TN CommonsBackupPull /V /FO LIST
powershell.exe -NoProfile -Command "Get-ScheduledTaskInfo -TaskName CommonsBackupPull"
schtasks /Run    /TN CommonsBackupPull      # force a pull now
schtasks /Delete /TN CommonsBackupPull /F   # remove the schedule
```

**What is still yours to do by hand**, since no schedule can do it for you:

- **The annual restore test** — now against these self-arriving tarballs in
  `~/heresay/backups/`: unpack one somewhere throwaway and confirm the accounts
  and mail actually come back. ⚠ **Since v1.48.0 a restore takes TWO inputs**:
  one of these tarballs *and* your one-time copy of `mail.key`, which the
  tarball deliberately no longer carries. Unpack the tarball alone and
  everything comes back except the ability to read a single message — a silent
  failure, which is exactly why this test stopped being a formality. A backup
  you have never restored is a guess; a two-part backup you have never restored
  is two guesses.
- **Card / billing checks** for the VPS and the domain.

(Remember what those tarballs hold: encrypted mail and `.revtell` buffers and
password hashes — but **not** `mail.key`, which is excluded at the source since
v1.48.0 so that the ciphertext and its key never travel together. They arrive
0600 in a gitignored **0700** directory.)

## After any deploy

Confirm health: `systemctl status heresay` + `journalctl -u heresay -n 40`. If the
release ran a storage migration, the journal says so once and `ls /var/lib/heresay`
shows the new layout plus a `.pre-split` rollback file.

---

## Key locations

| what | where |
|---|---|
| Daemon unit | `/etc/systemd/system/heresay.service` |
| Config | `/etc/heresay/config.toml` |
| State (encrypted mail/revtell, events log, bans, keys) | `/var/lib/heresay/` |
| Code + venv | `/opt/heresay/` (`.venv`) |
| fail2ban filter / jail | `/etc/fail2ban/filter.d/heresay.conf`, `/etc/fail2ban/jail.d/heresay.conf` |
| Caddy | `/etc/caddy/Caddyfile` |
| **Alert secrets** (ntfy topic, Healthchecks URL) | `/etc/heresay/notify.env` — root-only, 0600 |
| **Alert scripts** | `/usr/local/bin/heresay-{notify,hcping,ceiling-watch,report-watch,healthcheck}.sh` — outside `/opt/heresay`, so a deploy never touches them |
| **Alert units** | `heresay-notify@.service`, `heresay-ceiling-watch.service`, `heresay-report-watch.service`, `heresay-boot-notify.service`, `heresay-healthcheck.{service,timer}` |
| **`OnFailure` drop-ins** | `/etc/systemd/system/<unit>.service.d/onfailure.conf` on the five watched units; the backup also has `hcping.conf` |
| Timers (backup, maintenance, cert-copy, depscan, healthcheck) | `systemctl list-timers \| grep heresay` |
