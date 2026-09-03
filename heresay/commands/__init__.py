# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command parsing and dispatch.

Anything not starting with ``.`` (or the ``;`` emote shortcut) is speech to the
current room. Speech, emotes, thinks and tells share one rate-limit bucket.

Handlers are synchronous: they mutate in-memory world state and push text to
sessions. ``.quit`` returns the QUIT sentinel so the session loop can close.
"""

from __future__ import annotations
from ..permissions import DISABLED as PERM_DISABLED
from ..theme import DEFAULT_THEME

from ._shared import *   # noqa: F401,F403  (re-export shared surface)
from .registry import (ANCHORS, OWNER, BY_NAME, CANONICAL, CATEGORIES,
                       CATEGORY_LABELS, CLIENT, COMMAND, COMMANDS, RESIDENT,
                       SPEECH, STAFF, Command, command_seed,
                       privileged_commands)
from .speech import *   # noqa: F401,F403
from .movement import *   # noqa: F401,F403
from .social import *   # noqa: F401,F403
from .settings import *   # noqa: F401,F403
from .info import *   # noqa: F401,F403
from .staff import *   # noqa: F401,F403
from .system import *   # noqa: F401,F403



# Rank ordering comes from the LADDER now (theme.toml `[ranks] order`, v1.27.0)
# rather than from a dict of three literals here. `_ladder(session)` is the one
# accessor: a session's theme, or the built-in ladder for the handful of pure
# helpers that are called with a theme directly.
#
# An unknown rank reads as the FLOOR (`index_or_floor`) — the fail-safe
# direction, unchanged from the dict this replaces.


def _ladder(session):
    return getattr(session, "theme", None) or DEFAULT_THEME


# The single-character aliases that need no space before their argument. Derived
# from the registry, so registering one is all it takes.
_PREFIX_ALIASES = frozenset(v for v in CANONICAL if len(v) == 1 and not v.isalnum())


def _rank_of(session) -> str:
    """The caller's rank name, floor if they have none (a pre-login session)."""
    res = getattr(session, "resident", None)
    rank = getattr(res, "rank", "") if res is not None else ""
    return rank or _ladder(session).resident_rank


# ---------------------------------------------------------------------------
# The generated help screen (v1.29.0)
#
# `.help` used to be four hand-written tables — the resident grid, the short
# index, and two staff screens — kept in step with the permissions by tests and
# good intentions. It is GENERATED now, from the registry crossed with the live
# command table, so it cannot drift: reassign a command's rank and its entry
# moves to that rank's section, with no edit anywhere.
#
# The shape: a section per RANK, floor up to the caller's, each headed by the
# rank's name; CATEGORIES within. A command's section is the rank it REQUIRES,
# so the screen is a picture of the permission table.
# ---------------------------------------------------------------------------

# The short index's label column: wide enough for "administration", the longest
# category word, with the commands starting at one fixed column down the page.
HELP_LABEL_WIDTH = 16

# The header only appears when the caller can see more than one section — a
# resident's screen is one section and stays unheaded, exactly as it always was.
def _rank_header(rank: str) -> str:
    return f"[ {rank} ]"


def _alias_marks(cmd) -> list:
    """A command's shortcuts as they are shown: dotted, except the punctuation
    prefixes, which are typed bare."""
    return [("." + a if a.isalnum() else a) for a in cmd.aliases]


def _is_disabled(cmd, state) -> bool:
    """Is this entry switched off in this house? (v1.56.0)

    ⚠ Ungated entries — bare speech, the browser-side verbs — have no row in the
    permission table at all, so there is nothing to disable and nothing to ask.
    """
    if not cmd.gated:
        return False
    if state is not None and getattr(state, "commands", None):
        return state.command_disabled(cmd.name)
    return command_seed().get(cmd.name) == PERM_DISABLED


def _required_rank(cmd, theme, state) -> str:
    """The rank this entry belongs under. Ungated entries (bare speech, the
    browser verbs) have no table row, so they sit at the floor with the rest of
    what everyone can do."""
    if not cmd.gated:
        return theme.resident_rank
    if state is not None and getattr(state, "commands", None):
        return state.command_rank(cmd.name, theme)
    return theme.resolve_anchor(command_seed().get(cmd.name)) or theme.owner_rank


