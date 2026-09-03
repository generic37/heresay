# Commands

What a resident can type. The engine's own `.help` is generated from the live
permission table, so it always tells the truth about *this* house; this page is
the same ground in prose, and the place to read before you have a house running.

⚠ Which rank a command needs is **configuration**, not a constant — see
[`CONFIGURING.md`](CONFIGURING.md) for `permissions.toml`. The tiers below are
the shipped defaults.

## The commands

Anything not starting with `.` (or one of the `;` / `>` / `<` prefixes) is
speech to your current room.

| input | effect |
|---|---|
| `<text>` | `Corvid says: the rain finally stopped.` |
| `.emote <text>` / `.e` / `.em` / `;<text>` | emote — `Corvid stretches.` |
| `.think <text>` | `Corvid thinks . . oO( hmm )` |
| `.who` | who's on — a dated wholist: name, description, room, idle time |
| `.look` | re-show the room and its present company |
| `.go <room>` | move to a connected room (unambiguous prefixes ok) |
| `.map` / `.m` | print the world map |
| `.topic <text>` | set this room's topic (it goes when the room empties) |
| `.rev` | the entry room's recent lines, fading over ~30 min (that room only, in memory) |
| `.revtell` | your own last 20 tells and remotes (encrypted, yours alone) |
| `.tell <name> <text>` / `.t` / `>` | private message anywhere; both of you see `>> Name says: text` |
| `.remote <name> <text>` / `.r` / `<` | private emote; they see `>> Name waves`, you see `>> (to Them) Name waves` |
| `.shout <text>` | heard in every room (used sparingly) |
| `.lock` / `.unlock` | shut/open the door in a lockable room |
| `.invite <name>` / `.join <name>` | invite someone / join whoever invited you |
| `.desc <text>` | set your one-line description |
| `.set` | show/change preferences — all of them (`.help set`) |
| `.set dim on\|off` / `.set time on\|off` | the two display switches |
| `.set helpstyle long\|short` | the full `.help` grid, or a ten-line index |
| `.set enter_msg\|exit_msg\|afk_msg\|login_msg\|logout_msg <text>` | your own wording for coming and going |
| `.set <topic> default` / `.set default` | one topic, or all eight, back to the house default |
| `.ignore <cat> default` / `.ignore default` | one category, or all of them, back to shown |
| `.profile` | write/replace your profile (multi-line editor) |
| `.examine <name>` / `.exa` | read someone's description + profile (online or not) |
| `.mail` | read your mailbox |
| `.mail <name> <text>` | send a one-line message (recipient online or offline) |
| `.mail <name>` | compose a multi-line letter (editor) |
| `.dmail <n>` | delete message n (really deleted) |
| `.call_staff <text>` | call for staff — reaches everyone on duty at once (for real trouble, not chat; works while gagged, sternly rate-limited) |
| `.mail_staff <text>` | leave a note in the shared staff letterbox (one-liner, or bare to open the editor) |
| `.passwd` | change your password (echo-off prompts) |
| `.mute <name>` / `.unmute <name>` | stop/resume hearing someone (this session) |
| `.ignore` / `.ignore <category> yes\|no` | hide a KIND of line — logins, arrivals, AFK, shouts, murmurs, the browser tab mark (`.help ignore`) |
| `.help` | the command list (`.set helpstyle` picks long or short); `.help <command>` for one page |
| `.version` | what is running, e.g. `Commons, Heresay version 1.61.4` |
| `.agreement` | re-read the house agreement — the text itself, nothing to answer |
| `.quit` | leave (announced) |

In the multi-line editor (`.profile`, `.mail <name>`): type your text, then
`.end` on its own line to save or `.abort` to discard.

### Moderation — the ranks

