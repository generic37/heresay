# Configuring Heresay

This is the operator's manual for Heresay. Everything is configured by plain
text files that you edit directly and then run — there is no build step, no
admin panel, nothing in between the file and the program.

Configuration lives in two spots. Everything you author — the world, the words,
the theme — lives in a directory called `house/`. Everything about the machine —
ports, paths, limits — lives in `config.toml`. Edit, restart, done.

The one-sentence model: **what you edit is what runs.** There are no generators
and no hidden pipelines.

("Heresay" is the engine; a running instance is a house that you name and shape
yourself. What ships here is a SKELETON house — one sealed room — so everything
below is engine configuration, and the worked examples are examples.)

**Two markers are used throughout:** ⛔ a hard rule, or a thing not to do — the
engine or your residents will make you regret it; ⚠ a caution worth reading
twice before you act.

---

## Contents

| § | what is in it |
|---|---|
| [1. Development and production](#1-development-and-production) | the two shapes a house runs in |
| [2. The world — `house/world/`](#2-the-world--houseworld) | room files, the map, atmospherics |
| [3. The house's words — the rest of `house/`](#3-the-houses-words--the-rest-of-house) | greeting, agreement, help pages, the theme's words |
| [4. Ranks and moderation](#4-ranks-and-moderation) | the rank ladder, who may act on whom, `permissions.toml` |
| [5. The admin account](#5-the-admin-account) | what it is, and how you claim it |
| [6. `config.toml` — the dials](#6-configtoml--the-dials) | every key, section by section |
| [7. Templates — whole files to copy](#7-templates--whole-files-to-copy) | whole files: `config.toml`, `theme.toml`, `agreement.txt` |
| [8. What applies live, and what needs a restart](#8-what-applies-live-and-what-needs-a-restart) | **the live-vs-restart matrix** |
| [9. Live house settings — `.ownerset`](#9-live-house-settings--ownerset) | the house's own wording, changed in-world |
| [10. Resident preferences — `.set` and `.ignore`](#10-resident-preferences--set-and-ignore) | `.set` and `.ignore` |
| [11. Remember me — resume tokens](#11-remember-me--resume-tokens) | resume tokens |
| [12. Runtime state (hands off)](#12-runtime-state-hands-off) | what the daemon owns |
| [13. The web client — `web/index.html`](#13-the-web-client--webindexhtml) | `web/index.html`, `.reboot`, local TLS |
| [14. Live reload, and the control channel](#14-live-reload-and-the-control-channel) | editing a file while it runs; announce and shutdown |
| [15. Running it](#15-running-it) | dev, public, the event log |
| [16. When the house will not start](#16-when-the-house-will-not-start) | config-caused failures |

⚠ **Production install and restore are not here.** Standing a public house up —
the user, the directories, systemd, Caddy, fail2ban, backups — is
[`../deploy/install/README.md`](../deploy/install/README.md), and so is the
two-input restore. This file owns configuration, authoring, the live-vs-restart
model, the templates, the admin concept and config troubleshooting.

---

## 1. Development and production

There are two environments to know: a development setup for authoring and trying
things out, and a production setup for a public house.

### Development — for authoring, trying it, or a room full of people you know

The repo run directly, with no system install behind it: no `/etc`, no
`/var/lib`; state files are written right into the repo folder. Any unix-ish
environment with Python 3.12+ works — Linux, macOS, or WSL on Windows — your own
machine, or a working copy beside a production install on the same server.

Run it with `make venv` then `make run` (or `make run-dev` for throwaway state
you can delete freely, or `make lan` to add the web door for your local
network). You can expose a local instance to the internet, but production below
is the clean way to do that.

**What you configure:** just `house/` if you want to make the world your own.
`config.toml` already works out of the box — touch it only to change a port or a
limit. Nothing else.

The layout is simply the repo:

```
heresay/           the engine (Python) — never edit
house/             ← WHAT YOU AUTHOR (world, words, theme, help)
web/               the web client (index.html + font)
deploy/            deployment kit (ignore for local)
config.toml        ← the dials (defaults are fine)
config.dev.toml    throwaway-state variant for `make run-dev`
Makefile, tests/, README.md, LICENSE, …
accounts.json, mail.key, …   ← state, written here at runtime
```

### Serving the web client to your home LAN (Windows + WSL2)

To let other machines on your home network open the terminal in a browser:

1. **One-time (Windows admin):** forward the Windows host ports to the WSL VM and
   open the firewall. Run `scripts/lan-forward.ps1` as Administrator (right-click →
   *Run with PowerShell*, or from an elevated PowerShell:
   `powershell -ExecutionPolicy Bypass -File .\scripts\lan-forward.ps1`).
   It portproxies 8099 (web) and 8765 (WebSocket) to the current WSL IP and adds
   the `heresay-web` firewall rule.
2. **Serve:** `make lan` — starts the daemon and a static server for `web/` on
   port 8099, and prints the exact URL other machines must use, e.g.:

   ```
   LAN portal: http://192.168.1.10:8099/?ws=ws://192.168.1.10:8765
   (other machines must use this exact URL, including the ?ws= part)
   ```

   Give other machines that whole URL — the `?ws=` part points the page at the
   WebSocket door. Ctrl-C stops both. (`config.toml` binds the WS door to
   `0.0.0.0` and raises the per-IP connection cap to 10, since all LAN clients
   arrive through the portproxy sharing one apparent IP.)

> **The WSL IP changes across Windows reboots.** If the LAN URL stops working
> after a reboot, just re-run `scripts/lan-forward.ps1` as admin.

### Production — a public 24/7 house on a small Linux box

The three concerns are split into three locations so that updating
the code can never touch your residents' data or the machine's wiring. Caddy (a
web server) sits in front for HTTPS and the web client; systemd keeps the daemon
alive.

Two ways to get the code onto the box, both fine: clone the repo to
`/opt/heresay` and `git pull` to update (simplest — most operators start here),
or the workshop pattern — keep your working copy elsewhere and run `make
deploy`, which rsyncs the repo to the server and restarts the service (shipping
only what production needs; dev files are excluded and scrubbed). For deploy,
set your server's SSH host via `DEPLOY_HOST` — the script refuses to guess; a
git-ignored `deploy/local.env` can hold it. A fresh-server, start-to-finish
checklist lives at `deploy/install/README.md`.

**What you configure — four files, and only the first two need real thought:**

| file | what you put in it |
|---|---|
| `house/` (in your repo, then deployed) | your world and words — same as local |
| `/etc/heresay/config.toml` | the owner's name, plus any path/limit choices |
| `/etc/caddy/Caddyfile` | your domain, and the security-header block |
| `/etc/heresay/cert-copy.env` | your domain again (for the cert-copy helper) |

The systemd unit files and the maintenance script are copied verbatim — no
editing. The domain appears twice because Caddy and the cert-copy script are two
different programs that each need it.

The layout is spread across the machine — three homes, kept apart:

```
/opt/heresay/          the deployed code + house/ + web/  (never hand-edit)
/var/lib/heresay/      runtime state — the daemon owns this, hands off
                       (accounts, mail, mail.key, bans, state, revtells, resume)
/etc/heresay/
  config.toml          ← the dials (your copy; deploy never touches it)
  cert-copy.env        ← your domain, for the cert helper
  tls/                 the certificate, copied here for the telnet door
/etc/caddy/Caddyfile   ← your domain; the web proxy
/etc/systemd/system/   unit files (verbatim): heresay.service + the
                       backup / cert-copy / maintenance service+timer pairs
/usr/local/sbin/heresay-maintenance  the monthly script (verbatim)
/var/backups/heresay/  nightly state snapshots
```

**A `git pull` or a deploy can never overwrite your residents' data or the
machine's wiring**, because state (`/var/lib/heresay`) and config
(`/etc/heresay`) live outside the deployed code path. That separation is what
makes updating safe. The rest of this manual describes the engine's own
configuration — `house/` (§2–4) and `config.toml` (§6) — which is identical in
both environments; the production plumbing is §13 and the install checklist.

---

## 2. The world — `house/world/`

The world lives in **`house/world/`** — `map.txt` plus one plain-text file per
room — and the engine reads those files directly. What you edit is what runs:
there is no generator and no intermediate artifact. Residents arrive in the room
named by `[world] entry_room` in `config.toml`, move with `.go <room>` along the
exits each file declares, and `.map` prints `map.txt` verbatim.

**A WORKED EXAMPLE, not something that ships.** Picture a house of twenty-odd
authored rooms. Three of them would show off behaviour that is entirely a matter
of what a room file says, rather than a rule in the code:

- **a go-anywhere hub** — its file simply lists every
  room it reaches, and each of those lists it back. There is no "hub" flag; it is
  reachable from everywhere because everywhere names it.
- **a staff-only room** — `staff_only: yes`; residents are
  turned away with the room's own `refusal:` line, or the neutral built-in
  `You may not enter.` if it declares none.
- **the jail room** — named by `[world] jail_room`,
  with no exits, and nothing lists it, so it is off the graph entirely; a
  `.free` returns you to the entry room.

Staff can `.go` directly to any room, adjacent or not (announced normally), and
any room with `lockable: yes` can have its door `.lock`ed by an occupant (the
a house typically has a handful).

### `house/world/rooms/<id>.txt` — one file per room

The filename (minus `.txt`) is the room's id. Format: header lines, one blank
line, then the room's description verbatim to the end of the file (leading
spaces and blank lines in the description are preserved exactly).

```
name: commons
exits: hall, orchard
lockable: yes

A wide, low-ceilinged room with a few worn chairs and a cold
hearth. Doorways open onto the hall and the orchard.
```

Header keys (anything else is a startup error):

| key | required | meaning |
|---|---|---|
| `name:` | yes | display name shown in `[ brackets ]` and announcements |
| `exits:` | no | comma-separated room ids you can walk to FROM here; may repeat across several `exits:` lines, which merge |
| `lockable: yes` | no | residents inside may `.lock`/`.unlock` the door |
| `staff_only: yes` | no | staff only, by every route including invitation. (⛔ The pre-v1.54 `wizard_only:` was deleted in v1.60.0 — it is now an unknown header key, which is fatal at startup.) |
| `refusal:` | no | the line a resident refused entry to a `staff_only` room sees (absent → the neutral `You may not enter.`). **Only valid on a `staff_only` room** — a `refusal:` anywhere else is a startup error. |

⚠ **`entry:` and `jail:` are retired.** Which room is the front door
is a property of the *deployment*, not of a room file — a room that asserts "I am
the entry" cannot be reused in another house without editing it. They are named
in `config.toml [world]` now: `entry_room` (required) and `jail_room`
(optional). A leftover `entry:`/`jail:` header is accepted, ignored, and
warned about at startup — never fatal, because a deploy that refuses to start
over a line in a content file is a rename that closes the house.

Rules worth knowing:

- **Exits are directed.** Both rooms naming each other = a normal two-way door.
  One side only = a one-way passage (legal — trapdoors are a fine tradition). No
  exits = a sealed room, reachable only by invitation, staff travel, or jail
  sentence.
- At startup the daemon logs any one-way passages it finds
  (`one-way passages: cellar -> stall`). That line is your typo detector: if a
  passage you meant to be two-way appears there, one file forgot the other. It
  is also in **`.load_report`** (§15), so you can read it from in-world without
  going near the server's log.
- Fatal at startup, each with a clear message naming the file: an exit to a room
  that doesn't exist, a duplicate id, a missing `name:`, a bad flag value, an
  unknown header key, a header line without a colon, a `refusal:` on a
  non-`staff_only` room — and **no room files at all**, an unset `entry_room`,
  or an `entry_room`/`jail_room` naming a room that does not exist.
- **There is no void.** An empty `rooms/` used to boot one built-in room so a
  fresh install ran before any authoring existed. That turned a config mistake
  into a house that *looked* open and was not, so it is a startup error now,
  said plainly, once. A `rooms/void.txt` ships as an **ordinary sealed room** —
  nothing lists it, nothing special-cases it — so a bare skeleton house can
  delete the other rooms and point `entry_room` at it.
- Adding a room = adding a file (plus a line in each neighbor's `exits:`).
  Nothing else. The map does not update itself — see below.

### `house/world/map.txt`

The world map as plain text art, printed verbatim by `.map` (the display frame
is added at render time). It is hand-drawn and entirely decorative: rooms may
exist that the map doesn't show. Edit it like any drawing.

**`house/world/map.narrow.txt`** — an optional second drawing for phones; see
the `.narrow.txt` convention in §3.

### `house/world/echoes.txt` — atmospherics (optional)

One atmospheric line per line of the file. After a stretch of quiet in the
**entry room** — no resident say/emote for a randomized interval (see
`echo_min_minutes`/`echo_max_minutes` in §6) — the room murmurs one of these,
but only while someone is present. Blank lines are ignored; a missing or empty
file simply makes the feature silent. The line reads as the room speaking (not
dimmed), joins the `.rev` memory, and fades with it. Never logged, never stored.

---

## 3. The house's words — the rest of `house/`

All take effect on the next restart.

- **`house/greeting.txt`** — the login banner, printed **verbatim** before the
  `Name (or 'quit'):` prompt (leading spaces and interior blanks preserved). A
  missing file falls back to the built-in two-line banner — `. . . Heresay . . .`
  over `You had to be there.` — and is never fatal.
  The server adds one line of its own directly beneath it — `Heresay version
  X.Y.Z` — read from the engine at every login. **Do not put a version number in
  this file**: the one below it is always right, and one you typed will not be.
  One blank line of breathing room always separates the banner from the prompt,
  whatever the file contains. The shipped TEMPLATE banner is a wordmark and
  the koan line, and is meant to be replaced: it is the one file whose whole
  job is to look like your house rather than like the engine.
- **`house/agreement.txt`** — the rules shown at account creation, ending at the
  server's own `Do you agree? (y/n)`. ⚠ Consent is tied to its exact
  content (a hash): change ANY byte and every resident is asked to agree again.
  That is a feature; know it before fixing a typo casually. (Moving or renaming
  the file is safe — only content counts.)
  ⛔ **It is the one authored file that needs a RESTART.** Everything else in
  `house/` reloads live; this does not, deliberately. A live change would put
  people who log in after it on the new terms while everyone already in the
  house stayed on the old ones — a split that would last until each of them
  happened to reconnect. A restart drops everybody, and they come back and agree
  to the same version, together.
  It is **reflowed at display time** to the reader's width, 40 or 78 — write it
  as ordinary prose and every screen is handled. Wrapping
  is display-only and never moves the version, so a phone and a terminal hold the
  same one.
  **It may name config values as `{tokens}`.** Four exist:
  `{rev_window_minutes}` and `{revtell_count}` in clause 1, and
  `{event_retention_days}` and `{owner}` in clause 3. They are substituted at
  startup and the version is the hash of the **resolved** text — so a config
  change the agreement quotes re-consents everyone exactly as an edit to the
  prose does, which is the whole point: the document cannot come to state a rule
  the server does not enforce. ⛔ For the same reason those three values are
  **restart-only** as well: applying one live while the text stayed frozen would
  produce exactly the lie the tokens exist to prevent. The file itself is never written, so the token
  stays a token on disk.
  **`{owner}` comes from `theme.toml`, not `config.toml`** — it renders your
  house's word for the owner level, so a rethemed house says "Steward" without
  anyone editing the agreement. It is an input to the version like any other
  token: rename that word and everyone re-consents once. (⛔ The pre-v1.54
  `{arch}` was deleted in v1.60.0 — it is now an unrecognised token, left as
  written and named once in the log.)

  **Clause 3 discloses the event log**: that connecting is noted with
  a time and an IP, that certain actions are noted against the account they
  concern, that no word of what was said is in it, how long it is kept, and who
  can read it. If you change `event_retention_days`, that sentence changes with
  it — which is exactly why the number is a token rather than a figure somebody
  has to remember to edit.
- **`house/help/*.txt`** — one file per `.help <topic>`. Drop in a file, it
  becomes a topic; delete it, the topic's gone. House style (enforced by tests):
  first line `command — short phrase`, blank line, body; ≤78 columns. **There is
  no length limit** — how long a page should be is your call, and trailing blank
  lines are trimmed before it is framed, so a long file renders cleanly. (The
  width limit is not taste: a longer line is re-wrapped and your layout is
  lost.) Topics matching staff-only commands are shown only to staff. Bare
  `.help` is one screen; residents pick the full grid or a short index with
  `.set helpstyle long|short`.
- **`house/reboot.txt`** — the `.reboot` theater script. Plain lines print
  verbatim; directives: `@pause N` (seconds), `@beep`, `@clear`,
  `@sound power|fan|hdd|floppy|beep` (web door synthesizes audio; socket doors
  get bell-pattern fallbacks), `@progress <seconds> <LABEL>` (a building progress
  bar). Total pause time is clamped by config. A **missing `reboot.txt` is
  harmless**: `.reboot` simply answers `Nothing happens.` — see the boot-theater
  subsection below.
- **`house/theme.toml`** — the words the world uses for itself and its ranks,
  plus its icon. A missing key (or the whole file) falls back to the built-in
  default; never fatal.
  - `favicon` (top-level, optional) — the browser tab's icon: a path **relative
    to `house_dir`** (`.svg`, `.png`, `.ico`, `.gif`, `.jpg`, `.webp`). It lives
    here rather than in `config.toml` because it is *branding* — it belongs
    beside the house's name — and because `house/` deploys with the code, so
    adding one never needs an `/etc` edit. The server reads it **once at
    startup**, caps it at **64 KB**, and pushes it to the web client as a
    `data:` URL, so the page still makes no external request. Missing, empty,
    oversized or an unknown type → one warning line and the blank icon.

### The `.narrow.txt` convention — a second version for a portrait phone

Beside any authored file, a `<name>.narrow.txt` is served to sessions rendering
at 40 columns:

```
house/greeting.txt                 →  house/greeting.narrow.txt
house/world/map.txt                →  house/world/map.narrow.txt
house/world/rooms/<id>.txt         →  house/world/rooms/<id>.narrow.txt
```

**Silently optional.** Absent means the wide file goes to everyone, so an
existing house needs no change at all. Keep them within 40 columns; nothing
enforces it at runtime. (The greeting, the map and one room ship one for
Commons.)

**Rooms are the case where you almost never need one.** Room prose is prose, not
a drawing, so a narrow session with no `<id>.narrow.txt` gets the description
**reflowed**: the author's own line breaks are joined back into flowing text,
blank lines are kept as paragraph breaks, and the paragraph is set again at 40.
Write your rooms hard-wrapped near 70 for the wide screen, as the shipped rooms are,
and phones are handled for free.

So a room only wants a `.narrow.txt` when its layout is **deliberate** — a list,
verse, a scrap of ASCII, mirror writing — because a reflow will flatten exactly
that. The shipped `mirror_hall` is the worked example: it is written
backwards and right-aligned, and it has a `mirror_hall.narrow.txt` set by hand
at 40. Everything else in the house reflows.

A room's `.narrow.txt` is **the description only — no header block.** `name:`,
`exits:` and the flags are properties of the room, not of the width, and live in
the `.txt` alone. The file is served verbatim, so what you write is what a phone
sees.

**The wide screen never sees any of this.** At 78 the room file is served byte
for byte, hard breaks and all, and a `.narrow.txt` is not even consulted. The
reflow fires only at 40, only when there is no narrow file.

⚠ A `<id>.narrow.txt` whose `<id>.txt` does not exist (a typo in the stem) is
ignored, and the server names the file once at startup so it is not silent.

**How a session becomes narrow:** the browser measures its own usable monospace
columns and reports them; anything below 60 renders at 40, otherwise at
`wrap_columns`. It re-evaluates on every report, so rotating a phone flips the
layout live. The plaintext and TLS socket doors cannot report a width and are
therefore always wide. The threshold and the narrow width are code constants,
deliberately not operator knobs — the layouts are hand-set tables, and one that
must be right at every width between 30 and 200 is good at none of them.

---

## 4. Ranks and moderation

### The rank ladder — `[ranks]`

```toml
[ranks]
order        = ["resident", "staff", "owner"]
resident     = "resident"      # the lowest established rank (default: order[0])
staff        = "staff"         # the lowest rank that counts as staff
```

`order` is the ladder, **lowest first**, and a rank's name is both its
identity and the word residents see. The last entry is the owner's rank; the
one before it is the highest `.promote` can grant (the owner is set in
`config.toml`, never by a command). Everything from `staff` upward gets the
staff powers — locked-door passage, staff-only rooms, the shared letterbox,
being paged by `.call_staff`.

**Add a rank by adding a name.** `order = ["novice", "resident", "staff",
"owner"]` gives a fourth tier below staff with no code change; put it above
`staff` instead and it inherits the staff powers by threshold.

**The four LEVELS, and the zones.** The engine reasons in exactly four levels,
lowest to highest — **`newb` < `resident` < `staff` < `owner`** — and three
anchors cut the ladder into those four bands (the owner caps it):

```
[ newb … <resident ]   [ resident … <staff ]   [ staff … <owner ]   [ owner ]
  sign-on                 established              staff              supreme
```

- **`newb`** — **the bottom of `order`, always**, and where new accounts
  start by default (see `landing_level` in §6). Want a probationary tier? Put a
  rung at the bottom of `order` and `.promote` people out of it when you are
  satisfied. ⚠ It is **not a setting**: a `newb = "..."` line is warned about
  and ignored. Deriving it means nothing can sit *below* the newb rank, which is
  what stops a ladder change from accidentally *raising* somebody.
- **`resident`** — the lowest *established* rank; what "an ordinary resident"
  means, and what the `resident` permission anchor resolves to. ⚠ This anchor
  was called **`floor`** before v1.54.0. ⛔ That alias was deleted in v1.60.0:
  a `[ranks] floor` line is now an unknown key, ignored like any other, so the
  anchor falls back to `order[0]` rather than being read.
- **`staff`** — as before.
- **`owner`** — `order[-1]`, held by the account `config.toml` names.

**`resident` and `staff` default to the bottom of the ladder**, which collapses
the newb zone to nothing — so a house that ignores all of this behaves exactly
as it always has, and new accounts land on the lowest rung. You have to ask for
a newb tier to get one.

⛔ **`resident` is checked strictly**, unlike the rest of this file: naming no
rank, or sitting above `staff`, **stops the daemon** with a message naming
the problem. It decides who counts as established — there is no sensible
value to guess on your behalf. Leaving it out is always fine; getting it
wrong is not.

⚠ **The one reserved anchor word a rung may not be *named*** is `newb`, the
derived-level word; a rung called that is dropped and warned about.
(`floor`/`arch`/`wizard` left the list in v1.60.0 with the aliases that made
them ambiguous — nothing reads any of them now, so reserving them would reserve
three words this engine has no use for.) Note `resident`,
`staff` and `owner` are **not** reserved — a house legitimately names its anchor
rank after its level, as the shipped default ladder's `resident` rung does.

**Reorganising the ladder — everyone lands at the bottom of their tier.**

**Change the ladder's shape and the next boot collapses every account into
its zone.** Add or remove a rung, or move an anchor, and:

| you were | you become |
|---|---|
| the owner account | the owner, always (identified by name) |
| staff | the lowest staff rank |
| established | `resident` |
| below `resident` | `newb` |
| a rank that no longer exists | `newb` |

**Everyone lands at the bottom of their tier, and you re-promote the ones
who should be higher.** The startup log and `.load_report` both say how many
moved. Why: across a reorg the old words no longer mean what they did, and
guessing would hand out authority nobody granted.

⚠ **Renaming a rank drops its holders to `newb`.** The engine cannot know
that "staff" used to be your staff rank once the word is gone. If you want
to keep tiers across a reorg, keep the words and change the order.

**A normal restart never touches a rank.** The collapse only runs when the
ladder's shape actually changed — a fingerprint of `order` plus the resolved
`resident` and `staff` anchors is stored beside the accounts and compared at
every boot. ⚠ A ladder that keeps its rank names across an upgrade keeps its
fingerprint too, so **no account collapses**. Rename the
house, change the favicon, restart nightly: nobody moves. Upgrading the
engine does not move anyone either.

Getting this wrong is not fatal: a malformed `order` falls back to the
built-in ladder, and a `staff` naming no rank means only the top rank is
staff. Both log a line. An older `theme.toml` with `resident`/`wizard`/`arch`
keys and no `order` is read as a ladder from those three, so it keeps
working.

⛔ **One thing DOES refuse to start: a rank whose name is also an account
name.** A rank word is an identity here — every rank name is reserved from
registration, `.call_staff` means the rank, the letterbox is addressed to it
— so the house will not run with both. The message names the pair and tells
you to delete the account or rename the rank.

⚠ **Renaming a rank does not carry anyone across.** `accounts.json` stores
the rank NAME, so renaming "staff" to "warden" leaves everyone who held it on
a word the ladder no longer has — read as the resident floor, and collapsed to
`newb` by the reorg above. The server counts them at startup and says so; promote
them again, or rename back. (Internal
identifiers are untouched by any of this: `STAFF_BOX`, the `[staff]` config
section, and command names like `.promote` are code, not theme.)

### Whom you may act on — the target-rank rule

⛔ **You may act only on people whose rank is strictly BELOW your own, and never
on the owner.** It covers **every command that acts on a
person** — `.gag`, `.ungag`, `.jail`, `.free`, `.boot`, `.mailpurge`,
`.resetpassword`, `.deleteuser`, `.ban`, `.ban_ip`, `.unban`, `.unban_ip`,
`.promote` and `.demote`. Staff cannot moderate, ban, unban, promote or
demote one another, and nobody can touch the owner.

`.promote` and `.demote` are checked from both ends:

- **The person** must be below you. You cannot promote or demote a peer, or
  yourself.
- **The result** may reach your own level and no further. So: raise someone
  below you, up to your level, never above. (Owner is unreachable either way; it
  is set in `config.toml`, never by a command.)

Why it matters: you can assign any command to any rung. Handing `.promote` to your staff without this rule would have handed
each of them a one-command route to the rank below yours; handing them
`.resetpassword` would have let them set a *superior's* password — with their
own — and log in as them. The rule closes both.

Two exceptions:

- **The owner can free themselves.** Nobody else can self-free. Without it a
  jailed owner would be locked in — they are the only person with nobody
  below them, including themselves — and a jail that cannot be undone is worse
  than staff undoing one. Freeing anyone *else* obeys the rule like the rest.
- **An owner caller loses nothing.** "Strictly below the top" is everyone else.

**`.unban <address>`, the literal-address form, is the one place the check
cannot reach** — there is no person in it to rank. Nothing in the house prints an
address, so having one means you brought it from outside. `.unban_ip <name>` is
the by-name path, and it is gated.

### ⛔ `.ban_ip` will not ban staff — even by accident

Name a resident who happens to share a connection with one of your staff, and
without this your staff go down with them. Every other check passes: the person you
named really is below you.

So the whole set the ban would reach is checked — the named account **and**
everyone else seen on that address — and:

- **The owner is told, by name, which staff would be caught**, and asked
  `Continue? (y/n)` before the usual collateral question. Two questions, because
  "this also takes staff off the door" is a different decision from "this
  catches four housemates".
- **Everyone else is refused**, with a notice that says only *this ban would
  affect staff* — no name, no number, nothing identifying. The notice is the
  same in every case, **including when the staff account caught is your own**.
  That is deliberate: the house does not tell anyone who shares an address with
  whom, and a refusal that varied would be a way of asking.

The owner's own presence on an address never triggers it — their address is
never refused at the door anyway, so they are not collateral.

⚠ **`.mailpurge` asks for your own password**, like `.deleteuser` and
`.resetpassword`. It destroys someone's whole mailbox and cannot be undone, and
until now one mistyped line finished it. It still never *reads* the mail — the
password protects against the accident, not against the contents.

### Who may run what — `permissions.toml`

**A file beside `config.toml`** — `/etc/heresay/permissions.toml` in production —
listing every command and the rank it needs. **It is config, exactly like
`config.toml`: you own it, the daemon only reads it**, and your edits **apply
live** — no restart, and nobody is disconnected.

```toml
[commands]

# room life
look        = "resident"
lock        = "resident"

# moderation
gag         = "staff"
ban_ip      = "staff"

# accounts
promote     = "owner"
```

**A value is a RANK NAME from your `[ranks] order`.** It means that rank **and
everyone above it**: `ban_ip = "High Warden"` puts the command in the hands of
High Wardens and whoever outranks them. That is how you assign a command to a
rung you invented.

#### ⛔ `"disabled"` — the one value that is not a rank

```toml
reboot = "disabled"
```

**A disabled command is not a command.** Not hidden, not refused, not
permission-denied — it **does not exist in your house**:

- Typing it gets the **ordinary unknown-command reply**, byte for byte what
  typing nonsense gets. The house does not advertise what it does not have.
- It is **absent from `.help`** and from every listing and index.
- **No permission check runs.** There is nothing to check.
- **Its aliases die with it** — disable `.examine` and `.ex` is gone too.

⛔ **It binds everyone, INCLUDING YOU.** A value that let the owner through would
be a rank, not a disable — "reserved to me" is a different thing, and you already
have a way to say it (`= "<your top rank>"`). If you disable a command, your
house does not have it, and neither do you.

⛔ **Nothing is protected from it.** `.passwd`, `.quit`, `.recovery`,
`.agreement` — all disablable, with no warning and no "are you sure". It is your
house. The safety net is structural rather than a list of exceptions: this file
is root-owned on the server and **the daemon never writes it**, so nothing anyone
does in-world can stop you undoing it, and the undo is live within seconds.

⚠ **The help page stays installed.** A disabled command is simply not listed or
reachable; its `house/help/*.txt` page is untouched. Re-enabling is therefore a
one-line change to *this* file — never a content deploy.

⚠ **A rank may not be called `disabled`.** That collision stops the daemon, and
says so: a value would otherwise be ambiguous between "this rank and above" and
"no such command", which are opposite answers.

**`.reboot` ships disabled in a fresh install.** It is a joke command — a staged
486-era boot sequence, shown only to whoever typed it, that changes nothing — and
it lands very differently in a house whose regulars are in on it than in one
where they are not. Delete the line, or give it a rank, and it is back.

⚠ **The role words `newb`/`resident`/`staff`/`owner` are not values here.** They
still exist inside the engine as the built-in defaults, but they are turned into
your rank names when the file is written, so you never see one. (⛔ The pre-v1.54
`floor`/`arch`/`wizard` spellings were deleted in v1.60.0; a file still carrying
one refuses the boot, and `--write-permissions` is the fix.)

**Your existing file is converted for you, once.** `--write-permissions` (below)
rewrites any role word as the rank it resolves to — `resident` → your resident
(floor) rank, `staff` → your staff rank, `owner` → your top rank — and leaves
everything else alone: values already written as rank names, your comments, your
spacing, your ordering. Run it twice and the second run does nothing.

**The trade, stated plainly: rank names are exact, and they are not rename-safe.**
If you rename or remove a rank, every line naming it becomes invalid — and since
that is boot-fatal, a reorg forces you to update this file in the same reboot.
That is intended: it is the one moment you are certainly thinking about ranks.

⛔ **A value that is not one of your ranks stops the daemon. No exceptions.** A
typo here does not degrade a command, it *hides* one: the gate is invisible by
design, so `gag = "wardn"` would put `.gag` out of everyone's reach with nobody
the wiser. So the boot refuses and names the line — and that is true of a typo, a
rank you have deleted, and a leftover role word alike. There is no
resolve-with-a-warning.

**At runtime the opposite applies** — a live edit to an invalid rank is
*discarded*, the running table is kept, and `.load_report` says
`not a valid rank`. A typo can stop a start; it can never close a house that is
already open.

**It is complete, and it stays complete — in two halves.** A command missing
from the file takes its built-in default **in the running table**, so the house
is never short of a command; and the FILE gains the line when
`--write-permissions` runs (your deploy does this, as root, before the restart):

```bash
cd /opt/heresay && sudo /opt/heresay/.venv/bin/python -m heresay \
    -c /etc/heresay/config.toml --write-permissions
```

⚠ **From `/opt/heresay`** — the `cd` is not decoration. The venv holds the
dependencies, not the engine itself, so `-m heresay` is found via the working
directory (which is exactly what `WorkingDirectory=` gives the service).

Absent, it writes the file complete. Present, it converts any role word to a
rank name (once) and **appends only what is missing**, in a marked block — your
values, comments and ordering are never touched, and running it twice changes
nothing. `.load_report` tells you when the running table is ahead of the file.

⛔ **If it cannot make sense of your file it exits non-zero, and the deploy
stops before the restart.** That is on purpose: a bad permissions file would
refuse the boot, so the deploy catches it while the old code is still serving
and tells you what to fix. Nothing is restarted.

⛔ **The daemon never writes this file, and that is deliberate.** `/etc` is
mounted read-only for the service (as it should be — a daemon that can rewrite
its own configuration is one whose configuration is not protected), so writing
is a root job done at deploy time. If the file is missing, unreadable, or its
directory is read-only, the house **still starts**, on the built-in table, and
says so.

**Delete a line and it comes back at the default.** Say what you mean with a
VALUE, not with an absence.

**Two kinds of mistake, two answers:**

- **A value that is not a rank** (`gag = "sideways"`, `gag = "wardn"`,
  `gag = 3`) → the file is refused. At boot that stops the daemon, with the
  reason; **at runtime the running table is kept and the house carries on** — a
  typo cannot close a live house. `.load_report` shows what was refused and why.
- **A name that is not a command** (a line left over from an older release) →
  that line is ignored with a warning, and the rest of the file is used.

**Edits apply within a few seconds** — see "Live reload" below. There is no
command to type and none to remember.

⚠ **Upgrading from a release where this table lived in `state.json`?** Your
deploy's `--write-permissions` step brings your assignments across exactly, and
the daemon stops writing the old block.

⛔ **Nothing can eat an edit to this file**, because the daemon does not write it
at all.

### Names you won't host — `forbidden_names.txt`

A second file beside `config.toml` — `/etc/heresay/forbidden_names.txt` —
listing names nobody may register. **Yours to decide**: impersonation bait,
`admin` and `root`, slurs, a brand you would rather not host, somebody who keeps
coming back. The engine has no opinion about what belongs on it.

```
# names we won't host
admin
root
support          # people mistake it for staff
```

**One name per line.** `#` comments and blank lines are ignored, surrounding
whitespace is stripped, and matching is **case-insensitive** (`Admin`, `ADMIN`
and `admin` are the same entry). There is no syntax to get wrong: anything that
is not a comment is a name.

**No file, no problem.** Absent means an empty list — you simply have no blocks
of your own. Same if it cannot be read: it is a blocklist, never a reason the
house will not open.

**It only ADDS.** What may not be registered is your list **plus every rank name
in `[ranks] order`, always**. Rank names are reserved by the engine because a
rank word is an identity here — `.call_staff` has to mean the rank, not an
account that took the word — and nothing you write in this file can unreserve
one.

⚠ **On Commons that already covers `staff` and `owner`**, because they
are rank names. A house that calls its ranks something else has no reason to
reserve this house's words, and can list them here if it wants them. ⚠ **If you
rename your ranks and still want the old words held, list them here** — nothing
in this file can unreserve a rank name, but nothing reserves a retired one
either.

**A blocked attempt is told only `That name isn't available.`** — the same
sentence a rank name gets. It never says the list exists, and never says which
entry matched: otherwise anyone could read your list back one guess at a time.

**Someone already has a name you just added?** They **keep it.** Nothing is
renamed, nothing is deleted, and they can still log in — you have changed what
you will accept in future, not what already happened. The log says how many
accounts are affected and **not who**:

```
forbidden_names: 2 existing account(s) hold a now-forbidden name (grandfathered)
```

Which accounts is something to go and look up on purpose, not something a log
line hands you on the way past. New registrations of that name are blocked as
normal.

⚠ **This is not the rank↔account rule.** A *rank name* colliding with an
existing account stops the daemon, because the house cannot mean two things by
one word. This file never fails a boot and never touches an account.

**Edits apply live**, within a few seconds, like `permissions.toml` — the very
next registration attempt uses the new list, and `.load_report` shows how many
names were added or removed. It shows counts, never the names themselves: this
is a list of words you would rather not see, and the report is not the place to
recite them.

---

## 5. The owner account

**The owner is an identity, not a rank you grant.** `[moderation] owner` names an
account, and whoever logs into that account holds the top rank on the ladder
regardless of what `accounts.json` stores for them. No command can promote
somebody to it and no command can demote them out of it.

⛔ **`owner` is REQUIRED and has no default.** It ships as the placeholder
`owner = "[owner_name]"`, and the house **refuses to boot** until you set it to a
real account name (`heresay/config.py`, `validate_config`) — the same
refuse-until-configured posture as `entry_room`. An empty or placeholder value is
a boot-fatal `ConfigError` naming the fix. (⛔ Before v1.54.0 the key was `arch`;
that fall-back was deleted in v1.60.0, so a config still using it has no owner
and will not boot.)

- **The key is `owner`, under `[moderation]`** (§6). The account it names does not
  have to exist yet.
- **The rank is `order[-1]`** — the last entry in `[ranks] order`, `owner`
  in the template above.
- ⚠ **It is an ACCOUNT NAME, not the house's name.** `instance_name`, a few
  lines above it in the same `[world]` block, is a different setting entirely.

### Claiming it

⛔ **The owner name can only be registered from the local console, and only
while no such account exists** (`heresay/server.py`). From any network door the
name is refused with the same sentence every unavailable name gets — *"That name
isn't available."* — which is deliberate: three different reasons share one
message so nobody can map the reservations by guessing.

The local console is the **plaintext listener** (`127.0.0.1:2324` by default),
and it is the only door that may set that flag. ⚠ The gate is the flag, **not
the peer address** — a loopback-looking IP is forgeable behind a proxy; the door
you arrived through is not.

```
telnet 127.0.0.1 2324          # or: ncat 127.0.0.1 2324
# Name (or 'quit'): admin
# No one uses 'admin'. Create it? (yes/no) yes
# choose a password — this account now holds the top rank at login.
```

Once the account exists the exemption is spent: the name is an ordinary account
and the loopback path no longer treats it specially.

⚠ **The console is also exempt from private mode**, so `.logins off` cannot lock
the operator out of their own house while they are at the box.

⚠ **On a fresh box the daemon warns if the named account is missing** — the
startup log says the owner account does not exist and that its name may only be
registered from the localhost listener until it does. If you see that after
setup, you have not claimed it yet.

**In production**, this happens over an SSH session on the host — see
[`../deploy/install/README.md`](../deploy/install/README.md) §8, which puts it in
order with the rest of the install rather than repeating it here.

---

## 6. `config.toml` — the dials

Every key has a sane default, so a minimal config is legal. The list below is
verified against `heresay/config.py`, the source of truth; the TOML section and
key names are exactly as read there.

**Paths.** Most paths resolve relative to the config file's own directory. The
authored-content paths are the exception: they all default to sitting under
**`house_dir`**, the content root. Set `house_dir` and every content file is
found automatically — so on a real deployment it is usually the only content
path you ever set. (An *absolute* per-file override still wins outright, which
is what lets a config full of absolute content paths keep working with nothing
added but `house_dir` itself.)

⚠ `house_dir` defaults **relative to the config file**, so an `/etc`-based
deployment that omits it looks for `/etc/heresay/house` and will not find your
world. Set it explicitly in production.

### `[listeners]`

- `tls_host` — the bind address for the TLS (tt++/telnet) socket door: which of
  the server's own interfaces to listen on, **NOT your domain**. Two typical
  values: `0.0.0.0` (all interfaces; reachable from the internet — the
  production setting) or `127.0.0.1` (this machine only — a common dev setting).
  The same bind-address logic applies to `plain_host` and `ws_host` below.
- `tls_port` — the port clients dial for that door (default `2323`); leave it
  unless you must clear a firewall restriction or avoid a dev/production port
  collision on one box. The door needs a real certificate in production (§13).
- `plain_host` — the bind address for the plaintext (unencrypted) door; keep it
  on loopback (`127.0.0.1`, the default). Because the traffic is unencrypted,
  binding it anywhere else would broadcast plaintext logins across the network —
  extraordinarily insecure. This is the operator's LOCAL door — reach it from
  the box itself (e.g. over SSH) — and the only place the owner
  account can be registered.
- `plain_port` — the port for that door (default `2324`).
- `ws_host` — the bind address for the WebSocket door; stays on loopback
  (`127.0.0.1`) behind the reverse proxy in production. Bind wider only for LAN
  experiments.
- `ws_port` — the port for that door (default `8765`).
- `trusted_proxy` — spoofing protection for the WebSocket door only (the TLS and
  plaintext socket doors read the real client IP straight from the connection,
  so this never affects them). It names the one machine Heresay will trust. In
  production, web users reach Heresay through Caddy, so their real IP arrives
  only inside a header (`X-Forwarded-For`) — and a header can be forged by a
  client trying to fake its address to dodge a ban or the per-IP limits. Naming
  the proxy tells Heresay to believe that header ONLY when it comes from Caddy,
  never from a client pretending to be one. This is a production-only concern.
  Behind a proxy, set `trusted_proxy = "127.0.0.1"` (Caddy, same box). In local
  dev or testing there is no proxy and no public exposure, so leave it empty
  (the default) — empty means trust nobody.

**A note on `Origin`:** the WebSocket door does **not** allowlist `Origin`, and
that is deliberate rather than an oversight. Authentication is per-connection
credentials with no cookies and no ambient authority, so there is no CSRF
surface for an `Origin` check to protect; self-hosting operators serve from
their own domains; and non-browser WebSocket clients send no `Origin` at all, so
an allowlist would reject legitimate clients for protection the credential model
plus the per-IP limits already provide.

### `[cert]`

- `cert_file` — the TLS certificate for the socket door (2323).
- `key_file` — its matching private key.

Without a certificate that door doesn't open — and in production the web front
can't serve either (Caddy needs the same certificate), so no one can reach the
house from outside at all. Locally that's fine: no certificate, and you connect
on the plaintext door. In production the certificate is supplied automatically —
see §13.

### `[storage]` — paths

Every path here defaults, so a minimal config sets none of them. These keys only
say *where* each file lives (its default filename is shown after the arrow) —
what is IN them is covered in §2, §3 and §12.

**The content root:**

- `house_dir` → `house/` — everything you author. Every content key below is a
  name *relative to this*.

**State files** — default into the state directory (production
`/var/lib/heresay`):

- `accounts_file` → `accounts.json`
- `mail_file` → `mailboxes.json` — ⚠ this names the store rather than a file:
  the mailboxes live one-per-file in `<mail_file>.d/`, a directory derived from
  this value (so there is no second key to set, and no way for the two to
  disagree). The value itself is unchanged; an existing single-file
  `mailboxes.json` is split and renamed aside on the first boot.
- `mail_key_file` → `mail.key`
- `bans_file` → `bans.json`
- `state_file` → `state.json` — the private-mode switches, the jailed set, the
  `.ownerset` house wording (the command→rank table lives in
  `permissions.toml`, not here)
- `revtell_file` → `revtells.json` (defaults beside `mail_file`; they share the
  mail key) — same one-file-per-account layout, in `<revtell_file>.d/`
- `resume_file` → `resume.json` (defaults beside `bans_file`; the "remember me"
  tokens, §11)
- `events_file` → `events.jsonl` (defaults beside `bans_file`) — **the engine's
  operational event log**, described below
- `ban_history_file` → `ban_history.json` (defaults beside `bans_file`) — every
  ban ever placed, `0600`; see `ban_history_days`

**Content files** — default under `house_dir`, and you rarely set any of them:

- `agreement_file` → `agreement.txt`
- `world_dir` → `world/`
- `help_dir` → `help/`
- `greeting_file` → `greeting.txt`
- `reboot_file` → `reboot.txt`
- `theme_file` → `theme.toml`

Set `house_dir` and the rest follow.

### `[world]`

- `instance_name` — the house's own name, threaded into `.version`, the `.map`
  title, the `.examine` owner line, the login notices and the browser tab's
  title. ⚠ **Moved here from `house/theme.toml` in v1.60.0** so that a fresh
  install is one file to edit; there is no fall-back read of the old location,
  and a `theme.toml` still carrying the key is warned about and ignored. The
  shipped example says `[instance_name]`, which refuses the boot until it is
  filled in; delete the key entirely and the house calls itself `the house`.
- `entry_room` — **required.** The room id arrivals land in at login. There is
  no default and no fallback: an unset or unknown `entry_room` is a startup
  error.
- `jail_room` — the room `.jail` sends people to. Optional; unset means
  `.jail`/`.free` answer `No jail location set.` and change nothing.
- `wrap_columns` — output wrap width (78; the whole aesthetic assumes it). This
  is the **wide** value specifically — a browser reporting a narrow screen
  renders at 40 instead (§3).
- `rev_window_minutes` — how far back the entry room's fading `.rev` memory
  reaches (30 min).
  ⛔ **The agreement quotes this number: changing it needs a RESTART**, and
  re-consents everyone when the house comes back.
  `house/agreement.txt` carries the token `{rev_window_minutes}`, not a figure;
  it is substituted at startup and the agreement's version is the sha256 of the
  **resolved** text. So a house that re-tunes its `.rev` window has changed what
  clause 1 says, and every resident is shown the revised agreement once at their
  next login. That is the intent — a consent document must not quote a retention
  period the server has stopped enforcing — but it is worth doing on purpose
  rather than discovering. Editing the prose has always had the same effect.
- `rev_max_lines` — the cap on that memory, keeping the newest N lines (100). In
  RAM only.
- `revtell_count` — **a different number, and worth keeping straight**: how many
  of your OWN tell/remote lines `.revtell` holds, per account (20). The two above
  bound one *room's* public murmur, in RAM; this bounds one *person's* private
  buffer, encrypted on disk.
  ⛔ **The agreement quotes this too: changing it needs a RESTART**, and
  re-consents everyone when the house comes back —
  clause 1 carries `{revtell_count}`, on exactly the same footing as
  `{rev_window_minutes}`. Same reasoning: a consent document must not name a
  buffer size the server has stopped honouring.
- `event_retention_days` — how long the engine's event log keeps a fact (**10**).
  **One window for every event type**: a per-type table is
  something you would have to reason about, and the honest answer to "how long do
  we keep operational facts?" is a single number. Trimming happens in-process, on
  every append and once at startup — no cron, no logrotate, nothing to install.
  `0` or less means keep everything. See the event log below.
  ⛔ **The agreement quotes this number: changing it needs a RESTART**, and
  re-consents everyone when the house comes back — clause 3 discloses the
  recording and states the retention as
  `{event_retention_days}`, on the same footing as clause 1's two numbers. That
  is the intent: how long a person is remembered is part of what they agreed to.
- `security_window_hours` — how far back `.security` counts (**24**).
- `staff_calls_window_hours` — how far back `.staff_calls` tallies (**24**).
- `reload_poll_seconds` — how often the daemon checks whether a watched config
  file changed (**3**). `0` switches live reload off. See "Live reload" (§14).

  These two are the VIEWS' windows, and they are a different question from
  retention: retention is a policy about the file, a window is what somebody is
  asking today. **Both are capped at `event_retention_days`** — a screen cannot
  show what the store no longer holds — so setting one longer than the retention
  simply shows everything retained, and so does `0`. Neither can be
  misconfigured into an empty screen. They are display settings and the
  agreement does not quote them: **changing either re-consents nobody.**
- `echo_min_minutes` — the shortest quiet gap before an atmospheric line fires
  (10 min).
- `echo_max_minutes` — the longest quiet gap (15 min); the actual wait is drawn
  at random between the two.
- `disconnect_grace_seconds` — reconnect within this window and the room sees no
  exit/enter churn and you land back in the same room — but ⚠ **you receive
  nothing that was said while you were gone**; the house holds your place, not a
  transcript (**120** — long enough that a phone losing signal, or a browser
  discarding a backgrounded tab, does not read as leaving).

### `[moderation]`

- `owner` — the account name that holds owner rank at login. ⛔ **REQUIRED, no
  default**: it ships as the placeholder `owner = "[owner_name]"` and the house
  **refuses to boot** until it is set to a real account name (§5). ⚠ This is an
  **account name, not the house's name** — `[world] instance_name` is a
  different setting, and the two are easy to confuse. If no such account exists
  yet, that name can only be registered from the localhost listener (prevents
  owner-capture on a fresh or wiped install). (⛔ The pre-v1.54 `arch` spelling
  was deleted in v1.60.0.)
- `landing_level` — which **level** a NEW account lands in: `newb`, `resident`
  or `staff` (**default `newb`** = the bottom of the ladder, exactly the
  pre-v1.54 behaviour). It resolves to that level's anchor rank at account
  creation. ⛔ `owner`, or any value that is not a level, is a **boot-fatal**
  config error — a new account may never land at the supreme level.
- `ban_default_hours` — the default `.ban` duration (24 hours). `.ban` is
  **name-only**: it takes an account, never an address.
- `ban_ip_default_hours` — the default `.ban_ip` duration (24 hours). Its own
  key rather than sharing the one above, because banning an **address** is the
  blunter instrument — it can catch a household, a flat share, a campus or a
  phone carrier's NAT — so you can keep it short without shortening account
  bans. `.ban_ip` shows the collateral before it acts, and asks.
- `ban_history_days` — how long the **ban history** is kept (**0 = forever**,
  the shipped default). Every ban ever placed is recorded in
  `ban_history.json`, and it outlives the ban: `bans.json` drops an expired ban
  (holding it would be holding a punishment that has ended), so it cannot answer
  *"has this happened before?"* — this can. Set a finite number of days if you
  would rather it forgot.

### ⛔ IPs are never shown

**No command prints an IP address — not to staff, not to the owner.** You ban
`.ban_ip <name>`, lift `.unban_ip <name>`, and read `.bans` by name; the engine
resolves, stores and matches addresses internally and never puts one on a screen.

The raw address lives in exactly two files — `bans.json` (active enforcement) and
`ban_history.json` (the long record) — and surfaces only when **you deliberately
open one during a security audit**. That is a considered act with a reason
attached, not something a command can be talked into. A test drives every ban
command and greps the output to keep it true.

This is also why `.ban_ip` sits at the ordinary moderation tier: it was
owner-only while it could expose an address, and it cannot any more. `.ban`, `.ban_ip`, `.unban`, `.unban_ip` and `.bans` all sit at the staff
tier, assignable like any command.

**What `.ban_ip` shows before it asks** — the collateral, split in two, because
the two groups suffer differently:

```
This address is in use by 1 online and 2 offline account(s) (last 10 days):
  online:  Bob
  offline: Carol, Dave
Heads up: this address has been banned 2 time(s) before (last: 2026-07-20, by Wizzy, flooding).
Ban it for 24h? (y/n)
```

*Online* are kicked when it lands; *offline* were seen on the address inside
`event_retention_days` and are simply shut out going forward. The owner is in
neither list. The recidivism line is matched on the **address**, which is what
catches the same person returning under a new account name — and is the whole
reason the history keeps an address at all.

⚠ **Every `.ban_ip` run is recorded in `.staff_actions`, confirmed or
cancelled**, with the outcome — and `.staff_actions` **shows** which it was, so
a cancelled attempt cannot be misread as a ban. Running it is the moment a staff
member is shown who shares an address, so the *attempt* is the thing worth
auditing. The target recorded is the account name, never the address.
- `ban_max_hours` — the ceiling on `.ban` durations (1 year).

### `[mail]`

- `mailbox_cap` — messages per mailbox (50).
- `purge_days` — mail (and idle `.revtell` buffers) older than this are deleted
  (180).
- `send_burst` — how many mails can be sent in quick succession (3).
- `send_refill_per_sec` — how fast that allowance refills (1 per 20s), so mail
  can't become a spam channel.
- `profile_max_lines` — the profile editor's line cap (20).
- `mail_max_lines` — the mail editor's line cap (40).
- `report_retention_days` — how long a `.report` stays in the report box (**10**).
  ⚠ **Its own number, not `purge_days`** — and ⛔ **it dropped from 100 to 10 in
  v1.58.0**, because the box copy stopped being the record. A report is written
  to the operator's disk in full at the instant of filing (see below), so what
  sits in the box is the working copy `.read_report` opens; ten days matches
  `event_retention_days`, which is the honest window for anything the engine
  holds about people.
- `report_box_cap` — messages in the report box (**1000**). ⚠ **Its own cap since
  v1.58.0, no longer `mailbox_cap`'s 50.** A mailbox cap bounds how much one
  person can be sent; the report box is the whole house's single channel for
  saying something is wrong, and fifty junk reports could wedge it shut — past
  the cap the next report is refused outright and the filer is told the box is
  full. ⚠ **There is no notification when it fills**, deliberately: the operator
  is nudged on every report already.
- `report_capture_days` — how long a report's CAPTURE FILE lives (**375**). `0`
  keeps them for ever, like `event_retention_days`. A year of coverage counted
  from the *event* rather than from the filing, since the capture already reaches
  ten days back into the log.

#### ⛔ The capture file — the record, on your disk, out of the system

**At the instant a report is filed, the daemon writes the whole thing to disk**:
the reporter, the accused, the full text, the filing time, both account records,
and every event-log row about either of them — including the addresses they were
seen on. One plain-text file per report, in a `reports/` directory inside the
state dir (beside `events.jsonl`), mode **0600** in a **0700** directory.

**Why it exists.** A report used to outlive its own evidence: the box held it for
100 days while the context aged out of the log at ten, so at day eleven you could
read an accusation with no way left to check it. The capture inverts that —
everything is kept at filing, and the in-system copy then expires like everything
else. **The engine goes back to holding nothing durable about a report.**

⛔ **NOTHING IN THE ENGINE READS IT BACK.** There is no command, at any rank, that
lists, counts, searches or prints these files, and none is coming: no `.security`
row, no `.load_report` line, no admin panel. It is write-only from the engine's
side. You read it with `less` over ssh, and a test greps the engine for any read
path into that directory so a later command cannot quietly gain one.

⚠ **It is deliberately PLAINTEXT, and that is a real trade.** The mail store's
encryption protects a report from the *system* — a stolen disk, a casual file
read. This file is protected by being 0600 on a box only you reach. ⚠ **It is in
the nightly backup** (it is the record, so it must be), and that tarball leaves
the machine: the ciphertext in it is inert without `mail.key`, and these files
are not. It is excluded from the deploy rsync.

⚠ **Not root-owned** — the daemon runs as its own user and cannot chown to root.
0600 plus the 0700 state directory is what it can guarantee; read it with
`sudo less`.

### Top level

- `source_url` — where this house's source can be had; `.source` prints it beside
  the running version. ⛔ **A public instance must set this.** The AGPL's §13
  obliges an operator running a *modified* Heresay as a network service to offer
  its users the corresponding source, and a repository somebody else can find is
  not an offer to a user standing at a text prompt. Unset, `.source` says so
  plainly rather than naming an upstream this house may no longer match.

There is nothing to configure about the login notice: **`You have mail.` fires
at login when the box holds something UNREAD**, and reading it with `.mail`
marks the whole box read. The
**staff letterbox is the exception**: it is SHARED, so `You have <staff> mail.`
is still gated on non-empty. One flag per message cannot mean "read" for a box
several people read; whoever handles a note clears it for everyone with
`.dstaffmail`.

⚠ **A house upgrading from before that flag existed announces everyone's mail to
them once more** — it is migrated unread rather than guessed at, and nothing is
deleted or re-encrypted.

### `[keepalive]`

- `tcp_keepidle` — idle seconds before keepalive probing starts (120).
- `tcp_keepintvl` — seconds between probes (20).
- `tcp_keepcnt` — failed probes before a connection is declared dead (4).

Together these detect a dead socket connection within a few minutes, without
ever disturbing an idle-but-connected human. **There is no idle timeout and
never will be** — sitting quietly is presence, not a problem.

### `[ratelimit]`

Flood control:

- `speech_burst` — the shared speech/emote/think/tell burst allowance (5).
- `speech_refill_per_sec` — how fast it refills (1/s).
- `speech_mute_strikes` — flood strikes before a server-side cool-off (5).
- `speech_disconnect_strikes` — strikes before the session is disconnected (15).
- `speech_mute_seconds` — how long the cool-off lasts (10).
- `shout_per_seconds` — the stingier separate `.shout` bucket (30s).
- `command_burst` — burst allowance for commands (20).
- `command_refill_per_sec` — how fast that refills (5/s).
- `mute_cap` — the most names one resident may keep on their `.mute` list (100),
  so a mute list cannot grow without bound.

Per-connection and per-IP:

- `max_conns_per_ip` — concurrent connections per client IP (3).
- `churn_window_seconds` — the window over which rapid reconnects are counted (10).
- `churn_max_in_window` — the most reconnects allowed within that window (5).
- `auth_phase_seconds` — a hard deadline on the pre-auth phase (60), so a
  slow-loris dribbling bytes at the login prompt cannot pin a connection
  forever. An *authenticated* session is never subject to it. This is the
  **credential** clock — the name prompt, the two password prompts, the
  takeover question — and it does **not** run while the agreement is on screen.
- `agreement_seconds` — the deadline on **reading the agreement** and answering
  it (300). Reading the house rules is the one part of the pre-auth phase that
  is not a prompt but a document, and at 60 seconds a careful reader on a phone
  was dropped mid-clause. So the credential clock is **paused** while the
  agreement is up and this one runs instead; on the way out the credential clock
  resumes with **exactly the time it had left**, never a fresh budget. Both
  paths that show the agreement use it — account creation and re-consent after
  a revision. Still finite: raise it for a house with a long
  agreement, but a connection is never held open indefinitely. A timeout here
  drops the connection exactly as an auth timeout does (`Login timed out.
  Goodbye.`), with nothing written — an account is only created *after* consent.

Authentication:

- `auth_backoff_base` — the starting delay of the per-IP exponential login
  backoff (1s).
- `auth_backoff_cap` — the ceiling that delay grows to (30s).
- `registration_burst` — how many accounts one IP may create in quick succession
  (3).
- `registration_refill_per_sec` — how fast that refills (~1 per 120s), bounding
  how fast `accounts.json` can grow from one address.
- `global_auth_enabled` — the server-wide auth-failure ceiling, on by default.
  A deliberate, switchable-off, generously-tuned self-DoS: it slows a
  distributed credential run for everyone rather than letting it proceed at
  full speed.
- `global_auth_fail_window_seconds` — the window failures are counted over (60).
- `global_auth_fail_threshold` — failures in that window that trip it (50).
- `global_auth_cooldown_seconds` — how long the cooldown lasts (120). While
  cooling, each new auth attempt takes a small fixed delay and account creation
  is paused.

**Password policy** is not configurable, deliberately: 8 characters minimum, 128
maximum, and a common-password blocklist — no composition rules (no "must
contain a symbol"), which is current practice and produces better passwords than
the alternative. Passwords are hashed with argon2id; recovery codes are hashed
the same way and shown exactly once.

### `[reboot]`

- `rate_seconds` — per-person `.reboot` cooldown (60s).
- `pause_budget` — cap on the theater's total `@pause`/`@progress` time (240s).

### `[staff]`

- `call_seconds` — seconds between `.call_staff` calls per session (300).
  (Was `[keeper] call_seconds` before v1.54.0. ⛔ The `[keeper]` fall-back was
  deleted in v1.60.0 — a section still spelled that way is simply not read.)

### `[invite]`

- `burst` — how many invitations can be sent in quick succession (3).
- `refill_per_sec` — how fast that allowance refills (~1 per 30s).
- `ttl_seconds` — how long an invitation stays valid (5 minutes).

### The shipped examples, and obsolete keys

Two configs ship in the repo: `config.toml` (local dev: state at the repo root,
permissive LAN-friendly settings) and `deploy/install/config.example.toml` (the
reference for a server: state in `/var/lib/heresay`, doors behind a proxy,
tighter limits). Both set only the keys worth seeing; everything else defaults.
A server's own copy lives outside the repo (at `/etc/heresay/config.toml`) so
code updates never touch it.

**Obsolete keys left in an old config are simply ignored, never fatal:**
`[storage] rooms_file`, `[storage] defaults_file`, and `[world] start_room`.
Note that `start_room` is *ignored*, not quietly honoured as `entry_room` —
reviving a stale key as authoritative would be worse than failing loudly.

---

## 7. Templates — whole files to copy

The sections above explain keys one at a time. These are whole files, minimal
and working, to paste and edit. ⚠ For a **production** `config.toml` — absolute
paths, a hardened unit, a real domain — copy
[`../deploy/install/config.example.toml`](../deploy/install/config.example.toml)
instead; it is the reference deployment's own file and is kept in step with it.

### The smallest `config.toml` that runs

⛔ **Two keys are required: `[world] entry_room` and `[moderation] owner`.**
Everything else has a default (`heresay/config.py`). `load_world` refuses to
start without `entry_room` (*"[world] entry_room is not set…"*), and
`validate_config` refuses to boot while `owner` is unset or left at its
placeholder — a new account of that name may only be registered from the local
console (§5).

```toml
# Every path here is relative to THIS FILE's directory. A config in /etc with
# relative paths would put state in /etc — see §6 `[storage]`.

[world]
entry_room = "commons"      # REQUIRED: must name a room file that exists

[moderation]
owner = "admin"             # REQUIRED (no default): the account that holds owner rank at login
```

That is a running house: `house/` beside the config supplies the world, the
words and the help pages; state lands beside the config too; every limit takes
its built-in default. **What you get:** the TLS door on 2323 (if a cert exists),
the local console on 127.0.0.1:2324, the browser door on 127.0.0.1:8765.

A slightly fuller dev shape, if you want the state kept out of the way:

```toml
source_url = "https://example.invalid/your-fork"   # AGPL §13 — see §6 "Top level"

[storage]
house_dir      = "house"        # the content root; everything authored is under it
accounts_file  = "devstate/accounts.json"
mail_file      = "devstate/mailboxes.json"
mail_key_file  = "devstate/mail.key"
bans_file      = "devstate/bans.json"
state_file     = "devstate/state.json"

[world]
entry_room = "commons"
jail_room  = "quiet_room"       # optional; unset means `.jail` has nowhere to send

[moderation]
owner         = "admin"         # REQUIRED (no default)
landing_level = "newb"          # which level new accounts land in (newb|resident|staff)
```

### A complete `theme.toml`

Every key is optional — a missing file gives the built-in defaults and is never
fatal (`heresay/theme.py`).

```toml
# ⛔ The house's own NAME is not here — it moved to `config.toml [world]
# instance_name` in v1.60.0, so that configuring a house is one file. A line
# for it left in this file is ignored, and warned about at startup.

# The browser tab's icon: a path RELATIVE TO house_dir (.svg/.png/.ico/.gif/
# .jpg/.webp), read once at startup and pushed to the page as a data: URL.
# Missing, unreadable, oversized or unknown-typed = a warning and no icon.
favicon = "favicon.svg"

[ranks]
# The ladder, LOWEST FIRST. A rank's name is its identity AND the word residents
# see. The last entry is the owner's rank; the one before it is the highest
# `.promote` can grant.
order        = ["resident", "staff", "owner"]

# The lowest ESTABLISHED rank (was `floor` before v1.54.0; ⛔ that alias was
# deleted in v1.60.0). Optional; defaults to order[0].
resident     = "resident"

# The lowest rank that counts as staff: locked-door passage, staff_only rooms,
# the shared letterbox, and who `.call_staff` pages.
staff        = "staff"

# ---- Level DISPLAY names (v1.54.0) ----------------------------------------
# The four LEVELS' display words. Written out in full since v1.61.0 even though
# every value matches the built-in default: the mechanism is otherwise invisible
# until something reads wrong on a screen, which is a bad way to meet a setting.
[levels.newb]
singular = "newb"
plural   = "newbs"

[levels.resident]
singular = "resident"
plural   = "residents"

[levels.staff]
singular = "staff"
plural   = "staff"            # NOTE: "staff", not "staffs" — hence its own field

[levels.owner]
singular = "owner"
plural   = "owners"
```

#### The `[levels.*]` blocks — what they are, and what they are not

**The engine reasons in exactly four LEVELS** — `newb` < `resident` < `staff` <
`owner` — fixed in number and order, whatever your ladder looks like. Each
carries a display word, and that word is what the engine prints when it talks
about a CLASS of people: *"call the staff"*, *"staff mail"*, *"the owner of this
house"*.

⛔ **A level is not a rank, and a level word is not a rank name.** `[ranks]
order` is the ladder people climb, and a rank's NAME is the word shown for one
person's rung — what `.promote`, `.demote`, `.accounts` and `.examine` print. A
level is the BAND a rank falls into. A house can have five ranks and still
exactly four levels; `High Warden` and `Warden` are two rungs and one level, and
the level word is what `.staffmail` and `.call_staff` say about both of them.

⚠ **Singular and plural are separate fields because English plurals are
irregular.** `staff` is both; `warden` is `wardens`; `the watch` is `the watch`.
The engine will not guess at an irregular plural, and it will not
inflect your noun for you in a sentence either — see the note below.

⚠ **Leave a level unset and it wears its LOWEST RANK's name.** A level with no
block takes the name of the rank that anchors it and pluralises that by adding an
"s" — so a house whose staff rank is `warden` reads *warden* / *wardens* with no
`[levels]` block at all, which is what every pre-v1.54.0 house did and still
does. That fallback is the reason this block is optional; it is also the reason
it is worth writing out, because until you do there is nothing on disk that says
this mechanism exists.

⚠ **The one word the fallback cannot guess is an irregular plural**, which is
why the shipped `staff` level names both. (The engine knows the plurals of its
OWN default words, so a house that keeps the shipped ladder and deletes the
whole block still reads *staff* rather than *staffs* — but a house that renames
its staff rank to `watch` and expects *the watch* has to say so.)

⛔ **The engine never puts an article in front of your word.** `staff` is a mass
noun — "a staff" is not a person — so every engine string that names a level
sits in a position that works for a mass noun and a count noun alike: *the
staff*, *the wardens*, *staff mail*, *silenced by the staff*. That is a
constraint on the ENGINE, not on your choice of words: pick a plural noun, a
mass noun or a title, and the house will read.

⚠ **This is where a house with its own vocabulary differs.** Say its
`house/theme.toml` keeps the ladder `order = ["citizen", "warden", "Steward"]`
— then it sets only the level display words that are not its rank names:

```toml
[levels.staff]  singular = "warden"    plural = "wardens"
[levels.owner]  singular = "Steward"   plural = "Stewards"
```

⛔ **There is no `newb` key.** The bottom rank is **derived** from `order[0]`; a
`theme.toml` that still carries one is warned about and ignored
(`heresay/theme.py`). New accounts start at the bottom of the ladder.

⚠ **Changing `[ranks]` is a restart, and more than a restart** — see §8 and the
reorg-collapse warning in §4.

### An `agreement.txt` skeleton

⛔ **The file's CONTENT is its version** (sha256, computed once at startup), so
any edit — a comma — asks every resident to agree again at their next login.
Write it once, carefully.

```
<House name> — the terms

1. What is kept, and what is not.
   Room conversation is never written down: no logs, no history, no replay.
   The two exceptions are named here: the entry room's fading public memory
   (the last {rev_window_minutes} minutes) and your own last {revtell_count}
   private lines, held for you alone and encrypted.

2. What your account is.
   A name and a password. State plainly what else you do NOT collect — no
   email, no third-party sign-in — because that is the promise people are
   agreeing to.

3. What is recorded operationally.
   Connections are noted (a time and an address), and certain actions are
   noted against the account they concern. Never a word of what was said.
   Kept for {event_retention_days} days, then gone. Say who may read it —
   on this house, the {owner}.

4. Encryption, honestly.
   Mail and private-line buffers are encrypted at rest. Say that this is NOT
   end-to-end secrecy: whoever operates the server could read what the server
   holds.

5. Conduct and moderation.
   What gets someone gagged, jailed or banned, and how to appeal.

6. Age.
   Whatever affirmation your jurisdiction requires.
```

⚠ **The `{token}`s are real** and are the point: `{rev_window_minutes}`,
`{revtell_count}`, `{event_retention_days}` and `{owner}` are substituted from
the live config and theme **before** the hash is taken
(`AGREEMENT_PLACEHOLDERS` in `heresay/server.py`). So the document cannot come
to describe a retention rule the server has stopped enforcing — change the
setting and the agreement's text, its version, and everyone's consent move
together. An unrecognised `{token}` is left exactly as written and named once
in the log, never fatal.

⚠ Because those four values are quoted, **changing any of them is a restart**,
not a live reload — see §8.

---

## 8. What applies live, and what needs a restart

**The single most useful thing to know about configuring this house.** Most of
it applies while it runs: you edit a file, and within a few seconds it has
happened. The exceptions are few, and they are exceptions for reasons worth
knowing.

**The mechanism.** A watcher `stat()`s the watched files every
`[world] reload_poll_seconds` (default 3; `0` switches it off) and reloads the
sources that changed. Each source is validated in full before anything live is
touched, so **a syntax error changes nothing** — the old values stay, the house
stays up, and the failure is reported. `.load_report` shows the last pass.
⛔ There is no reload command, deliberately: editing the file *is* the request.

### Content — nine watched sources

| you edit | when it takes effect |
|---|---|
| `house/world/rooms/*.txt`, `map.txt`, `echoes.txt` | next few seconds — **unless a structural change hits an occupied room**, below |
| `house/greeting.txt` | the next arrival sees it |
| `house/help/*.txt` | at once, including for sessions already open |
| `house/reboot.txt` | the next `.reboot` |
| `house/theme.toml` | at once — **except `[ranks]`**, below |
| `permissions.toml` | the next command anyone types |
| `forbidden_names.txt` | the next registration attempt |
| `config.toml` | per the table below |
| `house/agreement.txt` | ⛔ **restart** — see below |

### ⚠ The three exceptions

**1. A structural room change waits for the room to empty.** Prose applies under
an occupant's feet; things that could strand or eject someone do not. Deferred
while anyone is in the room: the room being **removed**, `staff_only` turned
**on**, and an exit **removed or repointed**. Applied immediately: the name, the
description, `refusal:`, `lockable` turned on, and an exit **added**. The
reloader retries on every later poll until the room empties.

**2. A `[ranks]` change is refused live.** The rest of `theme.toml` applies; the
ladder is kept and the report says to restart. The ladder is what every stored
rank is read against, and changing it re-places accounts — that cannot happen
under a live house.

**3. The agreement is restart-gated, with its four quoted values.** Editing
`agreement.txt` is noticed and reported, never applied. If it applied live, new
logins would consent to the new version while everyone already in-world stayed
on the old one — a mixed-agreement state that drains only as people happen to
reconnect. A restart is a clean cutover: it drops everyone, and they re-consent
together, to one version.

### `config.toml`, key by key

All **90** keys, classified from the engine's own reload table
(`heresay/reload.py`), which a test holds exhaustive — every key is in exactly
one class.

⚠ **These are the engine's field names, and a few are spelled differently in
`config.toml`** (§6 has each in its own section). The ones that differ:

| in the table below | in `config.toml` |
|---|---|
| `owner_name` | `[moderation] owner` (was `[moderation] arch`) |
| `mail_purge_days` | `[mail] purge_days` |
| `mail_send_burst`, `mail_send_refill_per_sec` | `[mail] send_burst`, `send_refill_per_sec` |
| `reboot_rate_seconds`, `reboot_pause_budget` | `[reboot] rate_seconds`, `pause_budget` |
| `staff_call_seconds` | `[staff] call_seconds` (was `[keeper] call_seconds`) |
| `invite_burst`, `invite_refill_per_sec`, `invite_ttl_seconds` | `[invite] burst`, `refill_per_sec`, `ttl_seconds` |
| `base_dir` | ⚠ **not a key at all** — it is the config file's own directory, which every relative path resolves against |

| class | keys | when |
|---|---|---|
| **live** (19) | read at use time | the very next time something asks |
| **re-applied** (19) | copied into a store at boot | at once — the copy is refreshed too |
| **per-session** (18) | built per connection | **new connections only** |
| **infra** (33) | bound at boot | ⛔ **restart** |
| **agreement-quoted** (3) | quoted in the terms | ⛔ **restart**, with the agreement |

**live — 19 keys.** `agreement_seconds`, `auth_phase_seconds`, `ban_default_hours`, `ban_ip_default_hours`, `ban_max_hours`, `disconnect_grace_seconds`, `invite_ttl_seconds`, `mail_max_lines`, `mute_cap`, `profile_max_lines`, `reboot_pause_budget`, `report_capture_days`, `restart_checkpoints`, `restart_notice`, `security_window_hours`, `shutdown_cancelled_notice`, `shutdown_notice`, `source_url`, `staff_calls_window_hours`

**re-applied — 19 keys.** `auth_backoff_base`, `auth_backoff_cap`, `ban_history_days`, `churn_max_in_window`, `churn_window_seconds`, `echo_max_minutes`, `echo_min_minutes`, `global_auth_cooldown_seconds`, `global_auth_enabled`, `global_auth_fail_threshold`, `global_auth_fail_window_seconds`, `mail_purge_days`, `mailbox_cap`, `max_conns_per_ip`, `registration_burst`, `registration_refill_per_sec`, `report_box_cap`, `report_retention_days`, `rev_max_lines`

**per-session — 18 keys.** `command_burst`, `command_refill_per_sec`, `invite_burst`, `invite_refill_per_sec`, `mail_send_burst`, `staff_call_seconds`, `mail_send_refill_per_sec`, `reboot_rate_seconds`, `shout_per_seconds`, `speech_burst`, `speech_disconnect_strikes`, `speech_mute_seconds`, `speech_mute_strikes`, `speech_refill_per_sec`, `tcp_keepcnt`, `tcp_keepidle`, `tcp_keepintvl`, `wrap_columns` ⚠ Changing one of these
does not re-tune a conversation already in progress; the next connection gets it.

**infra — 33 keys, restart only.** `accounts_file`, `agreement_file`, `ban_history_file`, `bans_file`, `base_dir`, `cert_file`, `control_socket_file`, `entry_room`, `events_file`, `forbidden_names_file`, `greeting_file`, `help_dir`, `house_dir`, `jail_room`, `key_file`, `mail_file`, `mail_key_file`, `permissions_file`, `plain_host`, `plain_port`, `reboot_file`, `reload_poll_seconds`, `resume_file`, `revtell_file`, `owner_name`, `state_file`, `theme_file`, `tls_host`, `tls_port`, `trusted_proxy`, `world_dir`, `ws_host`, `ws_port` ⚠ These are sockets,
file paths, the content root, the entry and jail rooms, the admin's name and the
watcher's own interval. The live `Config` deliberately keeps the OLD value so it
never disagrees with the running process — the change is *reported*, not applied,
so nobody is left wondering why nothing happened.

**agreement-quoted — 3 keys.** `event_retention_days`, `rev_window_minutes`, `revtell_count` ⚠ Applying the value
while the text stayed frozen would make the document say one number while the
house enforced another. They move together, at a boot, or not at all.

### How a change is reported

Three ways, every time: the process log, `.load_report`'s reload block, and a
staff-action fact recorded with `actor="config"` — a change to who may do what,
made deliberately, is a staff action even when nobody typed a command.

---

## 9. Live house settings — `.ownerset`

Not `config.toml`. These are set at runtime by the owner with
`.ownerset <key> <value>`, persist in `state.json`, and — **all of them —
take effect live**: change one and every resident still on that default sees it
on their next line, with no relogging and no restart. `.ownerset` alone
lists every key with its value and built-in default; `.ownerset <key>
default` resets one. ⚠ **`.ownerset` is kept as an alias** (muscle memory),
so either spelling dispatches the same command.

**You set the words; the house keeps the grammar.** The brackets, the asterisks
and the name are the engine's and cannot be typed away, which is what keeps a
coming or going recognisable as one. Three render shapes:

| key | renders as | default |
|---|---|---|
| `login_msg` | `[{name} <phrase>]` | `enters the world` |
| `logout_msg` | `[{name} <phrase>]` | `leaves the world` |
| `jail_logon_msg` | `[*** {name} <phrase> ***]` | `arrives (jail)` |
| `enter_msg` | `{name} <phrase>` (room) | `arrives.` |
| `exit_msg` | `{name} <phrase>` (room) | `slips out toward the <room>.` |
| `afk_msg` | `{name} is AFK: <phrase>` | `away for a while` |

`<room>` in `exit_msg` is substituted with the destination room's name at the
moment of a `.go`.

Standalone lines, name-less and unwrapped:

| key | default |
|---|---|
| `jail_msg` | `You've been jailed.` |
| `release_msg` | `You have been released.` |
| `jail_reply_msg` | `Jailed residents cannot send tells or remotes.` |

And the rest:

- `broadcast_scope` — `world` (every occupied room) or `entry` (the entry room
  only) for login/logout lines.
- `desc` / `profile` — the house defaults for a resident's *own* `.desc` /
  `.profile`. Absent-means-default like the message keys; both ship **empty**,
  so out of the box nothing shows that did not show before. `profile` is
  multi-line and is written through the same editor `.profile` uses
  (`.ownerset profile edit`).
  A resident who runs a bare `.desc` while on the house default is **shown it and
  asked nothing**: it is not theirs to clear, and the offer would be an
  offer to do nothing. Only someone who wrote their own line gets the y/n.
- `dim` (`on|off`), `time` (`on|off`), `helpstyle` (`long|short`) — the display
  defaults an *unset* resident gets. **These replaced the deleted
  `defaults.txt`**, and they are live like every other key here. A resident who has chosen their own value with `.set <topic>
  <value>` keeps it until they hand it back with `.set <topic> default`.
- `raise_prompt_min` / `raise_prompt_max` — the bounds the web client's
  `.raise_prompt` clamps to, in lines (0 / 10). Pushed to every connected
  browser on change; changing them re-clamps a live session immediately.

---

### The display switches (`dim` / `time` / `helpstyle`)

`defaults.txt` is gone (1.17.0). The three switches an **unset** resident gets
are `.ownerset` keys now, like the house's wording — one settings surface
instead of two, and one fewer operator file to know about:

```
.ownerset dim on
.ownerset helpstyle short
```

These are **live** like every other `.ownerset` key (since 1.25.0 — they
used to apply only at a resident's next login): change one and everyone still on
that default sees it on their next line, with no relogging and no reboot. A
resident's own `.set dim|time|helpstyle` still wins until they hand it back with
`.set <topic> default`. The built-in values are what the shipped file used to say
(`off`, `off`, `long`), so nothing changed out of the box, and a stray
`defaults_file` key in an old config is ignored rather than fatal.

### `.topic` — a note on the door

**`.topic <text>`** — anyone present sets what the room is about (60 chars).
Shown below the `Exits:` line in `.look` and on arrival; no topic means no line.
It belongs to the gathering: it evaporates when the last person leaves, and on
restart. Never stored.

---

## 10. Resident preferences — `.set` and `.ignore`

Residents configure themselves. Both commands answer to **one grammar**:

```
.set <topic> <value>       .set <topic> default       .set default
.ignore <cat> yes|no       .ignore <cat> default      .ignore default
```

A bare `.set` or `.ignore` lists everything and marks what is still on the
default. For `.set`, "default" means the **house** value from §9 — so they
resume following you. For `.ignore`, no category is house-backed, so it always
means the built-in "shown".

**`.set` topics** (eight): `dim`, `time`, `helpstyle`, and the five wordings
`enter_msg`, `exit_msg`, `afk_msg`, `login_msg`, `logout_msg`. One line each, ≤60
characters, control characters stripped. A resident's own `exit_msg` replaces
the whole departure line and so names no destination — deliberately: leaving
without saying where is theirs to choose.

Two things `.set default` deliberately does **not** touch:

- **`.set name <NewCapitalization>`** — restyles the capitalization of your own
  name (display only; case-only, so identity, login, mail and mute stay
  casefold). It is an *identity restyle*, not a display preference, and it has
  no `default` to return to.
- **`.remember`** — a separate command tied to login (§11). A bulk reset that
  turned it off would sign every device out.

**`.ignore` categories** (eight): `login_msg`, `logout_msg`, `enter_msg`,
`exit_msg`, `afk_msg`, `shout`, `atmospherics`, and `tab_alert` (the web
client's background-tab mark — stored on the account and pushed to the browser,
since it takes effect there). All default to *shown*; quiet is something a
resident chooses.

**Two things always show, whatever is set:** staff entering a currently *locked*
room (a shut door is people choosing to be alone, and staff passing through it
is the one arrival nobody may filter away), and the AFK notice on a tell you
just sent (it answers a command you typed). `.jail`/`.free` room lines and your
own shout are likewise never filtered.

Note that five `.ignore` category names coincide with `.ownerset` keys.
They are unrelated: a house key is the *phrase*, a category is *a kind of line
to hide*.

---

## 11. Remember me — resume tokens

Off unless a resident turns it on. With `.remember on`, a **browser** is logged
back in without a password for **30 days** of inactivity; each visit renews
that. It is asked once, at account creation, defaulting to no, and explained in
`.help remember`.

What an operator should know:

- **Web door only**, by construction — the socket doors have no control channel
  and can neither present a token nor be issued one.
- The token is opaque, is never derived from the password, and is stored
  **hashed** in `resume.json`, so that file — and the nightly backups
  containing it — hold no usable credential.
- It **rotates on every use**, so a leaked token dies the next time the real
  owner returns.
- It is revoked by: any password change (all three paths — `.passwd`, the owner's
  `.resetpassword`, and the recovery-code reset), `.quit` (a full account
  logout, every device), `.remember off`, and account deletion.
- A resumed session still demands the real password for `.passwd`, `.deleteme`
  and `.recovery new` — a found phone cannot lock the owner out.

The 30-day token and the `disconnect_grace_seconds` window (§6) are
independent: after the grace window a returning resident is still logged in
silently, just into the entry room rather than where they were.

---

## 12. Runtime state (hands off)

Created and owned by the daemon; never edit while it runs; excluded from
deploys; all of it is the operator's real data:

- `accounts.json` — names, password/recovery hashes, descriptions, profiles,
  settings, consent hashes.
- `mailboxes.json.d/` + `mail.key` — mail, encrypted at rest; the key IS the
  privacy, guard backups accordingly. Three fields per message are plaintext —
  the mailbox key, the timestamp, and a `read` flag; the body is in the
  encrypted blob and nothing else ever is. **One file per mailbox**, named for
  the hex-encoded mailbox key, so a delivery writes one small file instead of
  the whole store; the directory is `0700` and each file `0600`. Schema version
  2. An existing single-file store is split on the first boot and **renamed** to
  `mailboxes.json.pre-split` — nothing is deleted, so putting that file back and
  removing the directory rolls the change back.
- `revtells.json.d/` — each resident's last private lines, same key, same
  one-file-per-account layout and the same `.pre-split` rename.
- `bans.json` — bans, with expiry.
- `resume.json` — hashed "remember me" tokens (§11).
- `state.json` — the `newusers`/`logins` toggles, the persistent jail list, and
  the `.ownerset` house settings (§9).

**Honest about the threat model:** at-rest encryption protects backups, casual
reads of the file, and a stolen disk image. It does **not** protect against an
attacker who has both root on the running host and the key file — they can
decrypt exactly as the server does. It is at-rest encryption, **not end-to-end
secrecy**, and the same is true of `revtells.json.d/`: the nightly backup
tarballs (and `make fetch-backup`) hold recent **private lines** under that same
key. Treat a backup accordingly.

Room conversation is in none of these. It is never written anywhere; that is the
covenant the rest of this file serves.

---

## 13. The web client — `web/index.html`

`web/index.html` is a single, self-contained static page — a full-viewport
terminal and nothing else. No framework, no build step, and **zero external
requests**: the one font (IBM Plex Mono) is self-hosted in `web/`. ⚠ Keep it
that way — and note the font is licensed under the **SIL Open Font License
1.1**, so redistributing the client means shipping the license text alongside
it.

**Tunable constants** sit near the top of the script and are edited directly (a
hard refresh in the browser picks them up): the `.reboot` sound synthesis
parameters (`SOUND`), the progress bar's look (`PROGRESS`), the compose box's
height cap (`COMPOSE_MAX_LINES`), the background-tab mark (`TAB_ALERT` — an
operator master switch; `enabled: false` removes it for everyone), the
reconnect-on-return behaviour (`RESUME`), and the three theme palettes. ⚠ This
is the one authored-adjacent thing outside `house/` — client tuning, not the
house's voice, and edits here are rare.

**Run it locally (exact steps):**

1. Start the daemon: `make run` (this opens the WS door on `127.0.0.1:8765`).
2. Serve the page (either way works):
   - **Simplest — open the file directly:** open `web/index.html` in a browser.
     On `file://` it auto-connects to `ws://127.0.0.1:8765`.
   - **Or a one-line static server** (keeps everything same-origin):
     ```bash
     python3 -m http.server 8099 --directory web
     # then open http://localhost:8099/
     ```
3. Create an account and talk. Open a second browser tab, or a `telnet
   127.0.0.1 2324` session, and watch both doors share the same room.

**Endpoint auto-detection** (override with `?ws=` for dev, e.g.
`index.html?ws=ws://127.0.0.1:8765`):
- `file://` → `ws://127.0.0.1:8765`
- plain `http://localhost` → `ws://127.0.0.1:8765`
- any `https://` page → same-origin `wss://<host>/ws` (production, or the dev
  Caddy below — a secure page can't open an insecure `ws://`)

**Client commands handled in the page (never sent to the server):**
- `.theme default` | `.theme green` | `.theme amber` — switch the terminal
  palette; the choice is remembered in `localStorage`.
- `.raise_prompt <n>` | `-<n>` | `0` — lift the input line off the bottom edge so
  it clears an auto-hidden taskbar. **A positive number SETS it** (`.raise_prompt
  4` → four lines up); **a negative one LOWERS it by that much** from where it is
  (`-1` → three). `0` is the floor, and bare reports. Clamped to the house's
  `.ownerset raise_prompt_min`/`_max`. Remembered in `localStorage` per
  browser; on a first visit it guesses one line up when the window reaches the
  true bottom of the screen, flush otherwise, and your own setting wins after.

**Behavior:** the input is an auto-growing compose box (a `<textarea>`): it starts
one line high and grows with content and **Shift+Enter** up to **6 visual rows**
(`COMPOSE_MAX_LINES`), the room text shrinking to make room. Six is a **hard cap**,
not a scroll point — once full, further typing is refused (the keystroke is
dropped and the beep cue plays); a paste that would overflow is trimmed to fit
with one beep; deletion and editing within the cap always work. **Enter sends,
Shift+Enter inserts a newline.** On send, a multi-line draft **collapses to one
line** (every run of whitespace, newlines included, becomes a single space, ends
trimmed) — a paragraph arrives as one `say`, a leading `.` still a command, and
the server never receives more than one line per submission. **Up/Down** recall a
session-only history (the collapsed sent line) **only when the box is empty**;
otherwise the arrows move within the draft. Password prompts swap to a masked
single-line field (Shift+Enter can't add a newline there). Scrollback is capped at
500 lines; on disconnect the screen clears to a single
`Connection closed. Press Enter to reconnect.` line (no auto-reconnect — the
server's 120s grace makes a quick manual reconnect churn-free). No notifications
of any kind — but since 1.18.0 a **background tab is marked** when a room line
arrives while you are elsewhere: the title blinks `● Commons` — whatever
`instance_name` says — and the favicon becomes a dot, both clearing the instant you come back. It says only "something
happened", never what or who; and it is drawn locally (no request). **Two
switches turn it off:** any resident can opt out for themselves with `.ignore
tab_alert yes` (it follows their account), and the operator can disable it for
everyone with `TAB_ALERT.enabled = false` in the script — the operator's wins. **Sound** is played only as the immediate, direct
result of the user's own command — the sole use today is the `.reboot` boot
theater, whose `@sound` cues are synthesised locally with WebAudio (see below).
It never plays from anyone else's activity or from a system event.

### `.reboot` — the boot theater (and how to tune it)

`.reboot` plays a personal, invoker-only 486-era boot sequence: timed lines,
pauses, a progress bar, and — on the web door — synthesised hardware sound,
framed as if the whole server is cycling (it is not; only your screen). Only the
invoker sees/hears it; the room sees one dry emote (`<name>'s terminal reboots.`).

- **The fake confirmation.** `.reboot` first asks `Reboot server? (y/n)`. The
  answer is **ignored on purpose** — `y`, `n`, empty, gibberish all run the
  sequence (that is the joke). A rate-limited caller is refused *before* the
  question, so they never see it.
- **Text & timing** live in `house/reboot.txt` (operator-editable). One
  directive per line — `@pause N`, `@clear`, `@beep`, `@sound <name>`,
  `@progress <secs> <label>` — and everything else is shown verbatim. Edit it,
  then restart (`make run-dev` / `make run`) to reload. The shipped script runs
  ~70s; the cap is `[reboot] pause_budget` in `config.toml` (default 240s).
- **Sound** is synthesised in the browser (no files, no network). Every audible
  value is a named constant in the `SOUND` block near the top of the sound code
  in `web/index.html` — `masterGain`, per-cue frequencies, filter corners, and
  durations. Edit a value, save, **refresh the browser**; no rebuild.
- **Progress bar** look (web) is the `PROGRESS` block beside `SOUND` (`width`,
  `fillChar`, `emptyChar`); the web client animates it in place. On the socket
  doors the bar is drawn as `reboot.PROGRESS_STAGES` discrete full lines (each
  newline-terminated, so tt++ split mode can't mangle it), filling to
  `[####################] done` over the duration.
- **Socket doors** have no audio channel: each `@sound` cue falls back to a
  built-in BEL pattern (`power` 1 beep, `hdd` 3, `floppy` 2, `fan` silent).

---

## 14. Live reload, and the control channel

### Warning people before the house goes away

Two things you run **on the box**, and one config block.

**A restart, on deploy.** `make deploy` now warns the house before it restarts:
by default at **2 minutes**, **1 minute** and **10 seconds**, then the restart.
You do not have to do anything — it is part of the deploy.

```toml
[world]
restart_checkpoints = [120, 60, 10]      # seconds remaining, largest first
restart_notice  = "{instance} is restarting in {t} — reconnect in a moment."
shutdown_notice = "{instance} is going down in {t}."
shutdown_cancelled_notice = "{instance} is not going down after all."
```

`{instance}` is your house's name from `[world] instance_name`; `{t}` becomes "2 minutes",
"1 minute", "10 seconds". A list written in the wrong order is sorted for you —
the first mark is the total wait, so `[10, 60, 120]` would otherwise say "10
seconds" and then leave everyone waiting two minutes.

**Taking the house down, on purpose.** Whole minutes, and it **stays** down:

```bash
cd /opt/heresay && sudo -u heresay /opt/heresay/.venv/bin/python -m heresay \
    -c /etc/heresay/config.toml --shutdown 30
```

It warns at **every whole minute** and again at **10 seconds**, then exits.
⚠ **It returns straight away** — the engine keeps the clock, so you can close
the terminal and go; the countdown is running whether or not you are watching. Changed your mind?

```bash
... --shutdown cancel        # announces it, and calls the countdown off
```

Under a minute (`--shutdown 0`) just does the ten-second warning and goes —
you meant *now*, and people still get to see it coming. Something that is not a
whole number is refused, because that is a typo — **and so is anything over a
week (10 080 minutes)**, which is the same protection: `--shutdown 200000` is one
slipped keystroke from `--shutdown 20`, and it used to be accepted.

**Say anything to everyone**, which is also what a future admin screen will use:

```bash
... --announce "back in five minutes, sorry"
```

⛔ **This channel is LOCAL ONLY** — a socket file at
`/var/lib/heresay/control.sock`, mode 0600, owned by the service. There is no
host to point it at and no port to expose: being able to use it means being on
the box with permission to open that file, exactly like `--write-permissions`.

⚠ **Three settings in the unit make this work**, already in the shipped
`heresay.service` — if you installed an older copy, update it from
`deploy/install/`:

| directive | value | why |
|---|---|---|
| `Restart=` | `on-failure` | so `--shutdown` actually STAYS down. Under `always` systemd brings it straight back |
| `TimeoutStopSec=` | `300` | so a `systemctl stop` landing mid-countdown is waited for, not killed |
| `RestrictAddressFamilies=` | must include `AF_UNIX` | without it the control socket cannot bind — the house runs, but the countdown silently does not |

**What is written down:** the *fact* of a restart or a shutdown, in the event
log, as `config`. **Never the message** — these are notices, and notices are not
kept, the same as everything anyone says here.

### Live reload — edit a file, and it happens

The daemon watches a small set of files and reloads what changes, every
`[world] reload_poll_seconds` (**3**; set `0` to switch the watcher off).

| file | what a change does |
|---|---|
| `permissions.toml` | the new table is live for everyone, including sessions already connected, on their next command |
| `forbidden_names.txt` | the next registration attempt uses the new list |
| `config.toml` | see the four classes below |
| `house/world/rooms/*.txt` | the rooms are grafted onto the live world — see the occupied-room rule |
| `house/world/map.txt` (+`.narrow`) | the map is redrawn |
| `house/world/echoes.txt` | the atmospherics change |
| `house/greeting.txt` (+`.narrow`) | the next arrival sees the new banner |
| `house/help/*.txt` | `.help <topic>` changes for everyone, including people already connected |
| `house/reboot.txt` | the `.reboot` script |
| `house/theme.toml` | the house's words and icon — **but not `[ranks]`**, see below |
| `house/agreement.txt` | ⛔ **seen, not applied — restart to apply** (a live change would split the house across two agreements) |

Not watched: `state.json` (the daemon writes that one itself) and the engine's
own code — swapping a running process's modules under it is a different and far
riskier thing than re-reading a text file. Code changes are still `make deploy`.

**A change is validated before anything is applied.** A file that cannot be read
is refused whole and the running values are kept; a file caught half-written
simply fails to parse and is picked up complete on the next pass. **`.load_report`
shows the last reload** — what changed, per file, and anything refused with its
reason.

**`config.toml` values fall into four kinds**, and the difference matters:

- **Live at once** — most world-facing values: the ban defaults and ceiling, the
  `.security`/`.staff_calls` windows, the login and reading clocks, the grace
  window, the editor caps, the invite lifetime, the owner's name.
- **Live, applied to the running house** — the ones the engine copies into
  something at boot: the `.rev` window and cap, the echo timings, the event-log
  and ban-history retention, the mailbox cap and purge age, the `.revtell` cap,
  and the connection/auth/registration limits.
- **New connections only** — the per-session buckets (speech, shouts, mail,
  invites, commands, `.reboot`, `.call_staff`) and the default width. Sessions
  already open keep the limits they started with; the reload says so.
- **Seen but not applied** — ports, hosts, certificates, the proxy setting, and
  every file path. Changing where the door is, or which accounts file is open,
  under a running house is a restart. The reload names what it saw so
  you are not left wondering.

#### Content — the occupied-room rule, and the two caveats

**Rooms are grafted onto the LIVE world**, never swapped in. Nobody is moved, no
lock is dropped, no topic is lost and the entry room keeps its memory — a
content change is a change of scenery, not of cast.

**What happens to a room somebody is standing in:**

| your edit | while occupied |
|---|---|
| the name, the description (either width), the `refusal:` line, `lockable: yes`, an exit **added** | **applies at once** |
| the room **deleted**, `staff_only: yes` turned on, an exit **removed** or repointed | **waits** until the room is empty, then applies by itself |

The line is *what could strand or eject somebody*. Prose cannot — and prose is
what you actually iterate on, so having to clear a room to fix a typo would make
this useless in the room people are most likely to be in. A waiting change is
named in `.load_report` (`cabin unchanged (exit(s) removed: stall; occupied by
2)`) and lands on its own within a few seconds of the room emptying.

⚠ **A bad room file changes nothing.** The whole world is parsed first; if it
does not hold together — an exit to a room that does not exist, a missing
`name:`, a deleted `entry_room` — the pass is rejected, the live world stands,
and `.load_report` says why. The house cannot be closed by a room file.

⛔ **A `[ranks]` change is NOT applied live.** Rename, reorder or re-anchor the
ladder and the reload keeps the old one, applies the rest of `theme.toml`, and
tells you to restart. The ladder is what every stored rank is read against, and
changing it re-places every account (§4) — that cannot happen underneath people
who are standing in rooms holding ranks. Everything else in the file — the
house's name, the staff plural, the favicon — applies at once, and the name and
icon are **pushed to open browser tabs**, which cannot discover them otherwise.

⛔ **An `agreement.txt` edit needs a RESTART.** The reload sees it and tells you
so; the running house stays on the version everyone consented to.

**You cannot have different people under different agreements** — which is why
this one is worth a restart. Why: if a new agreement took effect immediately,
people logging in from that moment would consent to the new terms while everyone
already in the house stayed on the old ones, and the house would sit in that
split until each of them happened to reconnect — days, possibly. A restart drops
everyone, and they come back and agree to the same version, together.

⛔ **The three config values the agreement QUOTES need a restart too** —
`rev_window_minutes`, `revtell_count` and `event_retention_days` (§6). Neither
the value nor the agreement changes live; both move on the next boot. If the
value applied on its own, the agreement would say "30 minutes" while the house
really kept 45, which is the one thing quoting them was meant to prevent.
Everything else in `config.toml` still applies live.

#### Pushing content without a restart

```bash
make deploy-content     # rsync house/ to the server; NO restart
```

The running engine picks it up within `reload_poll_seconds`. Same `--delete`
semantics as `make deploy`, so the repo stays the durable source for content and
a file deleted here is deleted there. Use plain `make deploy` for **code** — that
one restarts, as it must.

⛔ **Three values are quoted in the agreement and need a RESTART** —
`rev_window_minutes`, `revtell_count` and `event_retention_days`. A live edit to
any of them is **not applied** — not the value, not the agreement — and the
reload says `RESTART TO APPLY`. On the next boot both move together, and because
a restart drops everyone, the whole house re-consents to one version at once.
(Applying the value live would leave the agreement saying one number while the
house enforced another; letting the agreement move live would put new arrivals
on different terms from the people already here.)

---

## 15. Running it

- `make run` — local daemon, real local state.
- `make run-dev` — daemon on throwaway `devstate/` (safe experiments).
- `make lan` — `run` plus the static web server, with a shareable URL.
- `make cert` — self-signed dev certificate for the TLS door.
- `make deploy` — rsync the repo to the server and restart the service. State
  and server config are never touched.
- `make test` — the suite. Run it after anything nontrivial.
- `make webtest` — the browser client's harness alone (needs `node`).

### Running a public house

A production server, on one small Linux box: code in `/opt/heresay`, state in
`/var/lib/heresay`, config in `/etc/heresay`, a systemd unit with auto-restart,
a reverse proxy (Caddy) terminating TLS for the web door and serving `web/`, a
real certificate shared with the socket door, nightly state backups, and
journald retention capped to match your log-retention promise. The nightly job
keeps 14 days of snapshots on the box (`/var/backups/heresay`); getting a copy
off the box is yours to arrange (`make fetch-backup` is the reference pattern) —
and treat backups with the same care as the state dir itself, because they hold
password hashes and every encrypted mailbox.

⛔ **The tarball does NOT contain `mail.key`.** Shipping them together would put
the ciphertext and the key that opens it in the one file that leaves the machine.
So the key needs **one copy, made by hand, once** — it never rotates — stored
somewhere the tarballs are not; the exact step is in `deploy/install/README.md`
under "The one-time `mail.key` backup". ⚠ **Until you make it, the key exists in
exactly one place: the server.** ⚠ **A restore therefore takes two inputs**, and
getting it wrong is silent: unpack the tarball alone and every account, ban and
setting returns, with not one readable message.

**Encryption — who covers which door.** There is one certificate: a Let's
Encrypt cert for your domain, obtained and renewed automatically by Caddy in
production. Caddy uses it for the web side — HTTPS and the WebSocket door behind
it — terminating TLS and proxying in plaintext to the loopback WebSocket port
one hop away. The TLS socket door (2323) needs that same cert but faces the
internet directly and cannot read Caddy's private store — so a small script in
the deploy kit, the cert-copy helper (`heresay-cert-copy.sh`, run once a day by
a systemd timer), copies the current certificate out to a spot that door can
read, and refreshes it automatically whenever Caddy renews. (It reads which
domain to copy from the file `/etc/heresay/cert-copy.env` — that is what that
file is for.) The plaintext door (2324) carries no certificate: it is
unencrypted and loopback-only. In short: web clients are encrypted by Caddy,
tt++/telnet clients by the copied cert on the TLS door, and the plaintext door
is for on-the-box use only. Until that one certificate exists, nobody outside
can reach the house — the TLS door is skipped and Caddy can't serve the web
front — and only the operator's plaintext loopback door is open (which is
exactly where you register the owner on a fresh box, before turning outward).

Getting code onto that box is whichever pattern you chose in §1: clone the repo
there and `git pull` to update, or — the workshop pattern — keep your working
copy elsewhere and use `make deploy` (set `DEPLOY_HOST` to your server's SSH
host; the script refuses to run without it). The deploy ships only production
files: dev-only items (tests, git history, tooling, the shipped example configs)
are excluded, and stale copies on the server are scrubbed automatically. Either
way, every code update restarts the daemon, which empties the rooms; neither
pattern ever touches the state dir or the server's config.

After any restart, the journal prints a few lines worth glancing at
(`journalctl -u heresay -n 30`):

```
world: N room(s) loaded from <dir>; entry <id>; jail <id>
one-way passages: cellar -> stall        (only if there are any)
echoes: N line(s) loaded from <path>
help topics: N loaded from <dir>
favicon: N bytes loaded from <path>
```

If the world line names a room you did not expect, or the help-topic count is
`0`, your `house_dir` is wrong.

**You do not have to come here to read this.** The same report is also kept in
memory and shown in-client by **`.load_report`** (staff and the
owner) — the counts, the one-way passages, and **every warning the load
produced**: a retired header, a narrow file whose room does not exist, a
malformed `[ranks]` block, a favicon that could not be read, an account whose
rank the ladder no longer has. `no warnings.` is the good outcome. It describes
the run you are connected to and is never written to disk, so the way to get a
fresh one is to fix the file and restart. The journal still gets everything —
the in-client copy is additional, not a redirection.

⚠ It is a report, not a softening: everything that refuses the boot still
refuses it. An exit to a room that does not exist, a duplicate id, a missing
`name:`, an unknown header key or an unset `entry_room` will still stop the
daemon rather than appear as a line on a screen.

### Verifying with Caddy (optional)

`deploy/install/Caddyfile.example` is the production config for
`commons.example.net` (not installed here). To check the `/ws` proxy shape
locally with Caddy's built-in CA:

```bash
make run                                                   # daemon first
caddy run --config deploy/Caddyfile.dev --adapter caddyfile
# open https://localhost:8443  (accept the local CA once)
```

⚠ **The dev config carries the same Content-Security-Policy as the production
example** (minus HSTS, which has no business being pinned for `localhost`), so
this is also the rehearsal for it — a CSP is the one part of the deployment that
can white-screen the page, and localhost is where you want to find that out.
Open the browser console: CSP failures are reported there and almost nowhere
else. If the page loads but never connects, the WebSocket is being blocked and
the fix is the commented `connect-src 'self' wss://<domain>` fallback.

> Note: the plaintext dev port and `openssl s_client` echo what you type, so the
> password is visible locally during signup. This is a dev convenience; the
> transport for real use is TLS and no password is ever logged or stored in
> clear.

### The event log — `events.jsonl`

Those startup lines are the *only* kind of thing in the journal now. **Every
DOMAIN fact — who connected, who failed auth, what staff did to whom — goes
to the engine's own store instead**, `<state_dir>/events.jsonl`, `0600`, one JSON
object per line.

**Why not journald.** The engine could not read journald back without granting
the daemon `systemd-journal`, which is far more access than one owner screen
is worth; and a second file that merely *duplicated* the journal would drift the
first time somebody edited one side. So the engine owns its operational record —
and gains portability with it: no systemd, no `journalctl`, same behaviour on a
VPS, a laptop or a container.

**The split is absolute — no fact is written to both places:**

| | holds |
|---|---|
| `events.jsonl` | domain facts: connections, auth failures, login timeouts, self-deletions, staff actions |
| journald / stdout | process output: the startup lines above, malformed-config warnings, uncaught tracebacks, systemd lifecycle |

**The schema** — `{"ts": <epoch seconds>, "type": "<type>", …facts}`, with a
**closed** set of fourteen types. An unknown type is dropped with a warning
rather than written, so a typo cannot invent a category no view shows:

| type | facts | fsync? |
|---|---|---|
| `connection` | `dir` (`open`/`close`/`resume`), `ip` | |
| `login` | `account`, `ip` — the only type holding **both** | |
| `auth_fail` | `ip`, `phase` (`login`/`recovery`/`resume`) | |
| `login_timeout` | `ip` | |
| `refused` | `ip`, `reason` (below) | |
| `flood_disconnect` | `ip` | |
| `takeover` | `account`, `from_ip` (the displaced session's address) | ✔ |
| `account_security` | `event` (below), plus `account` **or** `ip` | ✔ |
| `account_deleted` | `name` (a self-delete) | |
| `staff_call` | `account` (who called; never the call) | |
| `staff_mail` | `account` (who wrote; never the note) | |
| `staff_action` | `actor`, `action`, `target` | ✔ |
| `bad_frame` | `reason`, `ip`, `sample`, and `account` if the session had one | |
| `auth_ceiling` | `phase` (`tripped`/`cleared`); the three numbers on `tripped` | |

⚠ **The type string is a stored token**, so a house upgraded from an older
release still holds rows written under the previous name; a reader of the raw
file may meet both. Nothing on any screen or in any log line prints the token
itself — every view calls it a **staff action**.

**`refused.reason`** — why entry was denied, one type rather than six because
"who was turned away, and why" is one question: `ip_banned` · `conn_cap` (per-IP
concurrency/churn) · `banned_name` (the account, post-auth) · `reserved_name` ·
`owner_name` (registering it from a network door) · `rate_limited` (registration
flood limit).

**`account_security.event`** — something that happened *to* an account that was
not ordinary use of it: `password_reset_recovery` · `recovery_codes_regenerated`
· `reconsent` (agreed to a revised agreement).

⛔ **The first two carry `account`; `reconsent` carries `ip` and no name.** That
asymmetry is the design. A password reset or a fresh code set is a *credential*
changing hands, and a record of that without the account it belongs to is no
record at all — it is exactly what someone robbed of their account needs to
find. Re-consenting is not a credential event and needs no name to be useful:
what the fact is for is "the revision is landing", which is a count over time.
(`accounts.json` holds each account's `agreed`/`agreed_hash` — the *current
state* of one person's consent — and remains the authoritative record. A log
would accumulate something different: who was at the door, and when.)

**`login`** is the one event that ties an account to an address, and
it exists so `.ban_ip` can answer "what address is this person on?" **without a
durable `last_ip` on the account** — a permanent account↔address link is the
thing the covenant declines. It is bounded by the same `event_retention_days` as
everything else, so the tie lasts exactly as long as the operational record: ban
someone not seen inside the window and the engine says so rather than reaching
for a stored address. It is read by exactly two things — resolving a ban
target's address, and counting an IP ban's collateral — and there is no
browsable "where does X connect from" lookup, because none was written. Clause 3
of the agreement discloses it in advance, which is why adding it required **no
re-consent**.

**`connection.dir = "resume"`** is a login by remembered token — a direction
rather than a type, so a view asking "who has been connecting?" gets it in the
same list. It carries no name and no token, exactly as before.

**`auth_ceiling.phase`** — the house-wide auth-failure ceiling `tripped` or
`cleared`. One type with a phase rather than two types: "the door's posture
changed" is one question, and which way is the answer. ⛔ **It carries no address
and no name, and could not honestly carry either** — the ceiling counts failures
across every source precisely because no single peer is responsible, so there is
nobody the fact is *about*. `tripped` carries the three numbers that explain why
it fired instead. ⚠ It is written once per transition, not once per attempt: a
sustained run writes thousands of `auth_fail` rows and exactly one of these.

⚠ **`bad_frame` is the one event that holds a fragment of what was sent**, in its
`sample` field, and it is a narrow exception to "facts, never content". It fires
only on a frame the engine has already judged malformed and is closing the
connection over — an attempt to smuggle a control code in behind ordinary text —
so there is no path from anything a resident legitimately typed to a `sample`.
The fragment is escaped and length-capped by the store itself rather than by the
caller. `account` appears only if the session had got that far; its absence is
itself the signal that the frame arrived before anyone logged in.

**The fsync column** marks the accountability-grade types: things a person may
later need to prove, on disk before the command that caused them returns. The
rest are flushed only — a `conn_cap` or `rate_limited` refusal arrives in bursts,
and fsyncing each would be a great deal of disk for a fact whose value is
statistical rather than individual.

⛔ **Facts only — never message content.** Nothing anyone says can reach this
file: no speech, emote, think, tell, shout, mail body, topic or profile. Names
are recorded (a moderation record without its target is not accountability);
words are not. The type set is closed precisely so that adding one is a
deliberate act with a covenant question attached, and a test speaks a canary
phrase through every verb and greps the file for it.

**Durability.** Append + flush on every event, and an **fsync on
`staff_action`** — a moderation record a power cut can eat is not a record. A
torn final line (a crash mid-append, a full disk) is skipped on read rather than
raising, and a later append starts on a fresh line so the accident cannot take a
good event with it.

**Retention** is `event_retention_days` (10), trimmed in-process on append and at
startup. Nothing to install, and the same window for every type.

**Reading it back:** `.staff_actions` (owner) shows the `staff_action`
events newest-first — when, who, what, and to whom. It is the first view over
this store and a thin one; future views are the same reader with a
different filter. Its rank comes from the command→rank table, so a house can hand
it to staff.

An **Outcome** column appears when a row on the screen carries one — today only
`.ban_ip`, which is recorded whether it is confirmed or cancelled. A screen with
none is laid out as though the column did not exist. At 40 columns the word sits
beside the time, under its own entry.

**Two more views, and both COUNT rather than list:**

- **`.security`** (owner) — six numbers over `security_window_hours`: failed
  login attempts, login timeouts, password resets made with a recovery code,
  recovery code sets regenerated, session takeovers, and accounts their owners
  deleted. **No names, no addresses, no times.** The screen answers "is anything
  happening?" and its footer points here, at the log, for "who and from where" —
  which should be a thing you decide to look up, not something a screen hands
  over on the way past. Re-consenting is deliberately not counted: it happens to
  the whole house whenever the agreement changes.
- **`.staff_calls`** (staff and the owner) — who has been reaching for
  staff over `staff_calls_window_hours`, one line per resident, `.call_staff`
  and `.mail_staff` in separate columns, busiest first. Names and counts only:
  a call is never written down and a note is mail, read by the people it was
  addressed to.

Both ranks come from the command→rank table like everything else, so a house can
move either. **Neither prints an address** — that rule is swept by a test over
both screens at both widths.

The two windows are `[world]` keys, both **hours**, both defaulting to **24**,
and both **capped at `event_retention_days`**: a screen cannot show what the
store no longer holds, so a larger setting simply means "everything retained" —
and so does `0`. There is no value that produces an empty screen, and none that
promises more history than exists. With retention itself off, the title reads
`everything kept`.

⚠ **fail2ban moved with it.** The auth-failure line is no longer in the journal,
so the jail reads the event log instead. The repo ships both the filter and the
jail, and `deploy/install/README.md` says where they go. ⛔ **An existing install
must update its filter or the jail silently stops matching**, so install the
shipped pair rather than leaving an older one in place.

Two other event types would suit a second jail if you ever want wider cover —
`refused` with `reason: "ip_banned"` (an already-banned address still knocking)
and `flood_disconnect` (a session dropped for sustained flooding). Neither is
wired up here, and neither needs to be: the app already refuses the first and
drops the second. It is only worth doing if you want the *firewall* to stop
paying for the connection at all.

---

## 16. When the house will not start

⛔ **Configuration failures only.** A daemon that ran and then stopped, a door
that will not answer, a certificate that expired — those are operational and
live in [`OPERATIONS.md`](OPERATIONS.md). What follows is the house refusing to open in
the first place, which is nearly always a file this manual describes.

**The shape of all of it:** the daemon writes one message naming the file and
exits. Under systemd it is in `journalctl -u heresay`. It does **not** start
degraded, and ⛔ **there is no fallback world** — a house that looks open and is
not is worse than one that says why.

| symptom | cause | fix |
|---|---|---|
| `[world] entry_room is not set in config.toml` | the one required key is missing | set it (§7) |
| `[world] entry_room 'X' is not a room in …` (and it lists the rooms it found) | the name does not match a filename stem | check the stem — `rooms/commons.txt` is `"commons"` |
| the same error listing **no** rooms, or a no-room-files error | `house_dir` points somewhere with no `world/rooms/` | ⚠ **the classic:** `house_dir` is resolved **relative to the config file**, so a config in `/etc/heresay/` looks for `/etc/heresay/house`. Point it at an absolute path |
| help topics silently zero, but the house runs | the same wrong root, quieter | startup logs `help topics: N loaded from <dir>` — read the path |
| a room file error naming the file and line | a missing `name:`, an unknown header key, a header line with no colon, a bad flag value, a `refusal:` on a room that is not `staff_only`, an exit to a room that does not exist, or two files with the same id | fix the file; §2 lists all of them |
| a permissions error naming a value | `permissions.toml` holds a value that is neither a rank in `[ranks] order` nor `"disabled"` | every value is a rank NAME (or `"disabled"`); run `--write-permissions` (§4) |
| a theme error about `resident` | `[ranks] resident` names no rank, or sits above `staff` | the anchors must be in ladder order (§4) |
| the house refuses to start naming a rank and an account | a rank name collides with an account name | rename the rank, or the account |

⚠ **`theme.toml` is otherwise never fatal.** A malformed `[ranks] order` falls
back to the built-in ladder and logs one line; a `staff` naming no rank makes
only the top rank staff. Only the two cases above stop the boot.

### "I changed it and nothing happened"

Nine times in ten the key is **infra** — a path, a port, the entry room, the
admin's name — and infra is restart-only. Check §8's table first,
then `.load_report`, which names every key it saw change and says which were
applied, which were held for new connections, and which need a restart.

⚠ If the reload was **rejected**, `.load_report` says so with the reason and the
old values are still running. A half-written file caught mid-save fails to parse,
is rejected, and the next poll picks up the finished one.

### The page loads but never connects

A Content-Security-Policy problem at the web edge, not a config error here — the
browser console says so, and the fix is in
[`../deploy/install/README.md`](../deploy/install/README.md) (§9). You can
rehearse the whole thing on localhost first with `deploy/Caddyfile.dev`, which
carries the same policy; see §13.