def _help_plan(theme, level: str, state=None) -> list:
    """[(rank, {category: [Command]})] for what this caller can reach, floor up.

    The one place the screen's content is decided — and it decides it by asking
    the same table the gate asks, which is the whole point.
    """
    here = theme.index_or_floor(level)
    buckets: dict = {}
    for cmd in COMMANDS:
        # ⛔ A disabled command is absent from the screen entirely — not greyed,
        # not footnoted. The house does not advertise what it does not have.
        if _is_disabled(cmd, state):
            continue
        rank = _required_rank(cmd, theme, state)
        if theme.index_or_floor(rank) > here:
            continue
        buckets.setdefault(rank, {}).setdefault(cmd.category, []).append(cmd)
    return [(r, buckets[r]) for r in theme.order if r in buckets]


def _fill(text: str, theme) -> str:
    """Descriptions name ROLES with placeholders, never baked words, so one
    source stays theme-aware."""
    return text.format(staff=theme.staff_word,
                       staff_plural=theme.staff_plural,
                       owner=theme.owner_word,
                       resident=theme.level_singular("resident"))


def _short_token(cmd) -> str:
    """Short uses the bare dotted name, not the full usage form: `.tell (.t >)`,
    not `.tell <name> <text> (.t >)`. An index is for someone who already knows
    the place — and the full forms do not fit at 40."""
    head = cmd.usage[0] if cmd.kind == SPEECH else f".{cmd.name}"
    marks = _alias_marks(cmd)
    return f"{head} ({' '.join(marks)})" if marks else head


def _help_body(theme, style: str, level: str, width: int, state=None) -> str:
    sections = _help_plan(theme, level, state)
    narrow = width <= NARROW_COLUMNS
    headed = len(sections) > 1

    # ONE label column across the WHOLE screen, so descriptions line up down the
    # page rather than per section — the wide grid has always read that way.
    every = [f for _r, cats in sections for cs in cats.values()
             for c in cs for f, _d in c.forms]
    label_w = max(len(f) for f in every) if every else 0
    tails = [len("  ".join(_alias_marks(c))) for _r, cats in sections
             for cs in cats.values() for c in cs if c.aliases]
    alias_col = (HELP_WIDTH - max(tails)) if tails else 0

    out = []
    for rank, cats in sections:
        if out:
            out.append("")
        if headed:
            out.append(_rank_header(rank))
        first = True
        for cat in CATEGORIES:
            if cat not in cats:
                continue
            # SHORT stays compact — its whole virtue is fitting on one screen,
            # so its categories run on. LONG breathes.
            if not first and style != "short":
                out.append("")
            first = False
            cmds = cats[cat]
            if style == "short":
                packed = pack_tokens([_short_token(c) for c in cmds],
                                     max(NARROW_MIN_RIGHT, width - HELP_LABEL_WIDTH))
                pad = " " * HELP_LABEL_WIDTH
                out.append(f"{CATEGORY_LABELS[cat]:<{HELP_LABEL_WIDTH}}{packed[0]}".rstrip())
                out.extend(f"{pad}{p}".rstrip() for p in packed[1:])
            elif narrow:
                # The alias tail folds into the description at 40: a third
                # column there is a margin, not a column.
                rows = []
                for c in cmds:
                    marks = _alias_marks(c)
                    for i, (form, desc) in enumerate(c.forms):
                        tail = f"  ({'  '.join(marks)})" if marks and i == 0 else ""
                        rows.append((form, _fill(desc, theme) + tail))
                out.extend(narrow_columns(rows, width, gutter=2,
                                          label_width=label_w))
            else:
                for c in cmds:
                    marks = _alias_marks(c)
                    for i, (form, desc) in enumerate(c.forms):
                        line = f"{form:<{label_w}}   {_fill(desc, theme)}"
                        if marks and i == 0:
                            line = f"{line:<{alias_col}}" + "  ".join(marks)
                        out.append(line.rstrip())
    return "\n".join(out)


# The covenant line the two staff screens used to close on. It is the one thing
# somebody newly given staff powers most needs to know they do NOT have, so it
# survives the screens:
# shown whenever the caller can see anything above the floor. Placeholders, like
# the descriptions, so a re-themed house says its own words.
STAFF_FOOTER = ("No one — {staff} or {owner} — can read anyone's mail but their "
                "own. {Staff} actions are logged as facts (who, what, to whom).")


def staff_footer(theme, width: int = HELP_WIDTH) -> str:
    text = STAFF_FOOTER.format(staff=theme.staff_word, owner=theme.owner_word,
                               Staff=cap_word(theme.staff_word))
    return "\n".join(wrap_at(text, width))