Ranks are an ordered **ladder** (`theme.toml` `[ranks] order`, by default
`resident` < `staff` < `owner`), and everything from the **staff** rank
upward holds the moderation tools. Everyone starts at the bottom of the ladder; privileged
commands are *invisible* below their rank (typing one gets the same "Unknown
command" as a typo). The **owner** is a single account named in `config.toml`
(`[moderation] owner = "..."`); that account holds the top rank at login whatever
is stored for it. **`.help` is one generated screen** (v1.29.0): it shows a
section per rank from the floor up to yours, so a resident sees their own
commands, staff see theirs added, and the owner sees all — built from the
command→rank table, so it can never list something you cannot run.

**Who may run what is a file you edit: `permissions.toml`**, beside
`config.toml`, one line per command whose value is **the name of a rank in your
ladder** — meaning that rank and everyone above it. Nothing else is accepted:
a value that is not one of your ranks refuses the boot rather than being guessed
at, because a mis-resolved permission is invisible (v1.43.0; the engine's own
role words are resolved to your rank names when the file is written, and your
existing file is converted for you at deploy).
It is config like `config.toml`: **you own it, the daemon only reads it**, and
your edits **apply live** — within a few seconds, for sessions already
connected, with no restart. `python -m heresay --write-permissions`, run as root
(your deploy does it), creates the file and appends a marked line for anything a
later release added; a command the file does not name simply uses its built-in
default in the meantime, so a missing or unreadable file can never stop the
house. A file it cannot *parse* is refused and the running table kept, so a typo
cannot close a live house; `.load_report` shows what changed and anything
refused. `config.toml`'s world-facing values reload the same way (ports, certs
and paths still need a restart, by design).

**The house is warned before it goes away** (v1.46.0). `make deploy` broadcasts a
countdown — 2 minutes, 1 minute, 10 seconds by default — and only then restarts;
and `--shutdown <minutes>` takes the house down on purpose, warning at every
whole minute and again at ten seconds (capped at a week, so a slipped keystroke
can't schedule a shutdown for the next century). It returns at once (the engine keeps the
clock, so you can close the terminal), `--shutdown cancel` calls it off, and
`--announce <text>` says one line to everyone. ⛔ The trigger is a **0600 socket
file in the state dir, never a port**: using it means being on the box. The
notices are system lines — shown to everyone, and like everything else said here,
**never written down**; only the fact of a restart is logged.

**So does the content** (v1.39.0): rooms, the map, the echoes, the greeting, the
help pages, the `.reboot` script, the house's words and icon, and the agreement
all apply **without a restart**. Rooms are *grafted* onto the live world —
nobody is moved, no lock or topic is lost, the entry room keeps its memory — and
a change that could strand somebody (a room deleted, an exit removed,
`staff_only` turned on) **waits for that room to empty**, while prose applies
under an occupant's feet. A room file that does not parse is rejected and the
live world stands. Two deliberate exceptions, both needing a **restart**: a `[ranks]` change (it
re-places every account) and the **agreement** — its text, or the three config
values it quotes. A live agreement change would put new arrivals on the new terms
while everyone already in the house stayed on the old ones; a restart drops
everyone and they come back to one version together. **`make deploy-content`** rsyncs `house/` *without*
restarting, for exactly this.

> **The words are `theme.toml`'s; the ROLES are the engine's.** A rank's name
> *is* its display word (v1.27.0) — the shipped ladder says `resident`,
> `staff`, `owner`, and this page uses those. What the engine keeps fixed
> are the four LEVELS (`newb` < `resident` < `staff` < `owner`) and their role
> anchors, which resolve against whatever ladder a house defines. The command names (`.call_staff`,
> `.staffmail`, `.promote`, …) are fixed verbs, not theme. See "Theme words"
> below.

Using the default display names, the staff commands are:

