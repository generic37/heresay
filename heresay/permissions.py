# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`permissions.toml` — who may run what, as a file the operator owns.

**Why this is a file and not a block in `state.json`.** The command table lived
inside the daemon's own runtime-state file from v1.28.0 to v1.36.2, and that was
wrong in three ways at once:

- ⛔ **It was silently clobbered.** `StateStore._save()` writes the whole payload
  from memory, and `.ownerset` / `.newusers` / `.logins` / `.jail` / `.free`
  all call it. An edit made while the daemon was running survived until the next
  staff member jailed somebody, and then vanished with no error.
- **It was JSON**, so it could hold no comments and no grouping — the two things
  that make a permissions file readable.
- **It lived in `/var/lib/heresay/`**, the directory the manual tells operators
  to keep their hands off.

Config the operator edits and state the daemon writes **must not share a file**.
So the table moved out, to a TOML file beside `config.toml`, which the daemon
reads and never rewrites except to append a new command's default line.

⚠ **It is deliberately NOT in `house/`.** `deploy.sh` rsyncs that directory with
`--delete --delete-excluded`, so a file there is overwritten by every push from a
developer's checkout — and adding an exclude would make the deploy *delete* it on
the server. `/etc/heresay/` is outside the rsync entirely, which is exactly why
`config.toml` lives there.

⛔ **THE DAEMON NEVER WRITES THIS FILE.** It is config, exactly like
`config.toml`: root edits it, the daemon reads it. v1.37.0 had the daemon create
and top up the file itself, and that **crash-looped in production on the first
boot** — the hardened systemd unit mounts `/etc` read-only, so
`p.write_text(...)` raised `OSError: [Errno 30] Read-only file system` and took
the house down with it. The lesson is not "find a writable directory": a daemon
that must be able to write its own configuration is a daemon that cannot be run
with its configuration protected, and protecting it is the point.

**Self-completing IN MEMORY.** A command the file does not name takes its
registry seed default **in the table the engine holds** — the file is untouched.
So the running table is always complete, an unwritable (or absent, or
half-populated) file is never fatal, and the FILE only changes when a person
changes it. `python -m heresay --write-permissions`, run as root at deploy time,
is what creates it and appends the lines a new release added; see `write_file`.

⛔ **VALUES ARE RANK NAMES, and nothing else (v1.43.0).** A value is legal iff it
is a rank in `theme.toml`'s `[ranks] order`. The role words the engine thinks in
— `newb` / `floor` / `staff` / `arch` — are **not** values for this file, with no
exception.

⚠ **v1.38.0 accepted both, and v1.43.0 took the anchors back out.** Accepting
two vocabularies for one field made the file impossible to read straight: on a
house whose staff rank was `warden`, `gag = "staff"` and `gag = "warden"` meant
the same thing, one of them was a word that
appears nowhere in the operator's own `theme.toml`, and "which ranks may I write
here?" had two answers. The anchors remain what the CODE seeds with — they are
the portable, house-independent default — but they are **resolved to this
house's rank names when the file is WRITTEN** (`--write-permissions`), so what
lands on disk names ranks the operator recognises. See `canonicalize`.

⛔ **A value that is not a rank REFUSES THE BOOT. No exception, no
resolve-with-warning, no belt.** A typo, a rank deleted from `[ranks]`, and a
leftover anchor word are all the same fact — *that is not a rank* — and the
house does not open until it is fixed. The maintainer, 2026-08-03: *"I don't want it
running if the configuration is corrupt — refuse boot and make me fix it."* At
RUNTIME the same value discards that reload and the running table is kept: a
live house must never be closed by a typo, which is the older rule and wins
there.

**Two different failures, two different answers**, and the line between them is
the same one the world loader draws:

- a **value** that is not a legal rank → the file is REJECTED whole. A permission
  table with an unreadable entry has no safe reading, and guessing which way to
  err on somebody's access is not the engine's call.
- an **unknown command name** → that line is skipped with a warning, the rest of
  the file is used. A name is far more likely to be a stale line from a previous
  release than a mistake with consequences, and rejecting a whole table — which
  at runtime means keeping the OLD one — because of one dead line would punish
  the house for the engine's own history.