def help_text(theme, style: str = "long", level: str = "resident",
              width: int = HELP_WIDTH, state=None) -> str:
    """The one command list — generated, sectioned by rank then category.

    `level` decides how much of it there is: a resident sees the floor section,
    each higher rank adds its own, the arch sees all. Both styles keep their
    identity — long is every command with its explanation, short is one line per
    group — and both lay out at either width through the shared narrow helpers.
    """
    body = _help_body(theme, style, level, width, state)
    # The mail-power line goes to anyone who can see a section above the floor:
    # a resident has no staff powers to be told the limits of.
    if theme.index_or_floor(level) > 0:
        body += "\n\n" + staff_footer(theme, width)
    # The pointer is a quiet LINE inside the frame, as it has always been — the
    # bottom rule stays plain.
    return framed("Talker commands", body + "\n\n" + HELP_FOOTER, width=width)



NO_TOPIC = "No additional help available."

# Authored help pages that are LISTS — one entry per line, hand-aligned into
# columns. In narrow they get a blank line between entries (see cmd_help). Every
# other page is prose and reads fine wrapped.
SPACED_NARROW_TOPICS = frozenset({"set", "ignore"})


def help_topic_key(name: str) -> str:
    """Normalize a topic name to lowercase alphanumerics AND underscores.

    Both a typed topic and a help/ filename stem go through this, so they can't
    disagree. It also makes a typed topic incapable of naming a path: '../etc'
    becomes 'etc', and the lookup is a dict of pages loaded at startup, never a
    filesystem read.

    The underscore was added in v1.22.0. Stripping it meant `.help raise_prompt`
    normalised to `raiseprompt`, missed the `raise_prompt` page, and then — worse
    — reported the mangled key back: "Unknown command: .raiseprompt", a name the
    reader had not typed and could not act on. `_` is safe here for the same
    reason letters are: it cannot name a directory, a parent, or a separator.
    """
    return "".join(ch for ch in name.lower() if ch.isalnum() or ch == "_")


# Commands the BROWSER handles and the server never sees (see web/index.html).
# They are real commands to the person typing them, so they get help pages and a
# place in the list — hiding them behind "that's client-side" would only mean
# nobody ever finds out they exist. A socket door has no use for them, which the
# pages say plainly.
CLIENT_VERBS = frozenset({"theme", "raise_prompt"})


# The footer names the shape of the thing, not the contents: there is a page per
# command now, so listing them was listing the list above it twice.
HELP_FOOTER = "More: .help <command>"

# The short form: the same ten groups as one line each, labelled — because
# without the descriptions the grouping is all the meaning there is. It is an
# index for someone who already knows the place; `.set helpstyle long` brings
# the full grid back.
def cmd_help(session, args: str) -> None:
    """Bare: the command list. With a name: that command's page.

    Two different failures, said two different ways — because they are two
    different facts about the world:

      .help umnute   ->  Unknown command: .umnute   (try .help)
      .help unmute   ->  the page, or "No additional help available."

    The first says the command does not exist; the second says it does and
    nobody has written its page yet. The old code said the second thing for
    both, which quietly taught that a typo was a documentation gap.

    A verb above the caller's rank is, to them, a verb that does not exist —
    `.help gag` answers a resident exactly as `.gag` does, and for the same
    reason: the lookup may never be the thing that reveals a hidden verb.
    """
    topics = getattr(session, "help_topics", None) or {}
    wanted = help_topic_key(args)
    if not wanted:
        session.send_raw(help_text(session.theme,
                                   getattr(session, "help_style", "long"),
                                   _rank_of(session),
                                   width=session.width,
                                   state=getattr(session, "state", None)))
        return
    level = _rank_of(session)
    known = wanted in _COMMANDS or wanted in CLIENT_VERBS
    if not known or (wanted in _COMMANDS and not _visible(
            wanted, level, _ladder(session), getattr(session, "state", None))):
        # Echo what they actually TYPED, not the normalised key: being told
        # ".raiseprompt" is unknown when you typed ".raise_prompt" sends you
        # looking for a typo you did not make.
        #
        # Safe to echo, and bounded on purpose: it goes ONLY back to the person
        # who typed it (never broadcast), every output line is control-stripped
        # at the session sink so nothing can be smuggled through it, and the cap
        # means a pasted kilobyte is not mirrored back wholesale. The lookup
        # itself is unchanged — a dict built at startup, never a filesystem read
        # — so a path-shaped topic still cannot reach a path.
        session.send(unknown_command(echo_topic(args) or wanted))
        return
    page = topics.get(wanted)
    if not page:
        session.send(NO_TOPIC)
        return
    # A page is one unit like any other output (so it gets the blank-line
    # separation), pre-wrapped so its authored indentation survives, and
    # framed like every other block the server shows you.
    #
    # The two LIST pages get spaced out in narrow. `set` and `ignore` are
    # hand-aligned tables of one entry per line; at 40 columns every entry wraps
    # to two or three lines and, run together, there is no way to see where one
    # setting ends and the next begins. Spacing happens BEFORE wrapping, so the
    # blanks land between entries rather than between fragments of one.
    width = session.width
    if width <= NARROW_COLUMNS and wanted in SPACED_NARROW_TOPICS:
        page = space_out(page)
    session.send_raw(framed(f".help {wanted}", wrap_page(page, width), width=width))