| rank | command | effect |
|---|---|---|
| staff | `.gag <name>` / `.ungag <name>` | server-wide, visible silence; the gagged may still `.call_staff` or `.tell` any of them to appeal |
| staff | `.jail <name>` / `.free <name>` | move to the jail room, if `[world] jail_room` names one (no exits, hidden from `.map`/`.go`) / back to the entry room |
| staff | `.boot <name> [reason]` | disconnect a session (also frees a stuck "already connected" account) |
| staff | `.ban <name> [hours]` / `.unban <name>` | refuse an ACCOUNT entry (default 24h; **persists** in `bans.json`; auto-expires) |
| staff | `.ban_ip <name> [hours]` / `.unban_ip <name>` | ban the ADDRESS they're on — shows who it catches, then asks. **No IP is ever printed** |
| staff | `.bans` | the active bans, by name |
| staff | `.staffmail` / `.dstaffmail <n>` | read the shared staff letterbox / clear a message for all staff |
| owner | `.promote <name>` / `.demote <name>` | one rung up / down the ladder (persisted) |
| owner | `.mailpurge <name>` | delete an entire mailbox for spam cleanup — **never reads it** |
| owner | `.deleteuser <name>` | erase an account permanently — two gates (re-type the username, then your own password); removes the account, mailbox and private lines, frees the name, disconnects them. Mail they *sent* and any ban on the name survive |
| owner | `.resetpassword <name>` | set someone's password when they've lost both it and their recovery codes — two gates; recovery codes untouched |

**How residents reach staff.** `.call_staff <text>` calls everyone on duty
at once (`Name (room) calls for staff: …`) — the discoverable emergency
channel, works while gagged, sternly rate-limited (1 per 5 min), and the call
text is never logged (only the fact). If no one is awake it points to
`.mail_staff <text>`, which drops a note in the **shared staff letterbox** — an
encrypted, capped, purged mailbox (`#staff`) that only staff can read; they're
told `You have staff mail.` at login. **Every rank name is reserved from
registration** — a rank word is an identity here, so `staff` and `owner`
can never be taken on the default ladder. (These verb and message strings use
the default display words; retheme the vocabulary in `theme.toml` and the
*messages* follow, though the verbs stay.)

**Names you won't host are a file you edit: `forbidden_names.txt`** (v1.44.0),
beside `config.toml` — one name per line, `#` comments, case-insensitive.
Impersonation bait, `admin`/`root`, slurs, whatever you choose; the engine has
no opinion. It **adds** to the built-in rank reservations and can never
unreserve one, it applies **live** like `permissions.toml`, and absent simply
means you have no list. A blocked attempt is told only `That name isn't
available.` — never which entry, never that a list exists. If somebody already
holds a name you add they **keep it**; the log says how many accounts that is
and never who.

**Moderation has a target-rank rule** (v1.41.0, every person-acting command
since v1.42.0): you may `.gag`, `.ungag`, `.jail`, `.free`, `.boot`,
`.mailpurge`, `.resetpassword`, `.deleteuser`, `.ban`, `.ban_ip`, `.unban`,
`.unban_ip`, `.promote` or `.demote` only someone whose rank is **strictly below
your own**, and never the owner. `.promote` additionally cannot lift anyone
(including you) above your own rank, and `.demote` cannot reach a peer. The one
exception is that **the owner can free themselves**, so a jailed owner
is never locked in; nobody else can self-free. `.mailpurge` also asks for your
own password: it destroys a mailbox irreversibly, though it still never reads
one.

**`.ban_ip` will not ban staff, even indirectly.** If the address would catch
any staff account — the person named, or anyone else seen on it — the owner
is told which and asked again, and everybody else is refused with a notice that
names nobody and counts nothing. The refusal reads the same even when the staff
account caught is your own, so `.ban_ip` cannot be used to work out who shares
an address with whom.

**Mail is inviolable:** no one — at any level — can read anyone's *private* mail
but their own (the shared staff letterbox is addressed to staff by design). There
is no private-mail-reading power at any level. `.mailpurge` deletes without
decrypting. Staff actions are logged as facts (actor, action, target) for
accountability — never message content.

### Theme words (`theme.toml`)

The words the world uses for itself and its ranks are operator-editable in
`house/theme.toml` (missing file → built-in defaults, never fatal):

⚠ **The house's own NAME is not in this file** — it is `config.toml [world]
instance_name` (moved there in v1.60.0, so that configuring a house is one file
to edit). A `theme.toml` still carrying the key is ignored and warned about.

```toml
[ranks]
order    = ["resident", "staff", "owner"]   # the ladder, lowest first
resident = "resident"     # the lowest established rank (was `floor`; default: the bottom)
staff    = "staff"        # the lowest rank that counts as staff

