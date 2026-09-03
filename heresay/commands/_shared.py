# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared command helpers, constants, and the framing/defaults machinery.

Imported by the theme modules and re-exported from the package __init__.
"""
from __future__ import annotations
import logging
import textwrap
import time
from pathlib import Path
from ..session import NARROW_COLUMNS
# house_switch and HELP_STYLES live in state.py (beside HOUSE_DEFAULTS) so that
# `session` can use them without importing the command package — see the
# display-switch properties on Session. Re-exported here, where they have
# always been imported from.
from ..state import HELP_STYLES, HOUSE_DEFAULTS, SWITCH_KEYS, house_switch  # noqa: F401
from ..world import Resident


log = logging.getLogger("heresay")
MAIL_FLOOD_MSG = "You're sending mail too fast — give it a moment."
# Shown to a jailed resident who tries to reach OUTSIDE the jail room
# (.shout/.tell/.remote/.invite). They can still .say/.emote in the room.
JAILED_OUTBOUND = "You're jailed — you can only speak in this room."


def house_setting(session, key: str) -> str:
    """A house-wide `.ownerset` value via the state store, falling back to the
    built-in default when no store is attached (unit-test sessions)."""
    state = getattr(session, "state", None)
    return state.house_setting(key) if state is not None else HOUSE_DEFAULTS[key]


def cap_word(word: str) -> str:
    """Capitalise a DISPLAY word for the head of a line, without flattening it.

    ⚠ `str.capitalize()` lowercases everything after the first character, so a
    two-word level name ("duty officer" -> "Duty officer") and a house whose word
    carries its own capitals ("McWarden" -> "Mcwarden") come back mangled. The
    first character is the only one a sentence position entitles us to touch; the
    rest is the operator's spelling.
    """
    return word[:1].upper() + word[1:]


# ---------------------------------------------------------------------------
# Framing informational blocks
# ---------------------------------------------------------------------------

FRAME_WIDTH = 78


def frame_rule(text: str = "", width: int = FRAME_WIDTH) -> str:
    """One full-width rule of asterisks, with text set INTO the rule where given.

    ``.who`` has headed itself this way since the beginning; every screen-sized
    informational block now does. The point is the distinction it draws: a block
    is a thing the server is showing you, it starts and ends on a rule, and
    **conversation is never framed** — so a say can never be mistaken for a
    listing, and a listing can never be mistaken for someone speaking.

    This is the ONE ``***`` filler in the program, and ``framed()`` is its only
    caller — so a session's width reaches every rule on every screen through
    these two functions and nowhere else.
    """
    if not text:
        return "*" * width
    lead = f"*** {text} "
    if len(lead) > width - 3:                # always leave a visible run of stars
        cut = lead[: max(0, width - 4)]
        # Back off to a word boundary where there is one: a title chopped
        # mid-word ("yes|") reads as corruption rather than as abbreviation,
        # and at 40 columns titles actually reach this.
        space = cut.rfind(" ")
        if space > 4:                        # keep at least "*** " plus a word
            cut = cut[:space]
        lead = cut.rstrip() + " "
    return lead + "*" * max(0, width - len(lead))


def framed(title: str, body: str, footer: str = "", width: int = FRAME_WIDTH) -> str:
    """A block between two rules. ``footer`` is embedded in the bottom rule where
    the block has a natural closing line (``.who``'s total, an account count);
    otherwise the bottom is a plain rule."""
    return "\n".join([frame_rule(title, width), "", body.strip("\n"), "",
                      frame_rule(footer, width)])


# ---------------------------------------------------------------------------
# Event-log views: the window one screen looks back over
#
# Retention and a view's window are different questions — one is a policy about
# the file, the other is what somebody is asking today — so each view carries its
# own `[world] *_window_hours`. But a screen cannot show what the store no longer
# holds, so the setting is CAPPED at the retention window, and that cap is what
# makes the knob impossible to misconfigure: too large means "everything
# retained", and so does zero or a negative. There is no value that yields an
# empty screen, and none that promises more history than exists.
# ---------------------------------------------------------------------------

def view_window_hours(config, key: str, default: float = 24.0):
    """The effective window for an event view, in hours.

    ``None`` means no time filter at all — the store is keeping everything
    (``event_retention_days <= 0``) and the setting asked for more than a day-
    less-than-nothing. Callers render that as "everything kept" rather than
    inventing a number.
    """
    try:
        hours = float(getattr(config, key, default))
    except (TypeError, ValueError):
        hours = default
    try:
        days = float(getattr(config, "event_retention_days", 0.0))
    except (TypeError, ValueError):
        days = 0.0
    cap = days * 24.0 if days > 0 else None
    if hours <= 0:                 # "as far back as you have" — see above
        return cap
    return min(hours, cap) if cap is not None else hours


def window_phrase(hours) -> str:
    """"last 24 hours" / "last 1 hour" / "everything kept" — for a view's title.

    ``:g`` so a whole number reads as one: "last 12 hours", never "last 12.0
    hours", while a genuine 1.5 survives.
    """
    if hours is None:
        return "everything kept"
    return f"last {hours:g} hour{'' if hours == 1 else 's'}"


def window_cutoff(session, key: str, default: float = 24.0):
    """``(cutoff_epoch_or_None, hours_or_None)`` for one view's window.

    ⚠ The WALL clock, not `session.now()`. Events are stamped with wall time
    (the store's own `time.time`), and the session's ordinary clock is monotonic
    — comparing the two would give a cutoff in some other epoch entirely and a
    screen that shows everything or nothing. It is still injected, so the view
    tests stay deterministic exactly as the retention ones are.
    """
    hours = view_window_hours(getattr(session, "config", None), key, default)
    if hours is None:
        return None, None
    return session.now_wall() - hours * 3600.0, hours


# ---------------------------------------------------------------------------
# Narrow rendering (40 columns — a portrait phone)
#
# Every screen-sized block in the program is a hand-set table: a key column, a
# value column, sometimes a third. At 40 columns there is no room for two
# columns side by side, so narrow uses ONE shape everywhere instead of inventing
# a different squeeze per screen:
#
#     label
#       the value, wrapped and indented under it
#
# or, where the label is short and fixed, a genuine two-column form with the
# right-hand block wrapped and its continuations aligned. Both are below, and
# every narrow screen is built from them — so the whole program looks like one
# program on a phone, and a new screen has an obvious thing to reach for.
# ---------------------------------------------------------------------------

# The indent under a stacked label. Two spaces: enough to read as subordinate,
# cheap enough that a 40-column line still holds a sentence.
NARROW_INDENT = "  "

# The narrowest right-hand column worth having. Below this, a two-column row
# shreds its value into fragments and the stacked form wins outright.
NARROW_MIN_RIGHT = 14

# The right column a two-column table AIMS for. The label column is capped so
# this survives: without the cap, one 21-character command (`.remote <name>
# <text>`) sets the column for all thirty rows and squeezes every description —
# including the twenty short ones — into a 17-column ribbon. Capping the column
# and letting the few over-long labels stack on their own line is the ordinary
# typographic answer, and it is why `narrow_columns` handles an over-long label
# per row rather than widening the table for it.
NARROW_GOOD_RIGHT = 22


def pack_tokens(tokens, width: int, sep: str = ", ") -> list[str]:
    """Greedily pack ``tokens`` into lines of at most ``width``, never splitting one.

    `textwrap` cannot do this: the tokens here are things like
    ``.tell <text> (.t >)`` — they contain spaces, so a wrapper that breaks on
    whitespace breaks them in half, and a command severed from its shortcut is
    worse than no shortcut at all. A token longer than the whole width gets its
    own line rather than being broken.

    ⚠ **A continued line ends with the separator, so the separator has to fit
    too** (fixed v1.50.0). Every line but the last gets a trailing ``,`` when it
    is flushed — and the fit test used to measure the content WITHOUT it, so a
    line landing exactly on ``width`` came out one column over and punched
    through the frame. Latent for a long time: it needs a group whose tokens
    happen to fill the width exactly AND another token after them. Adding
    `.report` to the `house` category produced exactly that
    (``.call_staff, .mail_staff`` is 24 in a 24-column field) and the 40-column
    help screen overflowed.

    So the budget reserves the mark for every token but the final one — the last
    line is never flushed and never carries a mark, so it may use the full
    width.
    """
    toks = [str(t) for t in tokens if str(t)]
    mark = sep.rstrip()
    lines, cur = [], ""
    for i, tok in enumerate(toks):
        budget = width if i == len(toks) - 1 else width - len(mark)
        candidate = f"{cur}{sep}{tok}" if cur else tok
        if cur and len(candidate) > budget:
            lines.append(cur + mark)
            cur = tok
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return lines or [""]


def wrap_at(text: str, width: int, indent: str = "") -> list[str]:
    """``text`` wrapped to ``width``, every line after the first indented.

    The workhorse of the narrow renderers. An empty string yields one empty
    line rather than nothing, so a value that is absent still occupies its row
    and the block does not silently lose a field.
    """
    if not text.strip():
        return [""]
    return textwrap.wrap(text, width=max(8, width), break_long_words=False,
                         break_on_hyphens=False, initial_indent=indent,
                         subsequent_indent=indent) or [""]


def wrap_hard(text: str, width: int, indent: str = "") -> list[str]:
    """`wrap_at`, but a token longer than the line is BROKEN rather than left to
    overhang.

    ⚠ The opposite trade from `wrap_at`, and it is right in exactly one place:
    `.load_report`, whose content is machine-generated diagnostics carrying
    filesystem paths. A path is one unbreakable token of arbitrary length, so at
    40 columns `wrap_at` would punch it straight through the frame — and a rule
    the text runs past reads as a broken screen, not as a long word. Everywhere
    else the tokens are authored prose, room ids or command names, where keeping
    a word whole is worth more than keeping the margin.
    """
    if not text.strip():
        return [""]
    return textwrap.wrap(text, width=max(8, width), break_long_words=True,
                         break_on_hyphens=False, initial_indent=indent,
                         subsequent_indent=indent) or [""]


def narrow_columns(rows, width: int, gutter: int = 2,
                   label_width: int = None, wrap=None) -> list[str]:
    """Two columns: a fixed-width left label, a wrapped right-hand block.

    **The one narrow column-block helper.** Every narrow two-column screen goes
    through it — the long help grid, the short index, both staff grids, bare
    `.set` — so the column geometry is decided once and none of them can drift.
    ``rows`` is ``(label, text)`` pairs. The left column is as wide as the
    longest label (or ``label_width`` if given); continuation lines indent to sit
    exactly under the right column, so the block reads as one paragraph hanging
    off its label:

        .say    Speak aloud to everyone
                in the room.

    ``wrap`` is the CALLER'S flavour — ``wrap(text, right) -> list[str]``,
    unindented, the helper does the padding. It defaults to `wrap_at` (ordinary
    prose). The short index passes a `pack_tokens` wrap instead, because its
    items contain spaces (``.tell <text> (.t >)``) and a command severed from its
    shortcut is worse than no shortcut. The helper owns the GEOMETRY; the caller
    owns how its text breaks.

    ⚠ **An over-long label keeps the column.** A label wider than ``lw`` takes a
    line to itself and its description then sits UNDER THE DESCRIPTION COLUMN,
    wrapped to that column's width — never a two-space hang, never the full
    screen width:

        .remote <name> <text>
                          private emote,
                          anywhere

    Falling back to a two-space hang was the bug: it broke the one vertical line
    the reader is following down the screen, and at 40 the rows that trip it
    (`.tell <name> <text>`, `.gag / .ungag <name>`) are precisely the ones the
    eye most needs to keep placed. Widening the table for them instead is the
    other wrong answer — one 21-character label would squeeze all thirty
    descriptions into a ribbon, which is why the label column is capped
    (``NARROW_GOOD_RIGHT``) and the few over-long labels stack their label only.

    Falls back to the STACKED form when the labels leave too little room on the
    right to be worth wrapping into: a right column under ``NARROW_MIN_RIGHT``
    turns every value into four ragged fragments, which is strictly worse than
    putting the value on its own line. With the cap in place this only bites well
    below 40 columns — at 40 the right column is always at least
    ``NARROW_GOOD_RIGHT``.
    """
    rows = list(rows)
    if not rows:
        return []
    wrap = wrap or wrap_at
    lw = label_width if label_width is not None else max(len(str(a)) for a, _ in rows)
    # Cap it, always — including a caller-supplied width, since the caller knows
    # its longest label but not how much room the screen has left.
    lw = min(lw, max(8, width - gutter - NARROW_GOOD_RIGHT))
    right = width - lw - gutter
    pad = " " * (lw + gutter)
    lines = []
    for label, text in rows:
        label = str(label)
        text = str(text)
        if right < NARROW_MIN_RIGHT:
            # No usable right column at all: stack the whole table instead of
            # squeezing. Prose wrap, since there is no column to keep.
            lines.append(label)
            if text.strip():
                lines.extend(wrap_at(text, width - len(NARROW_INDENT),
                                     NARROW_INDENT))
            continue
        wrapped = wrap(text, right)
        if len(label) > lw:
            # The label takes its own line; the description still belongs to the
            # column. See the warning above — this is the rule, not a fallback.
            lines.append(label)
            if text.strip():
                lines.extend(f"{pad}{ln}".rstrip() for ln in wrapped)
            continue
        lines.append(f"{label:<{lw}}{' ' * gutter}{wrapped[0]}".rstrip())
        lines.extend(f"{pad}{ln}".rstrip() for ln in wrapped[1:])
    return lines


def narrow_stacked(rows, width: int, blank_between: bool = True) -> list[str]:
    """A label on its own line, its value indented beneath, one blank between.

    For rows whose value is a phrase or a sentence rather than a word — where a
    two-column form would leave a 12-column ribbon on the right and wrap every
    value to four ragged lines. The blank line between entries is what makes a
    long settings list readable on a phone: without it the wrapped values run
    into each other and there is no way to see where one setting ends.
    """
    out = []
    for label, text in rows:
        if out and blank_between:
            out.append("")
        out.append(str(label))
        if str(text).strip():
            out.extend(wrap_at(str(text), width - len(NARROW_INDENT),
                               NARROW_INDENT))
    return out


def space_out(text: str) -> str:
    """Put a blank line between the entries of an authored, line-per-entry page.

    Used for the `.help set` / `.help ignore` pages in narrow. Those pages are
    hand-aligned tables: at 40 columns every row wraps to two or three lines and
    they run together into a wall. Separating them costs vertical space, which a
    phone scrolls and a terminal does not.

    Consecutive non-blank lines get a blank between them; existing blank lines
    and the surrounding structure are left exactly as authored, so a page that
    already spaces itself is unchanged.
    """
    lines = text.split("\n")
    out = []
    for line in lines:
        if out and line.strip() and out[-1].strip():
            out.append("")
        out.append(line)
    return "\n".join(out)


# The roll's indents: three spaces in, and five for a description that wraps —
# a hanging indent, so a long description at 40 columns reads as one entry
# rather than spilling back to the margin and looking like a new name.
PRESENT_INDENT = "   "
PRESENT_HANGING = "     "


def _present_line(session, name: str, description: str) -> list[str]:
    """One entry in the roll: ``   Name their description``.

    No description means just the indented name — nothing stands in for it.

    Pre-wrapped to the session's width so the continuation keeps its hanging
    indent: ``Session.send`` wraps too, but flat, and would drop a wrapped
    entry's second line back to column zero. Wrapping here first makes send's
    pass a no-op (every line already fits), so the two do not fight.
    """
    text = f"{name} {description}".rstrip()
    width = getattr(session, "width", None) or 78
    return textwrap.wrap(text, width=width, initial_indent=PRESENT_INDENT,
                         subsequent_indent=PRESENT_HANGING,
                         break_long_words=False, break_on_hyphens=False) or \
        [PRESENT_INDENT + name]


def _describe_room(session, res: Resident) -> str:
    room = session.world.room(res.room_id)
    if room is None:
        return "You are nowhere."
    # The title is the room file's `name:` header; the description is the authored
    # prose, set at this session's width (verbatim at 78 — see room_description).
    lines = [f"[ {room.name} ]", "", room_description(session, room)]
    # Present company — the room's roll, INCLUDING the looker. Reading a list
    # you are on is how a room tells you who is in it; the old form
    # ("Corvid (a bird) is here.", everyone but you) read as a series of
    # announcements about other people and left you to remember you were there.
    #
    # Name, one space, description. No parentheses, no "is here", no trailing
    # stop, and NO RANK MARKING — staff look exactly like anyone else here.
    # Who holds what is an `.examine` detail, not a thing the room advertises.
    people = list(session.world.occupants(room.id))
    if res not in people:
        people.append(res)          # defensive: you are always in your own room
    if people:
        lines.append("")
        lines.append("Present company:")
        for o in sorted(people, key=lambda r: r.name.lower()):
            shown = effective_description(session, o.description)
            lines.extend(_present_line(session, o.name, shown))
    exit_ids = session.world.exits_of(room.id)
    exits = sorted(session.world.room(e).name for e in exit_ids if session.world.room(e))
    if exits:
        lines.append("")
        lines.append("Exits: " + ", ".join(exits))
    # The topic sits BELOW the exits: the room itself first — what it is, who is
    # in it, where it leads — and then the note this particular gathering pinned
    # to the door. No topic means NO line at all, never a "No topic."
    # placeholder; the absence is the message.
    topic = session.world.topic_of(room.id)
    if topic:
        lines.append("")
        lines.append(f"Topic: {topic}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The resident's own comings and goings (.set enter_msg / exit_msg / afk_msg /
# login_msg / logout_msg)
#
# Each is one line of the resident's own words, stored on the account (in
# `settings`, alongside the .set toggles) and rendered to whichever room should
# hear it. An ABSENT key means "use the house default" — we never store the
# default itself, so an owner rewording the house moves everyone who hasn't
# chosen their own, live.
# ---------------------------------------------------------------------------

MSG_MAX = 60

# How long a DESCRIPTION may be — the one-liner `.examine` and the room's roll
# show beside a name. Its own number because it is its own thing: a description
# is a phrase about a person, not a line the engine wraps in brackets, and it
# has always allowed twice what a presence phrase does.
#
# ⚠ Named here rather than left as a literal in `cmd_desc` because the HOUSE
# default (`.ownerset desc`) has to be capped at exactly the same number
# (v1.52.4): two spellings of one bound is how they come to disagree.
DESC_MAX = 120

# The built-in AFK wording. It lives in HOUSE_DEFAULTS with the rest of the
# message defaults; the name survives because "what a bare .afk says with nothing
# configured" is a thing worth being able to say in one word.
AFK_DEFAULT = HOUSE_DEFAULTS["afk_msg"]


# ---------------------------------------------------------------------------
# The house display switches — dim / time / helpstyle
#
# `defaults.txt` is GONE (v1.17.0). These three were the last thing in it, and
# having a whole operator file, a loader, a bespoke error class and a config key
# for three words was more machinery than the idea deserved — especially with
# `.ownerset` already being the house's settings surface. They are ordinary
# house settings now.
#
# The one honest difference from the message keys: a session reads these ONCE,
# when it starts, so an owner's change reaches a resident at their NEXT LOGIN
# rather than immediately. Documented rather than hidden; making them truly live
# is a follow-up.
# ---------------------------------------------------------------------------

def clean_message(text: str, limit: int = MSG_MAX) -> str:
    """One line, no control characters, at most ``limit`` visible characters.

    Every run of whitespace — newlines included — collapses to a single space,
    so a pasted paragraph can only ever become one line. Shared by the .*msg
    family and .topic: same rules, different ceilings.
    """
    stripped = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    return " ".join(stripped.split())[:limit]


def resident_message(session, res, key: str) -> str:
    """The resident's own wording for ``key``, or the house default.

    Since 1.16.0 the house default is the LIVE `.ownerset` value rather than
    a startup-loaded file, so an owner rewording the house moves every
    default-following resident at once — no restart, no re-login.
    """
    own = (getattr(res, "messages", None) or {}).get(key)
    return own if own else house_setting(session, key)


def effective_description(session, description: str) -> str:
    """A resident's description, or the house default when they have none.

    Absent-means-default, the same pattern as the message wording: nothing is
    stored on an account that has not set one, so an owner's `.ownerset
    desc` moves everyone who has not written their own. Ships EMPTY, so out of
    the box a resident with no description still has no description.
    """
    if description and description.strip():
        return description
    return house_setting(session, "desc")


def clears_afk(line: str) -> bool:
    """Does this input line clear AFK? Any input does, EXCEPT (re)setting AFK
    and blank lines. (Called from the session loop.)"""
    s = line.strip()
    if not s:
        return False
    low = s.lower()
    if low == ".afk" or low.startswith(".afk "):
        return False
    return True


# ---------------------------------------------------------------------------
# Help screens, laid out on a fixed THREE-column grid: primary form, description,
# shortcut tail. Each row is (primary form, description, [alias tokens]). All
# three columns are dead straight — in particular the shortcuts (including the
# bare prefixes >, <, ;) start at ONE column shared by every row that has them,
# so they read down the page instead of trailing off wherever each description
# ended. Widths are computed from the tables, so a new verb can't re-ragged them;
# the price is that a description must stay short enough to clear the alias
# column (keep them under ~45 characters, and the tests will tell you).

HELP_WIDTH = 78

def wrap_page(text: str, width: int = HELP_WIDTH) -> str:
    """Wrap an authored help page at ``width``, keeping its own layout.

    A page is hand-laid-out, so each line's leading indent is carried onto any
    continuation and blank lines are kept — unlike Session.send's wrapping,
    which would flatten the indents an operator lined up on purpose.
    """
    out = []
    for line in text.split("\n"):
        if not line.strip():
            out.append("")
            continue
        indent = line[: len(line) - len(line.lstrip())]
        out.extend(textwrap.wrap(line, width=width, break_long_words=False,
                                 break_on_hyphens=False,
                                 subsequent_indent=indent) or [""])
    return "\n".join(out)


def reflow(text: str, width: int) -> str:
    """Re-set authored PROSE at ``width``, joining the author's own line breaks.

    The opposite judgement from `wrap_page`, and deliberately so. There, every
    authored break is a layout decision worth keeping. Here they are only where
    the author's screen ended: room files are hand-wrapped near 70, so at 40 each
    of those lines wraps again a few words from its end and the paragraph comes
    out as long/short/long/short. Collapsing them and setting the paragraph once
    at this width is what makes it read as prose again.

    A blank line is the one break that survives — that is a paragraph, not a
    screen edge. A RUN of them is one break: the gap is the signal, not its size.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.split("\n"):
        if line.strip():
            current.append(line.strip())
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(
        "\n".join(textwrap.wrap(p, width=width, break_long_words=False,
                                break_on_hyphens=False) or [""])
        for p in paragraphs)


def render_agreement(text: str, session) -> str:
    """The agreement, laid out for this session's width. THE only renderer.

    Two callers and they must never differ: the server shows this at account
    creation and at re-consent, and `.agreement` shows it to anyone who wants to
    re-read the terms. Showing somebody a differently-set copy of the thing they
    signed would be a small lie, so there is one function rather than one each.

    ⛔ Display-time only, and this is the line that guarantees it: the version is
    the sha256 of the RESOLVED but UNWRAPPED text, taken once at startup, so
    nothing done here can move it. A phone and a terminal read the same version
    of the same words.
    """
    return reflow(text, session.width)


def room_description(session, room) -> str:
    """The room's authored description, at this session's width.

    Three cases, and the first is the one to protect:

    * **wide** — the file exactly as authored, byte for byte. Every hard break the
      author put in is theirs, and a `.narrow.txt` is not even consulted here.
    * **narrow with a `<id>.narrow.txt`** — that file, verbatim. The author has
      set this room at 40 themselves and their version wins outright.
    * **narrow with none** — `reflow` the wide prose. This is the ordinary case,
      and it is why the convention costs a house nothing: a room only needs a
      narrow file when its layout is deliberate (mirror writing, a list, a
      scrap of art) and would flatten.

    `session.is_narrow` and `session.width` are live properties reading the
    transport's latest report, so rotating a phone re-sets the next room view.
    Nothing is decided at login and nothing is cached.
    """
    if not session.is_narrow:
        return room.description
    if room.description_narrow:
        return room.description_narrow
    return reflow(room.description, session.width)