# The dispatch table is DERIVED from the registry (v1.28.0), not hand-written.
# One declaration per command means an alias cannot exist without the gate
# knowing which command it belongs to — the alias hole, closed by construction.
# `cmd_<name>` is resolved from this module's namespace because the help handler
# lives here; a test asserts every registry entry binds to a callable.
_COMMANDS = {verb: globals()[f"cmd_{BY_NAME[canon].name}"]
             for verb, canon in CANONICAL.items()}


def _visible(verb: str, level: str, theme=None, state=None) -> bool:
    """Is this verb even visible to a holder of `level`?

    Data-driven since v1.28.0: the required rank comes from the house's command
    table (`state.json`, seeded from the registry) rather than from two hardcoded
    sets, and the answer is one threshold over the ladder.

    Keyed on the CANONICAL command, so an alias always carries its command's
    rank — `>` cannot outrank `.tell`. Below the required rank the verb is
    indistinguishable from a typo: residents get the plain "Unknown command",
    never a hint that a privileged command exists.

    Both fail-safe directions point at LESS access, and they point opposite ways
    on purpose: an unknown *rank* reads as the floor (`index_or_floor`), while an
    unknown *requirement* reads as the arch (`command_rank`).
    """
    theme = theme or DEFAULT_THEME
    canon = CANONICAL.get(verb, verb)
    # ⛔ DISABLED IS NOT A RANK, AND NOT A REFUSAL (v1.56.0). A command the house
    # has switched off does not exist here: no permission check runs, and the
    # answer is False for EVERYONE — the owner included, which is the whole
    # difference between a disable and a rank. Keyed on the CANONICAL name, so
    # an alias dies with its command exactly as it inherits its rank.
    #
    # ⚡ This is deliberately the ONLY place it is asked. `_visible` is what
    # dispatch consults before the unknown-command line, what `.help <topic>`
    # gates on, and what `may_run` is; saying False here therefore produces the
    # ordinary typo reply BY CONSTRUCTION rather than by a second code path
    # written to imitate it. There is no wording to keep in step.
    if state is not None and getattr(state, "commands", None):
        if state.command_disabled(canon):
            return False
    elif command_seed().get(canon) == PERM_DISABLED:
        return False
    if state is not None and getattr(state, "commands", None):
        required = state.command_rank(canon, theme)
    else:
        # No store, or a store that was never given a table — a pure-function
        # test, a session before state is wired, or a StateStore built without a
        # seed. Fall back to the registry's own seed, which is what the file
        # would have been seeded with anyway.
        #
        # An EMPTY table is "not configured", never "deny everything": a store
        # that was never told the defaults must not be able to lock the house
        # down to arch-only. A table with entries IS authoritative, including for
        # the commands it omits (those are arch-only — see `command_rank`).
        required = theme.resolve_anchor(command_seed().get(canon)) or theme.owner_rank
    return theme.index_or_floor(level) >= theme.index_or_floor(required)


def may_run(verb: str, session) -> bool:
    """Public: may THIS session run ``verb``? Exactly the dispatcher's gate.

    Added for the report box (v1.50.0), which asks "who may READ reports?" in
    two places outside dispatch — the login notice, and the live ping to online
    readers. ⛔ Both must answer it the SAME way `.read_report` itself is gated,
    or the house would tell somebody about reports they cannot open, or stay
    silent to somebody who can. So this is `_visible` under a public name rather
    than a second rank check, and there is deliberately no way to ask the
    question that does not go through the live command table.
    """
    return _visible(verb, _rank_of(session), _ladder(session),
                    getattr(session, "state", None))


# How much of a typed topic the "unknown" line will quote back. Long enough for
# any real command name, short enough that a paste is not echoed at length.
TOPIC_ECHO_MAX = 32


def echo_topic(raw: str) -> str:
    """The typed topic, trimmed and length-capped, for quoting back at them."""
    return " ".join((raw or "").split())[:TOPIC_ECHO_MAX]