# Level DISPLAY names (v1.54.0): each of newb/resident/staff/owner has a
# configurable singular + plural word, separate fields because English plurals
# are irregular. A level with nothing here shows its own anchor rank word. The
# skeleton writes all four out (v1.61.0) so the mechanism is visible before
# something reads wrong.
[levels.newb]      singular = "newb"      plural = "newbs"
[levels.resident]  singular = "resident"  plural = "residents"
[levels.staff]     singular = "staff"     plural = "staff"
[levels.owner]     singular = "owner"     plural = "owners"
```

`[world] instance_name` (in `config.toml`) is threaded through every line the
server generates that names the world — `.version` (`Commons, Heresay version …`),
the `.map` title, the `.examine` owner line (`The owner of Commons.`), and the
login notices — so setting `instance_name = "Mos Eisley"` moves them all at
once.

**`order` is the rank ladder itself** (v1.27.0), not just a list of words: a
rank's name is its identity and its display word both, and `accounts.json` stores
that name. Add a tier by adding a name — `["novice", "resident", "staff",
"owner"]` — and the permission thresholds follow with no code change. The last
entry is the owner's rank; the one before it is the most `.promote` can grant.

**Two anchors cut the ladder into level bands**: `resident` (the lowest
*established* rank, formerly `floor`) and `staff`. ⛔ **There is no `newb` key** —
the bottom rank is always `order[0]`, derived rather than set, and a `theme.toml`
that still carries one is warned about and ignored. `resident` defaults to the
bottom too, which collapses the newb band to nothing, so a house that sets
neither behaves exactly as it always has. Want a probationary tier? Put a rung at
the bottom of `order` and promote people out of it. ⚠ Change the ladder's shape
and the next boot **collapses every account into its band** (staff to the lowest
staff rank, established to `resident`, everyone else to the bottom, the owner
untouched) — you re-promote from there. A restart that changes nothing never
touches a rank.

The **internal identifiers never change**: the config still names the owner by
account name, the shared letterbox is `#staff` on disk (`#keeper` before v1.54.0,
read as a fall-back), and command names (`.promote`, `.call_staff`, …) are code,
not theme. (Authored content — the
greeting, help pages, agreement and the room descriptions — is yours, and its
wording is left untouched.) Two caveats worth knowing: **renaming a rank does not
move the people in it** (a name the ladder no longer knows reads as the floor;
the server counts them at startup and says so), and **a rank may not share a name
with an account** — that one refuses to start, because a rank word is an
identity here.

## Reports, and where the source lives

`.report <name> <what happened>` files a report about someone — one line, or
`.report <name>` alone for the editor. It is `.mail` underneath, addressed to a
shared report box rather than to a person, so it is encrypted at rest like any
letter. `.read_report` reads that box; by default only the owner can, so a
report *about* staff does not land in front of staff — but that is a
permission like any other, and the operator can widen it.

There is no way to delete a report. One leaves the box only by expiring, after
`report_retention_days` (**10**). A box its own reader could quietly empty would
not be much of a record.

⛔ **The box is not the record — the capture file is.** At the instant a report is
filed the daemon writes the whole thing to the operator's disk: the reporter, the
accused, the full text, the filing time, both account records, and every
event-log row about either of them. One plain-text file per report, in a
`reports/` directory inside the state dir, 0600 inside a 0700 directory, kept
`report_capture_days` (**375**). That is why ten days is enough for the box: the
report is already preserved, along with the evidence that would otherwise have
aged out of the event log at day ten. Before this, a report *outlived its own
evidence* — 100 days in the box against ten in the log — so at day eleven an
accusation could be read with nothing left to check it against.

⛔ **Nothing in the engine ever reads a capture file back.** There is no command,
at any rank, that lists, counts, searches or prints them, and none is planned —
it is write-only from the engine's side, read by the operator over ssh. ⚠ It is
deliberately plaintext, and that is a stated trade: `CONFIGURING.md` sets out what
it costs and why the alternative — an unanswerable accusation — was judged worse.

`.source` prints where this house's code came from, beside the version running.
⚠ **A public instance must set `source_url` in `config.toml`** — the AGPL obliges
an operator running a modified copy to offer its users the source, and unset,
`.source` can only say so.
