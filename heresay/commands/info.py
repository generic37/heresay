# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations
from .. import __version__
from ._shared import framed, render_agreement


NOTHING_YET = "Nothing yet."


# ---------------------------------------------------------------------------
# Review: the arrival room's short public memory, and your own private lines
# ---------------------------------------------------------------------------

REV_HEADER = "*** The last few lines of the entry room ***"
REV_ELSEWHERE = "There's nothing to review here."
REVTELL_HEADER = "*** Your last few tells and remotes ***"


def _replay(session, header: str, lines: list) -> None:
    """Render a block of stored lines the way they were delivered live.

    Buffers hold LOGICAL lines — one entry per thing someone said — because the
    wrap is a property of the screen, not of the sentence. So replay must go
    through send(), the same path live delivery uses, rather than send_raw:
    that is what makes a replayed line break at exactly the columns it broke at
    when it happened, by construction rather than by a second implementation
    agreeing with the first. (.who/.map/.accounts/help are raw on purpose —
    they are fixed-width grids and art, which wrapping would wreck.)

    dim=False for the same reason: live speech and tells are not dim, so a
    replay that dimmed them would not match what the room saw either.

    One unit, so one blank line before the block and none between the physical
    lines of a single message — a wrapped sentence is still one thing said.
    """
    session.send("\n".join([header, ""] + list(lines)), dim=False)


def cmd_rev(session, args: str) -> None:
    """Replay the arrival room's last lines — only while standing in it.

    The doctrine, stated once: concentration is assumed in the side rooms. If
    you want privacy from whoever walks in later, speak somewhere other than
    the public room.
    """
    res = session.resident
    if res.room_id != session.world.start_room:
        session.send(REV_ELSEWHERE)
        return
    lines = session.world.review_lines(res, session.now())   # faded + muted, per viewer
    if not lines:
        session.send(NOTHING_YET)
        return
    _replay(session, REV_HEADER, lines)


def cmd_revtell(session, args: str) -> None:
    """Replay your own last private lines, exactly as you saw them.

    The ONLY read of this store, anywhere, and it takes the owner's own name.
    """
    store = getattr(session, "revtells", None)
    lines = store.read(session.resident.name) if store is not None else []
    if not lines:
        session.send(NOTHING_YET)
        return
    _replay(session, REVTELL_HEADER, lines)


AGREEMENT_TITLE = "The agreement"
NO_AGREEMENT = "No agreement text available."


def cmd_agreement(session, args: str) -> None:
    """Show the agreement. No prompt, no question, nothing to answer.

    Everyone, at any rank: what you agreed to is not a privilege to read back,
    and until now the only way to see it again was to make a second account.

    ⚠ It is the CURRENT text, and that is the right text rather than a
    convenient one: consent is tied to the version (`agreement_version` is the
    sha256 of the resolved words), and any change re-consents everyone at their
    next login. So a logged-in resident has agreed to exactly this — there is no
    older copy that could be theirs, and none is kept.

    The layout goes through `render_agreement`, the same function the signup and
    re-consent screens use, so this is byte-for-byte the text they were shown
    when they agreed — at 40 as at 78. Framed like every other screen-sized
    block: the rules are the house's way of saying "this is the server showing
    you something", which is precisely what distinguishes it from the prompt it
    is NOT.
    """
    text = getattr(session, "agreement_text", "") or ""
    if not text.strip():                  # a unit session with no house behind it
        session.send(NO_AGREEMENT)
        return
    width = session.width
    session.send_raw(framed(AGREEMENT_TITLE, render_agreement(text, session),
                            width=width))


def cmd_version(session, args: str) -> None:
    """What is running, in one line — the instance's name, the engine's name, and
    the version. Every level: knowing which house you are standing in, and on
    what engine, is not a privilege."""
    session.send(f"{session.theme.instance_name}, Heresay version {__version__}")


def cmd_source(session, args: str) -> None:
    """Where this house's source can be had — the AGPL §13 offer, in-world.

    ⛔ §13 obliges an operator running a MODIFIED Heresay as a network service to
    offer its users the corresponding source. The licence has always been in the
    repository; what was missing was any way for somebody standing IN the house
    to be told where to look. A file in a checkout is not an offer to a user who
    only ever sees a text prompt.

    Floor rank, and not a decision worth agonising over: the licence says
    "users", so gating it above the people it names would be an odd reading of
    the obligation the command exists to discharge. Assignable like anything
    else, all the same.

    ⚠ It prints the VERSION beside the URL, because "the source" means the
    source of what is actually running — a URL alone points at whatever the
    branch happens to be today, which may not be this house.
    """
    url = getattr(session.config, "source_url", "") or ""
    version = f"Heresay version {__version__}"
    if not url.strip():
        # ⚠ Says so plainly rather than naming the upstream project. A modified
        # instance pointing at unmodified upstream would be a wrong answer
        # wearing the shape of a right one — worse than admitting the gap.
        session.send(f"{version}\nSource URL not configured — ask the "
                     f"{session.theme.owner_rank}.")
        return
    session.send(f"{version}\nSource: {url.strip()}")