"""

from __future__ import annotations

import logging
import re
import tomllib
from pathlib import Path

log = logging.getLogger("heresay")

# The role anchors, mirrored from the registry so this module does not import
# the command package (which imports session, which imports state — the same
# cycle the seed injection exists to avoid).
#
# ⛔ These are what the CODE seeds with and what `--write-permissions` RESOLVES.
# They are not legal values in the operator's file (v1.43.0); this tuple is here
# to recognise them — in a seed, in a legacy block, or as the leftover the
# canonicalizer rewrites — never to accept one.
# The ROLE words a seed emits, and the only ones `canonicalize` rewrites to a
# rank name. ⚠ They are not legal VALUES in the file — a value is a rank name or
# `disabled` — they are what the CODE seed says before `--write-permissions`
# resolves it against this house's ladder.
ANCHORS = ("newb", "resident", "staff", "owner")

# ⛔ THE ONE VALUE THAT IS NOT A RANK (v1.56.0).
#
# `disabled` means the command **does not exist in this house**. Not hidden,
# not refused, not permission-denied — absent. Typing it gets the ordinary
# unknown-command line, byte for byte what gibberish gets; it is missing from
# `.help` and from every listing; and NO PERMISSION CHECK RUNS, because there is
# nothing to check.
#
# ⛔ **It binds EVERYONE, the owner included.** A value that exempted the owner
# would be a rank, not a disable, and the difference is the whole point: an
# operator who turns a command off has turned it off, not reserved it for
# themselves.
#
# ⛔ **There is NO protected set.** `.passwd`, `.quit`, `.recovery`, `.agreement`
# — all disablable. The maintainer's explicit call: *"no matter how stupid means
# just that."* It is the operator's house. The escape hatch is structural rather
# than a guard list: this file is root-owned on the server and the daemon never
# writes it, so nothing done in-world can lock anybody out of fixing it, and the
# fix is live-reloaded within seconds.
DISABLED = "disabled"

SECTION = "commands"


def resolved(value: str, ladder) -> str:
    """A seed anchor (or a name) → the rank NAME it means on this ladder.

    The one place the code's generic vocabulary meets a particular house.
    Without a ladder the value is returned as it stands — the isolated-parse
    case, where there is no house to resolve against.
    """
    if ladder is None:
        return value
    return ladder.resolve_anchor(value) or value


class PermissionsError(ValueError):
    """A permissions file with no safe reading.

    Fatal at boot, and at runtime the reason a reload is REJECTED — the old
    table stays, the house stays up, and the operator is told. A live house must
    never be closed by a typo.
    """


class Permissions:
    """The parsed result: a table, plus what the parse had to say about it."""

    def __init__(self, table: dict, added=(), skipped=(), from_file=True):
        self.table = dict(table)
        # Commands the file did not name, which took their seed default IN
        # MEMORY. Reported so an operator knows to run `--write-permissions`;
        # never written to the file by the daemon.
        self.added = list(added)
        # Lines naming something the engine does not have (kept out of the table).
        self.skipped = list(skipped)
        # False when there was no readable file at all — the table came from the
        # seed (and the legacy block, on an upgrading house).
        self.from_file = bool(from_file)


def parse(text: str, seed: dict, source: str = "permissions.toml",
          known=None, ladder=None) -> Permissions:
    """Parse the file's text into a table. Never touches disk.

    ``seed`` is the registry's built-in defaults (the self-completion source).
    ``known`` bounds which names are real commands; it defaults to ``seed``'s
    keys, which is the same set.

    ``ladder`` is the `Theme`, and passing it turns on the STRICT reading: a
    value must be a rank NAME in `[ranks] order` and anything else — a typo, a
    deleted rank, a leftover role word — is a `PermissionsError` (v1.43.0;
    v1.38.0 also took the role words, see the module note). Without a ladder —
    a test parsing in isolation — only the shape is checked.
    """
    known = set(known if known is not None else seed)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PermissionsError(f"{source}: not valid TOML — {exc}") from None

    raw = data.get(SECTION, {})
    if not isinstance(raw, dict):
        raise PermissionsError(
            f"{source}: [{SECTION}] must be a table of command = \"rank\", "
            f"not {type(raw).__name__}")

    table: dict[str, str] = {}
    skipped: list[str] = []
    for name, value in raw.items():
        if not isinstance(value, str):
            raise PermissionsError(
                f"{source}: {name} = {value!r} is not a rank name — values here "
                f"are the rank names from theme.toml [ranks] order")
        if name not in known:
            # A stale line, almost certainly: a command a past release had.
            skipped.append(name)
            continue
        # ⛔ STRICT, and NAMES ONLY (v1.43.0). A typo, a rank deleted from
        # [ranks], and a leftover role word are one fact — that is not a rank —
        # and they get one answer: refuse. There is no resolve-with-warning and
        # no degrade path, because the symptom of a silently mis-resolved value
        # is a command out of reach of the people it was given to, and NOBODY
        # NOTICES: the gate is invisible by design. Fatal at boot; at runtime
        # the whole reload is discarded and the old table kept, so a live house
        # is never closed by a typo.
        # ⚠ `disabled` is legal everywhere a rank name is, and is the ONLY
        # value that is not one. A rank may not be CALLED `disabled` — that
        # collision refuses the boot in `theme.load_theme`, so the two readings
        # can never both be available.
        if (ladder is not None and value != DISABLED
                and value not in ladder.order):
            raise PermissionsError(_bad_value(source, name, value, ladder))
        table[str(name)] = value

    # Self-completion, IN MEMORY. The seed speaks in role anchors — the
    # house-independent default — so it is resolved to this house's rank names
    # here, which is what makes the running table identical whether or not
    # `--write-permissions` has been run yet.
    added = [k for k in seed if k not in table]
    for k in added:
        table[k] = resolved(seed[k], ladder)
    return Permissions(table, added=added, skipped=skipped)


def _bad_value(source: str, name: str, value: str, ladder) -> str:
    """Why this value is refused, in the operator's own vocabulary.

    A leftover anchor gets a sentence of its own — not as an exception, but
    because "not a rank" is unhelpful about a word that WAS legal one release
    ago and still appears in the engine's own documentation.
    """
    msg = (f"{source}: {name} = {value!r} is not a valid rank. Values here are "
           f"the rank names in theme.toml [ranks] order: {list(ladder.order)}"
           f" — or {DISABLED!r}, which removes the command from this house")
    if value in ANCHORS:
        msg += (f" — {value!r} is a role the engine uses internally, not a value "
                f"for this file. Run --write-permissions to convert the file, or "
                f"write the rank you mean")
    return msg


# ---------------------------------------------------------------------------
# Rendering — the file the operator actually reads
# ---------------------------------------------------------------------------

HEADER = """\
# permissions.toml — who may run what.
#
# One line per command. A value is a RANK NAME from `[ranks] order` in
# theme.toml. It means that rank AND EVERYONE ABOVE IT.
#
# The one other legal value is "disabled", and it does not mean "nobody may run
# this" — it means THE COMMAND DOES NOT EXIST HERE. Typing it gets the same
# reply as typing nonsense, it is absent from .help, and no permission check
# runs at all. It binds EVERYONE, the owner included, and nothing is protected
# from it: if you disable .passwd, your house has no .passwd. The help page
# stays shipped, so re-enabling is a change to this line and nothing else.
#
# Nothing else is accepted: a value that is not one of your ranks — a typo, a
# rank you have since removed, or one of the engine's internal role words
# (newb / floor / staff / arch) — will REFUSE THE BOOT rather than be guessed
# at. A mis-resolved permission is invisible (the gate does not announce
# itself), so it is an error to fix and not a policy for the engine to pick.
#
# Renaming a rank in theme.toml therefore means changing it here too, in the
# same restart. That is the trade for a file that says what it means.
#
# Edits apply LIVE: the daemon notices the file changed and reloads it within a
# few seconds. A file that cannot be read is rejected and the running table is
# kept — a typo cannot close a house that is already open. `.load_report` shows
# what changed, and what was refused.
#
# A command missing from this file takes its built-in default and the line is
# appended below, marked. Delete a line and it comes back at the default: say
# what you mean with a VALUE, not with an absence.
"""

# The order categories are printed in, and their headings. Code-side, like the
# registry's own category list: this is layout, not configuration.
CATEGORY_ORDER = ("talking", "private", "world", "room", "self", "mail",
                  "house", "account", "system", "misc", "moderation",
                  "accounts", "administration")


def render(table: dict, categories: dict = None, version: str = "") -> str:
    """The whole file, grouped and commented. Used to WRITE it — once, on the
    migration or on a first boot — never to rewrite an operator's copy.

    ``categories`` maps command -> category (from the registry) so the file
    reads in the same groups the help screen does. Absent, it is one flat list.
    """
    lines = [HEADER]
    if version:
        lines.append(f"# Written by Heresay {version}.\n")
    lines.append(f"[{SECTION}]")

    categories = categories or {}
    grouped: dict[str, list[str]] = {}
    for name in sorted(table):
        grouped.setdefault(categories.get(name, ""), []).append(name)

    order = [c for c in CATEGORY_ORDER if c in grouped]
    order += [c for c in sorted(grouped) if c not in order]
    for cat in order:
        if cat:
            lines.append(f"\n# {cat}")
        # ⚠ ONE COLUMN PER SECTION, set by that section's longest key — the
        # same rule `top_up` applies when it inserts a command later. They have
        # to agree: if the file this writes is not the shape the top-up
        # produces, every deploy re-flows the file and the diff is noise.
        width = max(len(n) for n in grouped[cat])
        for name in grouped[cat]:
            lines.append(f'{name:<{width}} = "{table[name]}"')
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Canonicalization — the one-time migration off the role words
# ---------------------------------------------------------------------------

# `name = "value"`, with whatever spacing and trailing comment the operator
# wrote. Deliberately a LINE rewrite and not a re-render: their comments, their
# grouping and their ordering are theirs, and a file that rewrites itself
# wholesale is a file nobody trusts to hold a comment.
_ASSIGNMENT = re.compile(
    r'^(?P<lead>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)'
    r'(?P<mid>\s*=\s*)"(?P<value>[^"]*)"(?P<tail>.*)$')


def _section_of(line: str):
    """The category a `# heading` line names, or None if it is ordinary prose.

    ⚠ Deliberately narrow: only a comment that is EXACTLY one known category
    word is a heading. An operator's own note — `# the two account powers` —
    must not be mistaken for one, or a new command would land under it.
    """
    m = re.match(r"^#\s*([a-z_]+)\s*$", line)
    return m.group(1) if m and m.group(1) in CATEGORY_ORDER else None


# The block `append_lines` used to add, which this replaces. Matched so it can
# be REMOVED: a file that has been topped up by v1.37–v1.60 carries one of these
# per release, and they are what the new writer exists to stop producing.
_BANNER = re.compile(r"^#\s*Added(\s+by\s+Heresay\b[^\n]*)?\s*[—-]\s*new command")
_BANNER_TAIL = re.compile(r"^#\s*Change the value to move one")


def top_up(text: str, names, table: dict, categories: dict = None) -> str:
    """Insert new commands INTO their sections, and leave everything else alone.

    ⛔ **This replaced `append_lines`, which appended a marked block at the END
    of the file — and that was wrong in two ways that only showed up in
    production.** It accreted one banner per release; and when the operator
    later wrote the same key in its proper place, the file held that key TWICE,
    which is a TOML duplicate-key error, which refuses the parse, which aborts
    the deploy. A writer that can produce a file its own reader rejects is not a
    writer you can run unattended.

    What it does, and the list is short on purpose because it is rewriting
    somebody else's config:

    * **inserts** each new command at the end of the section its registry
      category names, creating that section if the file has none;
    * **aligns** the `=` column PER SECTION, to that section's longest key;
    * **removes** `# Added by Heresay …` banner blocks;
    * **de-duplicates** a repeated key, keeping the FIRST occurrence.

    ⛔ What it will NOT touch: **values**. It moves lines and it sets spacing;
    it has no opinion about what a line says. An operator's `gag = "High
    Warden"` comes out the other side saying exactly that, in the same section,
    in the same order relative to its neighbours. Their comments, their section
    order and any prose they wrote are theirs and are carried through.

    ⚠ Alignment is per SECTION rather than per file or per line, so adding a
    longer key RE-ALIGNS its whole section. That is the intended behaviour: a
    column only some lines respect reads worse than no column at all.
    """
    categories = categories or {}
    lines = text.splitlines()

    # ---- 1. read it, dropping the old banner blocks ---------------------
    # `body` is (section, kind, payload); an assignment carries its name so the
    # rebuild can align and de-duplicate without re-parsing.
    #
    # ⚠ `orphaned` is what makes removing a banner honest. The lines UNDER one
    # are the commands the old writer appended, which is to say the ones in the
    # wrong place; stripping the label and leaving them stranded at the end of
    # the file under no heading would tidy the symptom and keep the mess. They
    # are re-homed into their own section below, values untouched.
    section, orphaned = "", False
    body: list[tuple[str, str, object]] = []
    seen: set[str] = set()
    relocate: list[tuple[str, str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if _BANNER.match(line):
            if i < len(lines) and _BANNER_TAIL.match(lines[i]):
                i += 1
            while body and body[-1][1] == "raw" and not str(body[-1][2]).strip():
                body.pop()
            orphaned = True
            continue
        heading = _section_of(line)
        if heading is not None:
            section, orphaned = heading, False
            body.append((section, "heading", line))
            continue
        m = _ASSIGNMENT.match(line)
        if m:
            name = m.group("name")
            if name in seen:                   # ⚠ the duplicate that broke the
                continue                       #    parse: the first one wins
            seen.add(name)
            entry = (name, m.group("value"), m.group("tail").rstrip())
            if orphaned:
                relocate.append(entry)
            else:
                body.append((section, "assign", entry))
            continue
        if orphaned and not line.strip():
            continue                           # the blank the banner sat in
        orphaned = False
        body.append((section, "raw", line))

    # ---- 2. put the orphans, and the new commands, in their sections ----
    for entry in relocate:
        _place(body, entry[0], entry, categories)
    for name in names:
        _place(body, name, (name, table[name], ""), categories)
        seen.add(name)

    # ---- 3. one column per section --------------------------------------
    widths: dict[str, int] = {}
    for sec, kind, payload in body:
        if kind == "assign":
            widths[sec] = max(widths.get(sec, 0), len(payload[0]))

    out: list[str] = []
    for sec, kind, payload in body:
        if kind == "assign":
            name, value, tail = payload
            out.append(f'{name:<{widths[sec]}} = "{value}"{tail}')
        else:
            # ⚠ One blank line, never two. Removing a block leaves a gap
            # behind it, and a file that grows an extra blank per release is
            # the same accretion in a quieter form.
            if not str(payload).strip() and out and not out[-1].strip():
                continue
            out.append(str(payload))
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def _place(body: list, name: str, entry: tuple, categories: dict) -> None:
    """Put one assignment at the end of the section its category names.

    Creating that section if the file has none — at the END, because the only
    thing worse than a new section in an unexpected place is one inserted into
    the middle of somebody's careful ordering.
    """
    cat = categories.get(name, "")
    at = None
    for idx, (sec, kind, _) in enumerate(body):
        if sec == cat and kind == "assign":
            at = idx + 1                        # after the LAST of that section
    if at is None:
        while body and body[-1][1] == "raw" and not str(body[-1][2]).strip():
            body.pop()
        body.append((cat, "raw", ""))
        if cat:
            body.append((cat, "heading", f"# {cat}"))
        at = len(body)
    body.insert(at, (cat, "assign", entry))


def canonicalize(text: str, ladder, known=None) -> tuple:
    """Rewrite role-word VALUES as the rank names they mean. Returns
    ``(text, converted_names)``.

    The v1.43.0 migration, and it runs at WRITE time only (`--write-permissions`,
    as root, at deploy) — never in the daemon, which has no tolerance for a
    leftover anchor and simply refuses to boot on one. Doing it here means the
    conversion happens once, with the old code still running, by the person
    entitled to edit the file.

    ⚠ **A canonicalization, not a reset.** Only the value between the quotes
    moves, and only on a line naming a command the engine actually has: values
    already written as rank names, every comment, every blank line and the
    original ordering all survive byte for byte. Running it twice is a no-op,
    because after the first pass there is no anchor left to match.
    """
    if ladder is None:
        return (text, [])
    out, converted = [], []
    for line in text.splitlines():
        m = _ASSIGNMENT.match(line)
        if m and m["value"] in ANCHORS and (known is None or m["name"] in known):
            rank = ladder.resolve_anchor(m["value"])
            if rank:
                out.append(f'{m["lead"]}{m["name"]}{m["mid"]}"{rank}"{m["tail"]}')
                converted.append(m["name"])
                continue
        out.append(line)
    body = "\n".join(out)
    if text.endswith("\n"):
        body += "\n"
    return (body, converted)


# ---------------------------------------------------------------------------
# Disk
# ---------------------------------------------------------------------------

def load(path, seed: dict, legacy: dict = None, ladder=None) -> Permissions:
    """Read the file if it is there, and complete the table IN MEMORY.

    ⛔ **Reads only. Never creates, never appends, never touches the disk** — see
    the module note for the production incident that made this rule.

    Three cases, and none of them writes anything:

    * **the file exists** — parse it; anything it does not name takes its seed
      default in the returned table.
    * **no file, but a legacy table** (`state.json`'s `commands` block, from a
      house upgrading from v1.28–v1.36) — use that, completed from the seed, so
      an operator's assignments keep working before anyone has run
      `--write-permissions`.
    * **neither** — the seed alone. A house with no permissions file runs on the
      engine's own defaults, which is exactly what it did before the file
      existed.

    Raises `PermissionsError` on a file with no safe READING — that is fatal at
    boot (like a broken world) and a rejected reload at runtime. An unreadable
    file (permissions, a race) is NOT fatal: it is reported and the engine falls
    back, because a config file the daemon cannot open must degrade to "runs on
    the defaults", never to a crash-loop.
    """
    p = Path(path)
    text = None
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            # Unreadable, not unparseable. Say so and carry on; the alternative
            # is a house that will not start because of a file mode.
            log.warning("%s: could not read (%s) — using the built-in defaults",
                        p, exc)
    if text is not None:
        return parse(text, seed, source=str(p), ladder=ladder)

    # No file (or none we could read): complete from the legacy block if this is
    # an upgrading house, else from the seed alone. Both are in memory only, and
    # both are RESOLVED to rank names — the seed speaks in role anchors and a
    # pre-v1.37 `state.json` block was written in them, so resolving here is
    # what makes the running table look the same as a written file.
    #
    # ⚠ The legacy block is deliberately NOT held to the file's strictness. It
    # is not the operator's file; it is what an upgrading house is being carried
    # off, and a value that resolves to nothing still lands on arch through
    # `command_rank`'s own fail-safe.
    base = {k: v for k, v in dict(legacy or {}).items() if k in seed}
    table = {k: resolved(v, ladder) for k, v in {**seed, **base}.items()}
    return Permissions(table, added=sorted(k for k in seed if k not in base),
                       from_file=False)


def write_file(path, seed: dict, categories: dict = None, version: str = "",
               legacy: dict = None, ladder=None) -> tuple:
    """Create, canonicalize and top up `permissions.toml`. **The CLI's job.**

    Returns ``(action, names, converted)`` — the action is ``"created"`` (with
    every command), ``"appended"`` (the new ones), ``"canonicalized"`` (nothing
    added, but role words rewritten as names), or ``"unchanged"``. ``converted``
    is the commands whose value was rewritten.

    Run by `deploy.sh` as root before the service restarts, and by an operator
    by hand any time. **Idempotent**: a complete, names-only file is left byte
    for byte as it was.

    * **Absent** — written complete, from the seed MERGED WITH the legacy
      `state.json` block, so a house upgrading from v1.28–v1.36 keeps every
      assignment it had. Every value is written as a rank NAME.
    * **Present** — role-word values are canonicalized to rank names (v1.43.0,
      once), and only the missing commands are APPENDED, in a marked block.
      Existing name-values, comments and ordering are untouched.

    ⚠ **This is where a bad file is meant to be caught.** The parse below is the
    strict one, so a value that is not a rank raises here — at deploy, with the
    old code still serving — rather than at the restart, as a house that will
    not open. `deploy.sh` runs this BEFORE the restart and aborts on it.
    """
    p = Path(path)
    if p.exists():
        original = p.read_text(encoding="utf-8")
        text, converted = canonicalize(original, ladder, known=set(seed))
        # Strict: after the conversion, anything still unresolvable is a real
        # error and belongs to the operator, not to a guess.
        # ⛔ NORMALISE BEFORE PARSING, and this order is the whole repair.
        # A file the OLD writer has topped up twice can hold one key TWICE —
        # once in its section, once in an appended block — which is a TOML
        # duplicate-key error, so `parse` below would raise and the deploy
        # would abort with the file still broken. `top_up` is a LINE rewrite
        # and needs no parser, so it runs first and hands `parse` a file that
        # can be read. (Empty `names`: nothing is added on this pass — this is
        # the de-banner, de-duplicate and re-align half.)
        text = top_up(text, (), {}, categories or {})
        parsed = parse(text, seed, source=str(p), ladder=ladder)
        # ...and now the new commands, into their sections.
        if parsed.added:
            text = top_up(text, parsed.added, parsed.table, categories or {})
        if text == original:
            return ("unchanged", [], [])
        p.write_text(text, encoding="utf-8")
        return ("appended" if parsed.added else "canonicalized",
                parsed.added, converted)

    base = {k: v for k, v in dict(legacy or {}).items() if k in seed}
    # ⚠ Resolved on the way out, both halves. The seed speaks in role anchors,
    # and an upgrading house's legacy block was written in them too — the file
    # this produces names ranks, always.
    table = {k: resolved(v, ladder) for k, v in {**seed, **base}.items()}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(table, categories, version), encoding="utf-8")
    return ("created", sorted(table), [])
