# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations
import time
from ..accounts import valid_name
from ._shared import (DESC_MAX, JAILED_OUTBOUND, MAIL_FLOOD_MSG, NARROW_COLUMNS,
                      NARROW_INDENT, effective_description, framed, house_setting,
                      narrow_stacked, resident_message, wrap_at, wrap_page)

# A bare `.desc` used to CLEAR your description — one stray word-less line and
# your entry was blank, with nothing asked and nothing to undo it. It shows the
# description now, and only offers to clear one you actually wrote. The y/n is
# an async prompt, so like `.passwd` and `.logoutall` the handler returns this
# sentinel and the session loop does the asking.
DESC_CONFIRM = "desc_confirm"


def _idle_str(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds < 3600:
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{hours}:{minutes:02d}"


# ---------------------------------------------------------------------------
# Navigation & presence
# ---------------------------------------------------------------------------

WHO_WIDTH = 78


def cmd_who(session, args: str) -> None:
    now = session.now()
    residents = sorted(session.world.all_connected(), key=lambda r: r.name.lower())

    # NUTS-style wholist: a dated header, a blank line, one line per user reading
    # as a sentence (name + description, or "has no description"), then aligned
    # room + idle columns, a blank line, and a total footer.
    def name_cell(r) -> str:                     # "(AFK)" marker rides the name cell
        return f"{r.name} (AFK)" if r.afk else r.name

    def room_name(r) -> str:
        room = session.world.room(r.room_id)
        return room.name if room else r.room_id

    def desc_of(r) -> str:
        shown = effective_description(session, r.description)
        return shown if shown else "has no description"

    def idle_of(r) -> str:
        return _idle_str(now - r.last_input)

    width = session.width
    header = "Current users on " + time.strftime(
        "%a %b %e %H:%M:%S", time.localtime(session.now_wall()))

    n = len(residents)
    footer = f"Total of {n} user{'' if n == 1 else 's'} logged on"

    if width <= NARROW_COLUMNS:
        # Two lines per resident, because four columns do not fit in forty
        # characters and truncating the description to nothing (which is what
        # the wide geometry degrades to) throws away the only part of the row a
        # person actually reads. Name and description first — the sentence — and
        # the where-and-how-long indented beneath it as a subordinate fact.
        lines = []
        for r in residents:
            if lines:
                lines.append("")
            lines.extend(wrap_at(f"{name_cell(r)} — {desc_of(r)}", width))
            lines.append(f"{NARROW_INDENT}{room_name(r)}, idle {idle_of(r)}")
        # The header carries a full timestamp that does not fit; at 40 the date
        # is what matters and the seconds are not.
        header = "Users on " + time.strftime(
            "%a %b %e %H:%M", time.localtime(session.now_wall()))
        session.send_raw(framed(header, "\n".join(lines), footer, width=width))
        return

    w_room = max((len(room_name(r)) for r in residents), default=0)
    idle_w = max((len(idle_of(r)) for r in residents), default=len("00:00"))
    gap = 2                                              # sentence -> room column
    tail_w = w_room + len("   idle ") + idle_w           # "<room>   idle <mm:ss>"
    sent_cap = max(10, WHO_WIDTH - gap - tail_w)         # bound so rows stay <= 78

    built = []
    for r in residents:
        lead = f" {name_cell(r)} "                       # leading space + name + space
        sentence = lead + desc_of(r)
        if len(sentence) > sent_cap:                     # truncate the desc, keep the name
            sentence = (lead + desc_of(r)[: max(0, sent_cap - len(lead))]).rstrip()
        built.append((sentence, r))
    w_sent = max((len(s) for s, _ in built), default=0)

    lines = []
    for sentence, r in built:
        tail = f"{room_name(r):<{w_room}}   idle {idle_of(r)}"
        lines.append(f"{sentence:<{w_sent}}{' ' * gap}{tail}".rstrip())
    # The total was always the closing line; now it IS the closing rule.
    session.send_raw(framed(header, "\n".join(lines), footer, width=width))


def cmd_desc(session, args: str):
    """Set your description — or, bare, SHOW it and offer to clear it.

    Bare `.desc` used to clear it outright, which made an accidental wipe one
    keystroke away and gave you no way to simply ask what yours said. A bare
    command should show, not surprise.

    Three cases, and the difference between the last two is the point:

    * **Your own words** — shown, then `Clear it? (y/n)`. Only an explicit `y`
      empties it (the session loop asks; see `DESC_CONFIRM`).
    * **The house default** — shown, marked as the house's, and NO prompt.
      There is nothing personal to clear, and offering to clear something you
      did not write would be offering to do nothing.
    * **Neither** — said plainly, and no prompt for the same reason.
    """
    text = args.strip()
    res = session.resident

    if not text:
        own = (res.description or "").strip()
        if own:
            session.send(f"Your description: {own}")
            return DESC_CONFIRM
        house = house_setting(session, "desc").strip()
        if house:
            # What others see when they examine you — but it is the house's
            # wording, not yours, so there is nothing here to clear.
            session.send(f"You have no description of your own. The house "
                         f"gives you: {house}")
        else:
            session.send("You have no description. "
                         "Set one with .desc <text>")
        return None

    # Keep descriptions to a single sensible line. ⚠ DESC_MAX rather than a
    # literal since v1.52.4: `.ownerset desc` writes the HOUSE default for
    # this same field and has to stop at the same place, and two spellings of
    # one bound is how they come to disagree.
    desc = " ".join(text.split())
    if len(desc) > DESC_MAX:
        desc = desc[:DESC_MAX]
    res.description = desc
    session.accounts.set_description(res.name, desc)
    session.send(f"Description set: {desc}")
    return None


def clear_description(session) -> None:
    """Empty the caller's own description. The session loop calls this after a
    confirmed `y`; nothing else does."""
    res = session.resident
    res.description = ""
    session.accounts.set_description(res.name, "")
    session.send("Description cleared.")


def cmd_mute(session, args: str) -> None:
    name = args.strip()
    if not name:
        session.send("Mute whom?")
        return
    target, ambiguous = session.world.resolve_resident(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return
    if (target is session.resident) or (
        target is None and name.casefold() == session.resident.name.casefold()
    ):
        session.send("You can't mute yourself.")
        return
    # Muting is a lenient session filter: you may pre-mute someone not present.
    key = target.name.casefold() if target else name.casefold()
    display = target.name if target else name
    muted = session.resident.muted
    if key not in muted:
        # A pre-mute of an absent name must at least be a legal account name
        # (no arbitrary strings), and the list is capped so it can't grow
        # without bound.
        if target is None and not valid_name(name):
            session.send("That isn't a name anyone could have.")
            return
        cap = getattr(session.config, "mute_cap", 100)
        if len(muted) >= cap:
            session.send(f"Your mute list is full ({cap}). .unmute someone first.")
            return
    muted.add(key)
    session.send(f"You no longer hear {display}. (.unmute {display} to undo)")


def cmd_unmute(session, args: str) -> None:
    name = args.strip()
    if not name:
        session.send("Unmute whom?")
        return
    target, _ = session.world.resolve_resident(name)
    key = target.name.casefold() if target else name.casefold()
    display = target.name if target else name
    session.resident.muted.discard(key)
    session.send(f"You can hear {display} again.")


def cmd_afk(session, args: str) -> None:
    """Mark yourself AFK with an optional message, and tell the room.

    The room line reads exactly as the notice someone gets when they .tell an AFK
    person — `Name is AFK: <message>` — so "away" says the same thing wherever
    you meet it. It is ambient, so `.ignore afk_msg` hides it; the tell-time
    notice is NOT ambient (it answers a command you just typed) and always shows.

    Any later input clears AFK (handled in the session loop). A bare .afk uses
    your .set afk_msg, or the house's.
    """
    res = session.resident
    msg = " ".join(args.split()) or resident_message(session, res, "afk_msg")
    res.afk = True
    res.afk_message = msg
    session.world.broadcast_room(res.room_id, None, f"{res.name} is AFK: {msg}",
                                 exclude=[res.name], category="afk_msg")
    session.send(f"You're now AFK: {msg}")


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def cmd_profile(session, args: str) -> None:
    """Enter the line editor to write/replace your profile."""
    max_lines = session.config.profile_max_lines

    def save(sess, text: str) -> None:
        sess.accounts.set_profile(sess.resident.name, text)
        sess.send("Profile saved." if text.strip() else "Profile cleared.")

    session.start_editor("profile", max_lines, save)


def cmd_examine(session, args: str) -> None:
    """Look someone over — or, bare, look yourself over as others do.

    Bare `.examine` used to answer "Examine whom?", which is a question nobody
    was asking: the interesting thing about your own entry is what OTHER people
    see when they type your name, and there was no way to check it. It renders
    through exactly the same path as any other target — same house-default
    fallbacks, same rank line, same width handling — because a preview that
    took a different route would be a preview of the wrong thing.
    """
    name = args.strip()
    if not name:
        acct = session.accounts.get(session.resident.name)
        if acct is None:
            # No stored account behind this session (a unit fixture, or an
            # account deleted mid-session). Nothing to preview, and nothing
            # worth an error either.
            session.send("There's nothing to examine.")
            return
    else:
        display, ambiguous = session.accounts.resolve(name)
        if ambiguous:
            session.send(f"Who do you mean by '{name}'?  Be more specific.")
            return
        acct = session.accounts.get(display) if display else None
        if acct is None:
            session.send(f"There's no one called {name}.")
            return
    # Their own words where they have written any, the house's where they have
    # not — absent-means-default, the same as the message wording.
    desc = effective_description(session, acct.description)
    width = session.width
    if desc and width <= NARROW_COLUMNS:
        # At 40 a name followed by a description wraps into one grey block and
        # the name stops being the first thing you see. Give it its own line.
        lines = [acct.name, *wrap_at(desc, width - len(NARROW_INDENT), NARROW_INDENT), ""]
    else:
        header = f"{acct.name} {desc}" if desc else acct.name
        lines = [header, ""]   # a description is theirs, never truncated
    # Rank line (staff only). Staff and the owner wear it; residents get none.
    theme = session.theme
    if acct.name.casefold() == session.config.owner_name.casefold():
        lines.append(f"The {theme.owner_rank} of {theme.instance_name}.")
    elif theme.is_staff_rank(acct.rank):
        # ⚠ "A {rank} of this house." put an article in front of a word the
        # OPERATOR chose; the shipped ladder's staff rung is `staff`, and "A
        # staff of this house" is not a sentence. The rank goes after a verb,
        # where any noun reads.
        lines.append(f"Their rank here is {theme.rank(acct.rank)}.")
    house_profile = house_setting(session, "profile")
    if acct.profile.strip():
        lines.append(acct.profile)
    elif house_profile.strip():
        lines.append(house_profile)
    else:
        lines.append("They keep their own counsel.")
    session.send_raw(framed(f"Examining {acct.name}",
                            wrap_page("\n".join(lines), width), width=width))


# ---------------------------------------------------------------------------
# Mail — the one thing the server holds (addressed and consented)
# ---------------------------------------------------------------------------

def _deliver_mail(session, canonical_name: str, body: str) -> None:
    ok = session.mail.deliver(canonical_name, session.resident.name, body)
    if ok:
        session.send(f"Mail sent to {canonical_name}.")
    else:
        session.send("Their mailbox is full.")


def _read_mail(session) -> None:
    msgs = session.mail.read(session.resident.name)
    if not msgs:
        session.send("No mail.")
        return
    width = session.width
    narrow = width <= NARROW_COLUMNS
    out = []
    for m in msgs:
        if out:
            out.append("")
        # Narrow splits the head in two: sender and date side by side is the
        # first thing to overflow once a name is more than a few letters long,
        # and the sender is the half you scan for.
        if narrow:
            out.append(f"{m.index}) from {m.sender}")
            out.append(f"{NARROW_INDENT}{m.date_str()}")
        else:
            out.append(f"{m.index}) from {m.sender}  ({m.date_str()})")
        out.append(m.body)
    n = len(msgs)
    session.send_raw(framed(f"You have {n} message{'s' if n != 1 else ''}",
                            wrap_page("\n".join(out), width), width=width))
    # Shown is read. The whole box is displayed in one screen, so the whole box
    # is marked — and the login notice stops firing until something new lands.
    session.mail.mark_read(session.resident.name)


def cmd_mail(session, args: str) -> None:
    args = args.strip()
    if not args:
        _read_mail(session)
        return
    if session.resident.jailed:            # reading mail is fine; SENDING is not
        session.send(JAILED_OUTBOUND)
        return

    parts = args.split(None, 1)
    target = parts[0]
    display, ambiguous = session.accounts.resolve(target)
    if ambiguous:
        session.send(f"Who do you mean by '{target}'?  Be more specific.")
        return
    acct = session.accounts.get(display) if display else None
    if acct is None:
        session.send(f"There's no one called {target}.")
        return

    if len(parts) == 2 and parts[1].strip():
        # One-line send.
        if not session.mail_bucket.take(session.now()):
            session.send(MAIL_FLOOD_MSG)
            return
        _deliver_mail(session, acct.name, parts[1].strip())
        return

    # No text: compose a multi-line letter. Spend the mail token on entry so a
    # burst of compose sessions can't become a spam channel.
    if not session.mail_bucket.take(session.now()):
        session.send(MAIL_FLOOD_MSG)
        return
    canonical = acct.name
    max_lines = session.config.mail_max_lines

    def save(sess, text: str) -> None:
        if not text.strip():
            sess.send("Empty letter — nothing sent.")
            return
        _deliver_mail(sess, canonical, text)

    session.send(f"Writing to {canonical}.")
    session.start_editor("mail", max_lines, save)


def cmd_dmail(session, args: str) -> None:
    arg = args.strip()
    # ⛔ TWO guards, and they catch two DIFFERENT `int()` raises (v1.52.3,
    # audit C3):
    #
    #   * `isascii()` — `str.isdigit()` is True for `²`, `³`, `¹`, `①` and the
    #     rest of Unicode's non-decimal numerals, and `int()` accepts only
    #     decimal digits (Nd). `.dmail ²` therefore passed the guard, raised
    #     ValueError out of the handler, unwound past `dispatch` into `_serve`'s
    #     blanket handler — and CLOSED THE CALLER'S CONNECTION. Floor rank, one
    #     keystroke.
    #   * `len(arg) > 9` — CPython refuses `int()` on a run longer than 4300
    #     digits. The original comment named this one; the character class is
    #     the half it missed.
    if not (arg.isascii() and arg.isdigit()) or len(arg) > 9:
        session.send("Delete which message?  Usage: .dmail <number>")
        return
    n = int(arg)
    if session.mail.delete(session.resident.name, n):
        session.send(f"Message {n} deleted.")
    else:
        session.send("No such message.")