def unknown_command(verb: str) -> str:
    """The one wording for "there is no such command" — typed directly, or asked
    about through `.help`. One function so the two can never drift apart."""
    return f"Unknown command: .{verb}   (try .help)"


# What a caller is told when a handler fails. Deliberately says nothing about
# what went wrong: the person typing has no use for the detail, and the detail
# is the one thing that might carry their own words back at them.
HANDLER_ERROR = "Something went wrong with that command."


def _safe_call(session, handler, arg):
    """Run one handler and never let it take the connection down.

    ⛔ **THE PROMISE MADE STRUCTURAL** (v1.52.3, audit C3). `dispatch` has
    documented "never raises on user input" since it was written, and until now
    that was true only for as long as every handler happened to be careful.
    `.dmail ²` was not careful: `str.isdigit()` accepts a superscript two,
    `int()` refuses it, and the ValueError unwound out of the handler, out of
    `dispatch`, into `_serve`'s blanket handler — which logged it and closed the
    caller's connection. The three guards are fixed at source; this is what
    stops the NEXT one being a dropped session.

    ⛔ **THE FACT, NEVER THE WORDS.** The exception's own message carries the
    user's input (`invalid literal for int() with base 10: '²'`), so `str(exc)`,
    `exc.args` and the traceback are all off limits — the same rule every other
    error path in the engine lives under. What is logged is the HANDLER's own
    name and the exception TYPE, both engine-defined strings: **no user
    variable is passed to the logger.** (`_serve`'s blanket handler is unchanged
    and still logs frames; this one runs first and stops most things reaching
    it.)

    ⚠ It does NOT swallow the return value: QUIT, PASSWD, DELETEME and the rest
    are how the session loop is driven, so the handler's result passes straight
    through on the ordinary path.

    ⚠ A `session.send` that itself raises is deliberately not caught. If the
    transport is gone there is nothing to say and nowhere to say it, and ending
    the session is then the right answer rather than a bug.
    """
    try:
        return handler(session, arg)
    except Exception as exc:
        log.error("command handler failed: %s raised %s",
                  getattr(handler, "__name__", "?"), type(exc).__name__)
        session.send(HANDLER_ERROR)
        return None


def dispatch(session, line: str):
    """Route one input line. Returns QUIT to end the session, else None.

    Never raises on user input; unknown verbs get a friendly nudge. Since
    v1.52.3 that is enforced rather than intended — see `_safe_call`.
    """
    if not line:
        return None

    # The prefix shortcuts — ';' is .emote, '>' is .tell, '<' is .remote — are
    # registered ALIASES of their commands (v1.28.0), so they are rewritten to
    # the dotted form here and go through the SAME gate as everything else.
    # Before that they were handled ahead of the gate, which was harmless while
    # permissions were hardcoded and all three sat at the floor, but would have
    # let `>` outrank `.tell` the moment the table became editable.
    #
    # The payload is lstripped so ';waves' and '; waves' are the same line; a
    # bare prefix falls through to that handler's usage error.
    if line[:1] in _PREFIX_ALIASES:
        line = f".{line[0]} {line[1:].lstrip()}"

    # Bare speech is the one ungated route, deliberately: `.say` has no dotted
    # form and gating it would not be a permission, it would be a mute.
    #
    # ⚠ Through `_safe_call` like everything else: this is the one path that
    # does not go via the command table, so leaving it out would put a hole in
    # the never-raises guarantee exactly the width of the thing people do most.
    if not line.startswith("."):
        return _safe_call(session, cmd_say, line)

    body = line[1:]
    if not body.strip():
        session.send("Say something, or type .help")
        return None

    parts = body.split(None, 1)
    verb = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    level = _rank_of(session)
    handler = _COMMANDS.get(verb)
    if handler is None or not _visible(verb, level, _ladder(session),
                                       getattr(session, "state", None)):
        # Privileged verbs are invisible below their level: same reply as a typo.
        session.send(unknown_command(verb))
        return None
    # Command governor: one token per dotted command from a generous per-session
    # bucket, so no client can spam handler/disk work (.set/.desc/.profile/.mute/
    # .who/…). Quit is always allowed (never trap someone trying to leave);
    # speech verbs also pass their own SpeechGovernor upstream.
    if verb not in ("quit", "q", "x"):
        bucket = getattr(session, "command_bucket", None)
        if bucket is not None and not bucket.take(session.now()):
            session.send("You're using commands too fast — slow down a moment.")
            return None
    return _safe_call(session, handler, args)
