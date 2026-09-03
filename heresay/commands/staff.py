# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations
import ipaddress
import math
import time
from .. import capture
from ..state import (ENUM_KEYS, HOUSE_DEFAULTS, INT_KEYS, MULTILINE_KEYS,
                     RAISE_BOUNDS_KEYS, house_phrase, phrase_or_none,
                     presence_line)
from ._shared import (
    DESC_MAX, MAIL_FLOOD_MSG, MSG_MAX, NARROW_COLUMNS, NARROW_INDENT,
    _describe_room, cap_word, clean_message, framed, house_setting, log,
    narrow_stacked, window_cutoff, window_phrase, wrap_at, wrap_hard, wrap_page,
)
# settings.py owns the client-push helpers and does not import this module, so
# there is no cycle here.
from .settings import push_raise_bounds_all


LOGOUTALL = "logoutall"         # ditto: needs a y/n confirmation prompt
# Arch account powers. Like the above, the prompts are async, so the handler
# validates and the session loop does the work. The target rides on
# `session.pending_target`.
DELETEUSER = "deleteuser"
RESETPASSWORD = "resetpassword"
# `.mailpurge` joined them in v1.41.0: it destroys a whole mailbox, which is as
# irreversible as the two above, so it asks for the caller's own password too.
MAILPURGE = "mailpurge"
# .ban_ip shows its collateral and asks y/n, so the prompt is async and the
# handler hands the pending ban to the session loop on `session.pending_ban_ip`.
BAN_IP = "ban_ip"

# The shared staff letterbox lives in the ordinary MailStore under this reserved
# key. The leading '#' cannot occur in a real account name (names start with a
# letter), so it never collides — and the box inherits mail's encryption, caps,
# and 180-day purge (the covenant's addressed-and-consented exception covers it).
STAFF_BOX = "#staff"

# ---------------------------------------------------------------------------
# The report box (v1.50.0)
#
# ⛔ ONE new thing: a reserved mailbox key. Everything else is the mail system
# already here — `MailStore`, the compose editor, Fernet, the caps, the unread
# flag, the purge loop, the read path. A report IS mail; it simply goes to a box
# rather than to a person, and it is ABOUT the person named.
#
# The leading '#' cannot occur in an account name (names start with a letter),
# so it can never collide with a resident's box — the same trick `#staff` has
# used since Phase 4, and the same reason `.mailpurge` cannot reach either of
# them: that command resolves an ACCOUNT, and neither box is one.
#
# ⚠ THE SUBJECT LIVES INSIDE THE ENCRYPTED BLOB, not beside it. `mailboxes.json`
# keeps three plaintext fields — the box key, a timestamp and the read flag —
# and "who has been reported" is not going to become the fourth. So the reported
# name is written into the BODY, where Fernet covers it like every other word.
# That is why there is no `subject=` parameter on `deliver()`: adding one would
# have meant adding a plaintext field, and the covenant answer to that is no.
REPORT_BOX = "#report"

REPORT_NOTICE = "A new report has been filed.  (.read_report to view)"


def _log_action(session, action: str, target: str,
                outcome: str = None) -> None:
    """Record one staff action as a fact — actor, action, target.

    The ONE place names are recorded, and never message content: `.gag Bob` keeps
    "Bob", not a syllable of why. This is what `.staff_actions` reads back, and
    it is the reason the store fsyncs `staff_action` — an accountability record
    that a power cut can eat is not one.

    Goes to the EVENT STORE, not journald (v1.30.0). Journald keeps process
    output; a domain fact lives in exactly one place. A session with no store
    behind it (a unit-test fake) simply records nothing.

    ``outcome`` is set only by actions that can end without doing anything —
    just `.ban_ip` today, which is recorded whether it is confirmed or cancelled
    because RUNNING it is itself the notable act: it is the moment a staff
    member is shown who shares an address.
    """
    events = getattr(session, "events", None)
    if events is not None:
        facts = dict(actor=session.resident.name, action=action, target=target)
        if outcome is not None:
            # A fourth field, only where an action can END without doing
            # anything — `.ban_ip`, which is audited on the ATTEMPT (see
            # cmd_ban_ip). One of the two words below; still a fact, still no
            # content, and the covenant guards cover it.
            facts["outcome"] = outcome
        events.record("staff_action", **facts)


# The same fact-logging, for the staff actions whose prompts live in the server's
# session loop rather than in a handler here (.deleteuser / .resetpassword).
def log_staff_action(session, action: str, target: str,
                      outcome: str = None) -> None:
    _log_action(session, action, target, outcome)


# ---------------------------------------------------------------------------
# The residents' channel to staff — a live call and a shared letterbox
# ---------------------------------------------------------------------------

# The constant NAMES stay (internal identifiers); their WORDS follow the LEVEL
# now, so a re-themed house says its own word here.
#
# ⚠ Both used to read "a {theme.staff}" — the raw `[ranks] staff` ANCHOR, which
# is a RANK name, in an article slot. Two faults in one string: the level is what
# `.call_staff` reaches (everyone at staff and above, whatever their rung), and
# "a staff" is not a person. The plural says both correctly on any ladder.
def keeper_called_msg(theme) -> str:
    return f"Your call has gone out to the {theme.staff_plural}."
def keeper_refused_msg(theme) -> str:
    return (f"Not so fast — you called the {theme.staff_plural} only moments "
            f"ago. Give it a little while.")


def cmd_call_staff(session, args: str) -> None:
    """Residents' emergency channel: page everyone at staff level and above.

    Works while gagged (this is the discoverable version of the gagged-appeal
    rule). Sternly rate-limited (1 per staff_call_seconds). The room name is
    included so staff know where to go; the call text is NOT logged."""
    text = args.strip()
    staff_word = session.theme.staff_word
    staff_plural = session.theme.staff_plural
    if not text:
        session.send(f"Call the {staff_plural} about what?  "
                     f"Usage: .call_staff <text>")
        return
    if not session.staff_bucket.take(session.now()):
        session.send(keeper_refused_msg(session.theme))
        return
    res = session.resident
    room = session.world.room(res.room_id)
    room_name = room.name if room else res.room_id
    on_duty = [r for r in session.world.all_connected() if r.is_staff()]
    if not on_duty:
        session.send(f"No {staff_plural} awake just now. "
                     f"Leave {staff_word} mail: .mail_staff <text>")
        return
    call = f"{res.name} ({room_name}) calls for the {staff_plural}: {text}"
    for r in on_duty:
        if r.session is not None:
            r.session.send(call, dim=False)
    # A fact — WHO called, never the text (room-text discipline: the call itself
    # is conversation, and conversation is not written down). To the event store
    # rather than journald, so no resident's name is left in the process log:
    # that split is the whole point of the store.
    events = getattr(session, "events", None)
    if events is not None:
        events.record("staff_call", account=res.name)
    session.send(keeper_called_msg(session.theme))


def _deliver_keeper_mail(session, body: str) -> None:
    ok = session.mail.deliver(STAFF_BOX, session.resident.name, body)
    if ok:
        # A fact — WHO wrote, never a word of what (the note itself is mail, and
        # mail is read by the people it was addressed to, not by a log). The
        # other half of `staff_call`: both are "a resident reached for staff",
        # and `.staff_calls` shows them together.
        #
        # ⚠ Recorded on DELIVERY only. A full letterbox stored nothing, so
        # counting it would put a contact in the tally that never arrived.
        events = getattr(session, "events", None)
        if events is not None:
            events.record("staff_mail", account=session.resident.name)
        session.send(f"Left with the {session.theme.staff_plural}.")
    else:
        session.send(f"The {session.theme.staff_word} letterbox is full.")


def cmd_mail_staff(session, args: str) -> None:
    """Leave a note in the shared staff letterbox. One-line with text, or the
    multi-line editor when bare. Any resident, anytime; works while gagged. Uses
    the mail rate bucket so it can't become a spam channel."""
    text = args.strip()
    if not session.mail_bucket.take(session.now()):
        session.send(MAIL_FLOOD_MSG)
        return
    if text:
        _deliver_keeper_mail(session, text)
        return

    def save(sess, body: str) -> None:
        if not body.strip():
            sess.send("Empty note — nothing sent.")
            return
        _deliver_keeper_mail(sess, body)

    session.send(f"Writing to the {session.theme.staff_plural}.")
    session.start_editor("mail", session.config.mail_max_lines, save)


def cmd_staffmail(session, args: str) -> None:
    """Staff-only: read the shared staff letterbox (numbered, sender + date)."""
    msgs = session.mail.read(STAFF_BOX)
    if not msgs:
        session.send(f"No {session.theme.staff_word} mail.")
        return
    width = session.width
    narrow = width <= NARROW_COLUMNS
    out = []
    for m in msgs:
        if out:
            out.append("")
        # Same narrow split as the resident mailbox: sender first, date beneath.
        if narrow:
            out.append(f"{m.index}) from {m.sender}")
            out.append(f"{NARROW_INDENT}{m.date_str()}")
        else:
            out.append(f"{m.index}) from {m.sender}  ({m.date_str()})")
        out.append(m.body)
    n = len(msgs)
    # ⚠ The LEVEL word, not the staff RANK (v1.61.0). This box is read by every
    # account at the staff level and above, whatever rung they hold, so naming
    # the anchor rung described the wrong set of people. Modifier slot, so a
    # mass noun reads correctly: "Staff mail" on the shipped words, "Warden
    # mail" on a house that renamed the level.
    session.send_raw(framed(f"{cap_word(session.theme.staff_word)} mail — "
                            f"{n} message{'s' if n != 1 else ''}",
                            wrap_page("\n".join(out), width), width=width))


def cmd_dstaffmail(session, args: str) -> None:
    """Staff-only: delete a staff-mail message for EVERYONE (shared box — whoever
    handles it clears it for the rest). Logged as a staff-action fact."""
    arg = args.strip()
    # ⛔ `isascii() and isdigit()`, not `isdigit()` alone (v1.52.3, audit C3):
    # `str.isdigit()` is True for `²`/`³`/`¹`/`①`, which `int()` REFUSES. The
    # length bound guards a different `int()` raise (CPython's 4300-digit
    # conversion limit); neither guard implies the other. See cmd_dmail.
    if not (arg.isascii() and arg.isdigit()) or len(arg) > 9:
        session.send("Delete which message?  Usage: .dstaffmail <number>")
        return
    n = int(arg)
    if session.mail.delete(STAFF_BOX, n):
        _log_action(session, "dstaffmail", f"#{n}")
        session.send(f"{cap_word(session.theme.staff_word)} message {n} "
                     f"cleared for all.")
    else:
        session.send("No such message.")


# ---------------------------------------------------------------------------
# Reports — mail, wearing a different label (v1.50.0)
# ---------------------------------------------------------------------------
#
# `.report` is `.mail` with the destination changed and the named person moved
# from the envelope to the subject. Both forms are the ones `.mail` already has:
# bare opens the same editor, with text sends one line. Both go through
# `MailStore.deliver`, so encryption, the cap, the unread flag and the purge are
# not re-implemented anywhere.
#
# ⛔ TWO ORDINARY PERMISSION-SETTABLE COMMANDS, and that is the whole access
# model. `.report` seeds to the floor (residents can report) and `.read_report`
# to the arch — which keeps reports off staff eyes, since a report may be ABOUT
# a staff account. ⚠ That is the operator's PERMISSION CHOICE, not a hardcoded
# lock: a house that wants its staff reading reports moves one line in
# `permissions.toml`, exactly like any other command.


def _report_readers(session):
    """Every ONLINE session that may run `.read_report`, minus the filer.

    ⚠ Asked through `may_run`, i.e. through the live command table — never
    through a rank comparison of its own. If the operator hands `.read_report`
    to the staff level, everyone at that level starts getting the ping the same
    instant, with no second place to remember to change.
    """
    from . import may_run
    out = []
    for r in session.world.all_connected():
        if r.session is None or r is session.resident:
            continue
        if may_run("read_report", r.session):
            out.append(r)
    return out


def _capture_report(session, reporter: str, accused: str, stored: str) -> None:
    """Write the report and everything the house knows about both people to disk.

    ⚠ Everything here is defensive on purpose. A session that is missing a store
    (a fake, a stripped-down harness) must still be able to FILE a report — the
    capture is the operator's record, and refusing the report because the record
    could not be written would be the wrong way round.
    """
    cfg = getattr(session, "config", None)
    if cfg is None or not hasattr(cfg, "report_capture_path"):
        return
    events = getattr(session, "events", None)
    rows = []
    if events is not None:
        try:
            rows = capture.relevant_events(events.read(), (reporter, accused))
        except Exception:                                   # pragma: no cover
            rows = []
    capture.write_capture(
        cfg.report_capture_path(),
        reporter=reporter, accused=accused, body=stored,
        filed=session.now_wall(), rows=rows,
        accounts=getattr(session, "accounts", None),
        bans=getattr(session, "bans", None),
        history=getattr(session, "ban_history", None),
        window_days=getattr(cfg, "event_retention_days", None),
    )


def _deliver_report(session, subject: str, body: str) -> None:
    """File one report. The subject rides INSIDE the encrypted body."""
    stored = f"About {subject}:\n\n{body}"
    if not session.mail.deliver(REPORT_BOX, session.resident.name, stored):
        session.send("The report box is full — tell the "
                     f"{session.theme.staff_plural} directly.")
        return
    # ⛔ A FACT, AND NOT EVEN A NAME. `staff_mail` records WHO reached for staff;
    # this records the BARE FACT and nothing else, because the only fields a
    # report has are the reporter, the person reported and the words — and the
    # middle one is exactly the thing a log must not casually accumulate. An
    # unproven accusation is not a moderation record. The report itself is mail,
    # read by the people it was addressed to; `.staff_actions` shows what was
    # DONE about it, which is the part that belongs in an audit trail.
    #
    # ⚡ `report_filed` (v1.57.0) carries `{ts, type}` — NO reporter, NO subject,
    # NO text, and no field that could hold one. It exists so an external
    # watcher can nudge the reader's phone (see
    # `deploy/install/heresay-report-watch.sh`); the engine stays free of any
    # outbound network, and the nudge it enables says "a report is waiting" and
    # never a word of what is in it. v1.50.0's "records NO event" is narrowed
    # exactly this far and no further — see events.py for the full argument.
    #
    # ⚠ AFTER the deliver, never before: a full box stored nothing, and a
    # doorbell for a report that was never filed rings at nothing. Same rule as
    # `staff_mail`, for the same reason.
    events = getattr(session, "events", None)
    if events is not None:
        events.record("report_filed")

    # ⛔⛔ THE CAPTURE FILE — the whole record, to the operator's disk, NOW.
    #
    # This is the one moment the whole picture exists. `report_filed` carries no
    # names by design, so nothing outside the daemon knows WHO to capture; and
    # the context this sweeps up ages out of `events.jsonl` in ten days, so
    # anything that waited would be capturing a window that had already moved.
    # See capture.py for the covenant argument — it is a named, bounded
    # exception, not a precedent.
    #
    # ⚠ AFTER the deliver and after the event, and its failure costs neither:
    # `write_capture` never raises, so a full disk loses the CAPTURE and never
    # the report. The ordering is deliberate — the report is on disk and fsynced
    # before anything else is attempted.
    _capture_report(session, session.resident.name, subject, stored)

    session.send("Report filed. Thank you.")
    # ⚠ UNLIKE MAIL, this does not wait for a re-login. Somebody has just told
    # the house something is wrong; a reader who is awake should hear so now.
    # The body is NOT in the ping — they open `.read_report` to read it — so an
    # over-the-shoulder glance reveals nothing but that a report exists.
    for r in _report_readers(session):
        r.session.send(REPORT_NOTICE)


def cmd_report(session, args: str) -> None:
    """Report someone to whoever the house lets read reports.

    Two forms, the same two `.mail` has: `.report <name>` opens the multi-line
    editor, `.report <name> <text>` sends one line.
    """
    args = args.strip()
    if not args:
        session.send("Report whom?  Usage: .report <name> [what happened]")
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
    # ⚠ The one refusal. Everything else about `<name>` is just a subject line —
    # no new validation machinery, because a report is a message rather than an
    # action on anybody.
    if acct.name.casefold() == session.resident.name.casefold():
        session.send("You can't report yourself.")
        return
    canonical = acct.name

    if len(parts) == 2 and parts[1].strip():
        _deliver_report(session, canonical, parts[1].strip())
        return

    def save(sess, text: str) -> None:
        if not text.strip():
            sess.send("Empty report — nothing filed.")
            return
        _deliver_report(sess, canonical, text)

    session.send(f"Writing a report about {canonical}.")
    session.start_editor("mail", session.config.mail_max_lines, save)


def cmd_read_report(session, args: str) -> None:
    """Read the report box — the existing mail read path, on a different key.

    ⛔ THERE IS NO DELETE. A report leaves only by expiring
    (`report_retention_days`, 10 by default — the capture file the daemon writes
    at filing is the durable record). That is the deliberate difference
    from mail: the reader is often the person a report might be about the
    handling of, and a box its reader can quietly empty is not accountability.
    """
    msgs = session.mail.read(REPORT_BOX)
    if not msgs:
        session.send("No reports.")
        return
    width = session.width
    narrow = width <= NARROW_COLUMNS
    out = []
    for m in msgs:
        if out:
            out.append("")
        mark = "" if m.read else "* "
        if narrow:
            out.append(f"{mark}{m.index}) from {m.sender}")
            out.append(f"{NARROW_INDENT}{m.date_str()}")
        else:
            out.append(f"{mark}{m.index}) from {m.sender}  ({m.date_str()})")
        out.append(m.body)
    n = len(msgs)
    unread = sum(1 for m in msgs if not m.read)
    title = f"Reports — {n} filed"
    if unread:
        title += f", {unread} new"
    session.send_raw(framed(title, wrap_page("\n".join(out), width), width=width))
    # Shown is read — the same rule as `.mail`, and what stops the login notice
    # repeating. ⚠ The flag is per MESSAGE, not per reader: if a house widens
    # `.read_report` beyond one person, the first to look clears the notice for
    # the rest. That is the staff letterbox's known limitation, inherited here
    # for the same reason — per-reader read state is a design decision about
    # staff workflow, not something to invent halfway.
    session.mail.mark_read(REPORT_BOX)


# ---------------------------------------------------------------------------
# Staff tier (gag / jail / boot / ban) and arch tier (promote / mailpurge)
# ---------------------------------------------------------------------------

def _resolve_online(session, args: str):
    """Resolve the first token of ``args`` to a connected resident, or None
    (having already told the caller why)."""
    name = args.strip().split(None, 1)[0] if args.strip() else ""
    if not name:
        session.send("Whom?")
        return None
    target, ambiguous = session.world.resolve_resident(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return None
    if target is None:
        session.send(f"There's no one called {name} online.")
        return None
    return target


def cmd_gag(session, args: str) -> None:
    target = _resolve_online(session, args)
    if target is None:
        return
    if not _may_act_on(session, target.name, target.rank, "gag"):
        return
    if target.gagged:
        session.send(f"{target.name} is already gagged.")
        return
    target.gagged = True
    _log_action(session, "gag", target.name)
    staff = session.theme.staff_plural
    # Visible: the room learns the voice was taken. ⚠ Passive, and by the PLURAL
    # — "A {staff_word} has silenced X" put an article in front of a level word,
    # which reads "A staff has silenced X" on the shipped house. The actor is
    # deliberately no more identified than it was.
    session.world.broadcast_room(target.room_id, None,
                                 f"{target.name} has been silenced by the {staff}.")
    if target.session is not None:
        target.session.send(
            f"You have been gagged by the {staff}. "
            f"You may still .call_staff <text> to appeal."
        )


def cmd_ungag(session, args: str) -> None:
    target = _resolve_online(session, args)
    if target is None:
        return
    # ⚠ The mirror of `.gag`, and it matters as much: without it somebody could
    # undo a gag placed from ABOVE them, which is acting on a decision above
    # their rank
    # even though the effect is a kindness.
    if not _may_act_on(session, target.name, target.rank, "ungag"):
        return
    if not target.gagged:
        session.send(f"{target.name} isn't gagged.")
        return
    target.gagged = False
    _log_action(session, "ungag", target.name)
    session.world.broadcast_room(
        target.room_id, None,
        f"{target.name}'s voice has been restored by the "
        f"{session.theme.staff_plural}.")
    if target.session is not None:
        target.session.send("Your voice is restored.")


NO_JAIL = "No jail location set."


def cmd_jail(session, args: str) -> None:
    jail = session.world.jail_room
    if jail is None:
        # No room declares `jail: yes` (the void has none either). Say so and
        # stop: somebody reaching for a room that does not exist gets a fact,
        # not a traceback.
        session.send(NO_JAIL)
        return
    target = _resolve_online(session, args)
    if target is None:
        return
    if not _may_act_on(session, target.name, target.rank, "jail"):
        return
    if target.room_id == jail:
        session.send(f"{target.name} is already in jail.")
        return
    from_room = target.room_id
    target.room_id = jail
    target.jailed = True                       # live mirror for the isolation gates
    state = getattr(session, "state", None)
    if state is not None:
        state.set_jailed(target.name)          # PERSISTENT: survives a relog
    _log_action(session, "jail", target.name)
    session.world.broadcast_room(from_room, None,
                                 f"{target.name} is hauled away by the "
                                 f"{session.theme.staff_plural}.",
                                 exclude=[target.name])
    session.world.room_emptied(from_room)
    session.world.broadcast_room(jail, None, f"{target.name} is thrown in.",
                                 exclude=[target.name])
    if target.session is not None:
        target.session.send(house_setting(session, "jail_msg"))
        target.session.send(_describe_room(target.session, target))
    session.send(f"You jail {target.name}.")


def cmd_free(session, args: str) -> None:
    jail = session.world.jail_room
    if jail is None:
        session.send(NO_JAIL)      # nowhere to free anyone from, same fact
        return
    target = _resolve_online(session, args)
    if target is None:
        return
    # ⚠ `.free` acts on a person, so it takes the target-rank rule like the
    # rest — with ONE exception, and the exception is the whole reason the
    # command is written this way: **the arch freeing THEMSELVES**. That is the
    # escape hatch that keeps a jailed arch from being locked in, and
    # strictly-below would refuse it, since nobody is below the top rank —
    # including themselves. So self-freeing is the owner's alone: anyone else who
    # gets jailed cannot simply undo it, and no one can undo a release decision
    # made above their rank (the `.ungag` argument, exactly).
    #
    # v1.41.0 left this ungated to preserve the hatch; that was a bigger opening
    # than the hatch needed.
    if target is session.resident or _same_person(session, target.name):
        if not _caller_is_arch(session):
            session.send("You can't free yourself.")
            return
    elif not _may_act_on(session, target.name, target.rank, "free"):
        return
    if target.room_id != jail:
        session.send(f"{target.name} isn't in jail.")
        return
    target.room_id = session.world.start_room
    target.jailed = False
    state = getattr(session, "state", None)
    if state is not None:
        state.clear_jailed(target.name)        # clears the persistent status
    _log_action(session, "free", target.name)
    session.world.broadcast_room(jail, None, f"{target.name} is released.",
                                 exclude=[target.name])
    session.world.broadcast_room(target.room_id, None, f"{target.name} arrives.",
                                 exclude=[target.name])
    if target.session is not None:
        # The mirror of jail_msg: the freed resident is told, in the house's own
        # words, what just happened to them.
        target.session.send(house_setting(session, "release_msg"))
        target.session.send(_describe_room(target.session, target))
    session.send(f"You free {target.name}.")


def cmd_boot(session, args: str) -> None:
    parts = args.split(None, 1)
    name = parts[0] if parts else ""
    reason = parts[1].strip() if len(parts) > 1 else ""
    if not name:
        session.send("Boot whom?")
        return
    target, ambiguous = session.world.resolve_resident(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return
    if target is None:
        target = session.world.get_resident(name)   # maybe a stale/grace session
    if target is None:
        session.send(f"There's no one called {name} here.")
        return
    if target is session.resident:
        session.send("You can't boot yourself.")
        return
    if not _may_act_on(session, target.name, target.rank, "boot"):
        return
    _log_action(session, "boot", target.name)
    if target.session is not None:
        # Resolves the "already connected" case: the stale session is dropped.
        target.session.kick(f"({reason})" if reason else "")
    else:
        # In the grace window with no live session: finalize the departure now.
        if target.grace_task is not None:
            target.grace_task.cancel()
        # Same house-wide leave line a live boot ends up producing (kick -> the
        # normal departure path -> _announce_logout), so a booted resident's exit
        # reads the same either way: [name phrase], their own logout_msg unless
        # jailed, else the house logout_msg.
        personal = None if target.jailed else phrase_or_none(
            (getattr(target, "messages", None) or {}).get("logout_msg"))
        phrase = personal or house_phrase(getattr(session, "state", None), "logout_msg")
        line = presence_line(target.name, phrase)
        if house_setting(session, "broadcast_scope") == "entry":
            session.world.broadcast_room(session.world.start_room, None, line,
                                         exclude=[target.name],
                                         category="logout_msg")
        else:
            session.world.broadcast_all(None, line, exclude=[target.name],
                                        category="logout_msg")
        session.world.remove_resident(target)
        session.world.room_emptied(target.room_id)
        target.in_grace = False
    session.send(f"You boot {target.name}.")


def _looks_like_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


def _ban_hours(session, parts, default: float):
    """The shared `[hours]` argument. Returns the hours, or None having already
    said why not.

    ⛔ **NON-FINITE IS REFUSED, and `math.isfinite` is the check** (v1.52.3,
    audit C1). `float("nan")` parses happily and then slips *every* guard there
    is, because NaN fails every comparison silently:

      * `nan <= 0` is False, so the positivity check waves it through;
      * `min(nan, ceiling)` returns `nan`, so `ban_max_hours` does not clamp it.

    What landed was a ban whose `until` was NaN — **never enforced** (`nan >
    now` is False), **never listed** by `.bans` (the same comparison), **never
    purged** (`nan <= now` is False), and written to `bans.json` as a bare
    `NaN` token, which is not valid JSON per spec. The staff member was told
    `Banned Mallory for nanh.` and walked away. There was no screen in the
    house on which the mistake was visible.

    ⚠ `inf` was never the same bug — it clamped to the ceiling — and it is
    refused with NaN anyway, because "ban them for infinity hours" is not a
    duration anybody means and one rule reads better than a rule with a
    footnote.

    ⚠ The check runs on `hours` whatever its SOURCE, so it also catches a
    non-finite `ban_default_hours` / `ban_ip_default_hours` in `config.toml`.

    ⚠ **ONE helper, both commands.** `.ban` and `.ban_ip` share it, which is
    why the fix belongs here rather than in either of them —
    `test_both_ban_commands_share_the_hours_parser` pins that they still do.
    """
    hours = default
    if len(parts) > 1:
        try:
            hours = float(parts[1])
        except ValueError:
            session.send("Hours must be a number.")
            return None
    if not math.isfinite(hours) or hours <= 0:
        session.send("Hours must be a positive number.")
        return None
    return min(hours, session.config.ban_max_hours)


def _is_arch(session, name: str) -> bool:
    return name.casefold() == session.config.owner_name.casefold()


# ---------------------------------------------------------------------------
# ⛔ THE TARGET-RANK RULE (v1.41.0; UNIVERSAL since v1.42.0)
#
# **You may act only on ranks STRICTLY BELOW your own, and never on the arch.**
#
# ⚠ v1.41.0 applied this to seven commands and left six alone — `.ban`,
# `.ban_ip`, `.unban`, `.unban_ip`, `.promote`, `.demote` — on the reading that
# a ban was a different kind of act. It is not: the rule was never about the
# verb, it is about WHO YOU ARE ENTITLED TO REACH, and a ban reaches a person as
# surely as a gag does. v1.42.0 covers every command that acts on a person, so
# "does this one check the target?" is answered yes rather than by a list
# somebody has to keep. `test_target_rank_universal` derives that list from the
# REGISTRY — every gated command whose usage takes a <name> — so the next one
# cannot be forgotten the way these six were.
#
# Where it came from: every command checked whether the ACTOR could run it, and
# only some checked whether the TARGET could be acted upon. That gap was nearly
# unreachable while the ladder was three rungs and the account powers were
# arch-only — but v1.38.0 made every command assignable to any rung, and the
# audit found what that opened:
#
#   * `.promote` had no target check at all, so a holder assigned it below arch
#     promoted THEMSELVES one call at a time to the rung under arch;
#   * `.resetpassword` refused only the arch (v1.33.0's fix), so on a ladder with
#     an intermediate rung a warden could reset a High Warden's password —
#     using their OWN password — and walk in as them. The v1.33.0 reasoning did
#     not stop at the arch; it stopped wherever there is a rank above the holder,
#     and multi-rung ladders put one there;
#   * `.gag`/`.jail`/`.boot` had no check whatsoever: a staff account could gag, jail
#     and boot the config-named arch, and each other.
#
# ONE rule in ONE place, because four separate near-identical checks is how the
# next command gets added without one. STRICTLY below, not at-or-below: peers
# are excluded deliberately — one staff account moderating another is a policy a
# house should choose, not something it inherits from an omission.
#
# ⚠ The arch is refused by NAME as well as by rank. The rank check alone would
# already stop them (nobody outranks the top), but the owner is an identity
# rather than a rung — `_rank_for` gives them the top rank whatever is stored —
# so the name check is what makes the refusal true even mid-reorg.
#
# An ARCH caller loses nothing: "strictly below the top" is everyone else.
# ---------------------------------------------------------------------------

def _may_act_on(session, target_name: str, target_rank: str, verb: str) -> bool:
    """True if the caller may use ``verb`` on this target; else it says why.

    ``target_rank`` is what the target HOLDS — the stored rank for an account,
    the live rank for a connected resident. Unknown ranks read as the floor, the
    same fail-safe direction as everywhere else, so an unreadable rank can never
    make somebody harder to moderate than they should be.
    """
    theme = session.theme
    if _is_arch(session, target_name):
        session.send(f"You can't {verb} the {theme.owner_rank}.")
        return False
    mine = theme.index_or_floor(_caller_rank(session))
    theirs = theme.index_or_floor(target_rank)
    if theirs >= mine:
        session.send(f"You can only {verb} someone below your own rank "
                     f"— {target_name}'s rank is {theme.rank(target_rank)}.")
        return False
    return True


def _caller_rank(session) -> str:
    """The acting session's rank name, floor if it somehow has none."""
    res = getattr(session, "resident", None)
    return (getattr(res, "rank", "") or session.theme.resident_rank)


def cmd_ban(session, args: str) -> None:
    """Ban an ACCOUNT. Name only since v1.32.0 — see `.ban_ip` for addresses.

    It used to take either, guessing from the shape of the argument. That put
    two quite different powers behind one word: banning a person, and banning an
    address that may hold a household. Splitting them means the blunter one has
    to be asked for by name, shows its collateral first, and can carry its own
    default duration.
    """
    parts = args.split()
    if not parts:
        session.send("Usage: .ban <name> [hours]")
        return
    subject = parts[0]
    if _looks_like_ip(subject):
        session.send("That looks like an address. Use .ban_ip <name> to ban "
                     "someone's address, or .ban <name> to ban the account.")
        return
    hours = _ban_hours(session, parts, session.config.ban_default_hours)
    if hours is None:
        return

    display, _ = session.accounts.resolve(subject)
    subject = display or subject
    # The arch may not be banned — the same lockout-proofing as .deleteme and
    # .promote; an arch shut out of their own house has no way back in — and
    # since v1.42.0 neither may a peer or a superior. `_may_act_on` is both
    # checks in one, and its arch line is word for word the one this used to
    # send.
    if not _may_act_on(session, subject, _stored_rank(session, subject), "ban"):
        return

    session.bans.ban(subject, "name", hours, now=session.now_wall())
    _log_action(session, f"ban[name,{hours:g}h]", subject)

    # Drop them if they are connected right now.
    t = session.world.get_resident(subject)
    if t is not None and t.session is not None:
        t.session.kick("(banned)")
    session.send(f"Banned {subject} for {hours:g}h.")


def cmd_ban_ip(session, args: str):
    """Ban the ADDRESS a named account is on. Arch by default (it exposes an IP).

    You name a PERSON, not an address, and the engine finds the address — from
    their live session if they are connected, else from their most recent
    `login` event inside the retention window. There is no durable `last_ip` on
    an account, deliberately: a permanent account↔address link is the thing the
    covenant declines, so the tie lives in the event log and fades with it. If
    they are offline and have not been seen in the window, the engine says so
    and offers the account ban instead rather than reaching for a stored value.

    Because an address can hold a household, a shared flat, a campus or a
    carrier's NAT, it shows the collateral — how many accounts have been seen on
    that address, and which of them are on it right now — and asks before
    acting.
    """
    parts = args.split()
    if not parts:
        session.send("Usage: .ban_ip <name> [hours]")
        return
    name = parts[0]
    hours = _ban_hours(session, parts, session.config.ban_ip_default_hours)
    if hours is None:
        return

    display, ambiguous = session.accounts.resolve(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return
    target = display or name
    if session.accounts.get(target) is None:
        session.send(f"There's no one called {name}.")
        return
    if _is_arch(session, target):
        session.send(f"You can't ban the {session.theme.owner_rank}'s address.")
        return
    if not _may_act_on(session, target, _stored_rank(session, target),
                       "ban the address of"):
        return

    ip = _resolve_target_ip(session, target)
    if ip is None:
        session.send(f"{target} isn't connected and hasn't been seen recently, "
                     f"so there's no address to ban. You can still ban the "
                     f"account: .ban {target}")
        return

    # ⛔ THE STAFF-COLLISION GUARD (v1.42.0). Naming a resident who happens to
    # share an address with a staff member is the INDIRECT route to taking staff
    # access down, and the target-rank rule above cannot see it — the named
    # account really is below the caller. So the whole affected set is checked,
    # and a non-arch caller is refused ANONYMOUSLY: no name, no count, nothing
    # that says which account. That silence is the point. A named refusal would
    # turn `.ban_ip` into an oracle for "who shares an address with whom", which
    # is precisely the lookup the covenant declines to offer — and it holds even
    # when the staff account caught is the caller's OWN, so nobody can confirm
    # their own co-tenancy by probing either.
    if _ban_ip_hits_staff(session, ip, target) and not _caller_is_arch(session):
        session.send("This ban would affect staff, so it can't be placed from "
                     "here. Ask the "
                     f"{session.theme.owner_rank} if it needs doing.")
        return

    # Hand the pending ban to the session loop, which owns the y/n.
    session.pending_ban_ip = (target, ip, hours)
    return BAN_IP


def cmd_unban_ip(session, args: str) -> None:
    """Lift the IP ban tied to a name. By name; the address is never shown.

    The mirror of `.ban_ip`: you name the person you banned, and the engine
    resolves the same address and lifts it. `.unban <name>` still handles
    ACCOUNT bans — two commands, because the two bans are two things.
    """
    name = args.strip().split(None, 1)[0] if args.strip() else ""
    if not name:
        session.send("Usage: .unban_ip <name>")
        return
    display, ambiguous = session.accounts.resolve(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return
    target = display or name
    # Lifting is gated like placing: nobody may undo a ban that somebody above
    # placed on a peer, for the same reason they may not `.ungag` one.
    if not _may_act_on(session, target, _stored_rank(session, target),
                       "unban the address of"):
        return

    ip = _resolve_target_ip(session, target)
    if ip is None:
        session.send(f"No address on record for {target}, so there's no IP ban "
                     f"of theirs to lift.")
        return
    if not session.bans.is_banned_ip(ip, now=session.now_wall()):
        session.send(f"There's no active IP ban on {target}'s address.")
        return
    session.bans.unban(ip, "ip")
    hist = getattr(session, "ban_history", None)
    if hist is not None:
        hist.mark_lifted(ip)            # the history keeps the record, stamped
    _log_action(session, "unban_ip", target)
    session.send(f"Lifted the IP ban on {target}'s address.")


def cmd_bans(session, args: str) -> None:
    """The active bans, by NAME. Never an address.

    An IP ban is labelled by the account `.ban_ip` was run on — which is how it
    was placed, so it is how it reads back. That the label is a name rather than
    an address is not a display choice: it is the same rule everywhere, and the
    reason `ban_history` records the originating account alongside.
    """
    now = session.now_wall()
    hist = getattr(session, "ban_history", None)
    rows = []
    for b in session.bans.list_active(now=now):
        if b["kind"] == "name":
            rows.append((b["subject"], "account", b))
        else:
            # Recover the name it was placed under from the history; an IP ban
            # with no history entry (hand-edited bans.json, or one placed before
            # v1.33.0) is shown as an unnamed address rather than AS one.
            label = "(unnamed address)"
            if hist is not None:
                prior = [e for e in hist.for_ip(b["subject"])
                         if e.get("lifted_at") is None]
                if prior:
                    label = prior[-1].get("account") or label
            rows.append((label, "address", b))

    if not rows:
        session.send("No active bans.")
        return

    def when(b):
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(b["until"]))

    def by_and_why(b):
        """Who placed it and why. For an IP ban both live in the HISTORY — the
        ban record itself carries only the address and the expiry — so this is
        the one place the two stores are read together."""
        if b["kind"] == "ip" and hist is not None:
            prior = [e for e in hist.for_ip(b["subject"])
                     if e.get("lifted_at") is None]
            if prior:
                return prior[-1].get("actor", ""), prior[-1].get("reason", "")
        return "", b.get("reason", "")

    rows.sort(key=lambda r: (r[1], r[0].casefold()))
    width = session.width
    if width <= NARROW_COLUMNS:
        lines = []
        for label, kind, b in rows:
            if lines:
                lines.append("")
            lines.extend(wrap_at(f"{label} ({kind})", width))
            lines.append(f"{NARROW_INDENT}until {when(b)}")
            tail = ", ".join(x for x in by_and_why(b) if x)
            if tail:
                lines.extend(wrap_at(tail, width - len(NARROW_INDENT),
                                     NARROW_INDENT))
        session.send_raw(framed("Active bans", "\n".join(lines),
                                f"{len(rows)} ban(s)", width=width))
        return

    w_label = max([len(r[0]) for r in rows] + [len("Name")])
    w_kind = max([len(r[1]) for r in rows] + [len("Kind")])
    lines = [f"  {'Name':<{w_label}}  {'Kind':<{w_kind}}  "
             f"{'Until':<16}  By / reason".rstrip()]
    for label, kind, b in rows:
        tail = ", ".join(x for x in by_and_why(b) if x)
        lines.append(f"  {label:<{w_label}}  {kind:<{w_kind}}  "
                     f"{when(b):<16}  {tail}".rstrip())
    session.send_raw(framed("Active bans", "\n".join(lines),
                            f"{len(rows)} ban(s)", width=width))


def _resolve_target_ip(session, name: str):
    """The address to ban: their live session's, else their last `login`.

    Live first because it is the address they are on NOW, which is what an
    operator dealing with trouble means. The event log is the fallback for
    someone who has just left, and it fades on the ordinary window — so this
    returns None rather than guessing once they have been gone long enough.
    """
    res = session.world.get_resident(name)
    if res is not None and res.session is not None:
        return res.session.peer_ip
    events = getattr(session, "events", None)
    return events.last_login_ip(name) if events is not None else None


def ban_ip_collateral(session, ip: str):
    """(online, offline) account names for an address — what this ban catches.

    Split in two because the two groups suffer differently, and an operator
    deciding whether to press y should see which is which:

    * **online** — connected on that address right now. They are kicked the
      moment the ban lands.
    * **offline** — seen logging in from it inside the retention window, but not
      here now. Nothing happens to them today; they are simply shut out when
      they next try.

    The ARCH is excluded from both. They are exempt from enforcement, so
    counting them would overstate the damage and naming them would be noise.

    ⚠ This is the one moment the house tells a staff member that these accounts
    share an address, and it tells them at BAN TIME only — it is a consequence
    of the act being considered, not a lookup anyone can browse to. No address
    is printed here or anywhere else.
    """
    events = getattr(session, "events", None)
    seen = list(events.accounts_on_ip(ip)) if events is not None else []
    online = [r.name for r in session.world.all_connected()
              if r.session is not None and r.session.peer_ip == ip
              and not _is_arch(session, r.name)]
    online_keys = {n.casefold() for n in online}
    offline = [s for s in seen
               if s.casefold() not in online_keys and not _is_arch(session, s)]
    return (sorted(online, key=str.casefold), sorted(offline, key=str.casefold))


def ban_ip_collateral_lines(session, ip: str, window_days: float):
    """The collateral block, ready to print. Never contains an address."""
    online, offline = ban_ip_collateral(session, ip)
    window = f" (last {window_days:g} days)" if window_days > 0 else ""
    head = (f"This address is in use by {len(online)} online and "
            f"{len(offline)} offline account(s){window}:")
    lines = [head]
    if online:
        lines.append(f"  online:  {', '.join(online)}")
    if offline:
        lines.append(f"  offline: {', '.join(offline)}")
    if not online and not offline:
        lines.append("  (no other accounts seen on it)")
    return lines


def ban_ip_staff_caught(session, ip: str, target: str) -> list[str]:
    """The STAFF accounts an IP ban would catch — the named one included.

    "Affected" is the named account plus everyone else seen on that address:
    `ban_ip_collateral` already computes the second half, and the named account
    is added because a ban placed on someone offline and unseen-on-this-address
    still lands on them.

    Staff means at or above the configured `staff` anchor, read from the STORED
    rank — the arch is excluded by `ban_ip_collateral` and by the caller's own
    guard, and is never IP-refused at login anyway, so they are never among the
    affected and can never trigger the notice.
    """
    online, offline = ban_ip_collateral(session, ip)
    seen: dict[str, str] = {}
    for name in list(online) + list(offline) + [target]:
        if name and not _is_arch(session, name):
            seen.setdefault(name.casefold(), name)
    theme = session.theme
    return sorted((n for n in seen.values()
                   if theme.is_staff_rank(_stored_rank(session, n))),
                  key=str.casefold)


def _ban_ip_hits_staff(session, ip: str, target: str) -> bool:
    return bool(ban_ip_staff_caught(session, ip, target))


def _caller_is_arch(session) -> bool:
    res = getattr(session, "resident", None)
    return _is_arch(session, getattr(res, "name", "") or "")


def _same_person(session, name: str) -> bool:
    """Is this the caller? Casefold, like every identity comparison here."""
    res = getattr(session, "resident", None)
    mine = getattr(res, "name", "") or ""
    return bool(mine) and mine.casefold() == (name or "").casefold()


def ban_ip_recidivism(session, ip: str):
    """A warning line if this address has been banned before, or None.

    ⚠ Matched on the ADDRESS, which is the whole reason the history keeps one:
    it catches the same person returning under an account nobody has seen
    before, which a name-keyed history could not. The address itself is not in
    the line — only the count, the date, who banned, and why.
    """
    hist = getattr(session, "ban_history", None)
    if hist is None:
        return None
    prior = hist.for_ip(ip)
    if not prior:
        return None
    last = prior[-1]
    when = time.strftime("%Y-%m-%d", time.localtime(float(last.get("set_at", 0))))
    bits = [f"last: {when}", f"by {last.get('actor', '?')}"]
    if last.get("reason"):
        bits.append(str(last["reason"]))
    n = len(prior)
    return (f"Heads up: this address has been banned {n} time(s) before "
            f"({', '.join(bits)}).")


def apply_ip_ban(session, target: str, ip: str, hours: float,
                 reason: str = "") -> None:
    """Place the ban, note the history, and drop everyone it catches.

    Called only after an explicit yes. THREE things happen together and a
    cancelled attempt does none of them: the enforcement record (`bans.json`),
    the long-lived history entry, and the kick.
    """
    session.bans.ban(ip, "ip", hours, now=session.now_wall())
    hist = getattr(session, "ban_history", None)
    if hist is not None:
        hist.record(ip, target, session.resident.name, hours, reason)
    for r in list(session.world.all_connected()):
        if (r.session is not None and r.session.peer_ip == ip
                and not _is_arch(session, r.name)
                and r is not session.resident):
            r.session.kick("(banned)")
    session.send(f"Banned the address {target} is on, for {hours:g}h.")


def cmd_unban(session, args: str) -> None:
    subject = args.strip().split(None, 1)[0] if args.strip() else ""
    if not subject:
        session.send("Usage: .unban <name|ip>")
        return
    kind = "ip" if _looks_like_ip(subject) else "name"
    if kind == "name":
        display, _ = session.accounts.resolve(subject)
        subject = display or subject
        # ⚠ The rank check applies to the NAME form only. The literal-address
        # form has no person in it to rank, and no way to acquire one: an
        # address is never shown by any command, so reaching this branch means
        # the caller already had the address from outside the house. `.unban_ip
        # <name>` is the by-name path and IS gated.
        if not _may_act_on(session, subject, _stored_rank(session, subject),
                           "unban"):
            return
    ok = session.bans.unban(subject, kind)
    _log_action(session, "unban", subject)
    session.send(f"Unbanned {subject}." if ok else f"{subject} wasn't banned.")


def cmd_promote(session, args: str) -> None:
    _step_rank(session, args, +1)


def cmd_demote(session, args: str) -> None:
    _step_rank(session, args, -1)


def _step_rank(session, args: str, step: int) -> None:
    """Move someone ONE rung along the ladder (v1.27.0).

    Was a two-value toggle hardcoded to two ranks; it is a single step now,
    so a ladder of any length works. The bounds are the point:

      * UP is capped at `grantable_rank` — the rung just below arch. Arch is
        granted in config, never by a command, so `.promote` can never reach it.
      * UP is ALSO capped at the CALLER's own rank (v1.41.0): **you cannot lift
        anyone to a level higher than yourself**, and that includes yourself.
      * DOWN is floored at `order[0]` — the bottom of the ladder, which since
        v1.41.0 IS the newb rank (it is derived, not configured). Nothing can
        sit below it, which is what makes a below-newb orphan impossible.

    ⚠ The caller cap is the security fix. Before it, `_step_rank` checked that
    the target existed and was not the config arch, and NOTHING else — so a
    house that assigned `.promote` to its staff (which v1.38.0 exists to make
    possible) handed every staff account a one-call route to the rung below the
    owner, on
    themselves. The cap is on the RESULT rather than on the target, because
    that is the thing that must not exceed you: demoting somebody below you is
    ordinary, and promoting a junior up to your own level is legitimate.

    On the shipped three-rank ladder this is exactly the old behaviour: the arch
    is the only holder, and the arch's own rank is the top, so the new cap binds
    nothing that `grantable_rank` did not already bind.
    """
    name = args.strip()
    if not name:
        session.send("Whom?")
        return
    display, ambiguous = session.accounts.resolve(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return
    acct = session.accounts.get(display) if display else None
    if acct is None:
        session.send(f"There's no one called {name}.")
        return
    theme = session.theme
    if acct.name.casefold() == session.config.owner_name.casefold():
        session.send(f"The {theme.owner_rank}'s rank is fixed in config.")
        return
    # ⛔ THE TARGET, not just the result (v1.42.0). The v1.41.0 cap below asks
    # what rank would COME OUT; it never asked whether this was a person you
    # were entitled to touch. For `.demote` that is the whole of it — demoting
    # is always downward, so no cap on the result could ever have stopped a
    # staff account demoting a peer.
    #
    # ⚠ Checked FIRST, which makes the result cap unreachable through `.promote`
    # — a target strictly below you sits at most one rung under you, so one step
    # lands at most on your own level. The cap is kept as the second lock: if
    # this rule were ever loosened to at-or-below, it is what would still stop a
    # rank appearing above the person who granted it.
    if not _may_act_on(session, acct.name, acct.rank,
                       "promote" if step > 0 else "demote"):
        return

    here = theme.index_or_floor(acct.rank)
    ceiling = theme.rank_index(theme.grantable_rank)
    target = min(max(here + step, 0), ceiling)
    # ⛔ The RESULT may never exceed the caller's own rank. Checked before the
    # "already at the edge" message below, so somebody refused for this reason
    # is told THAT reason rather than a misleading one about the ladder's top.
    mine = theme.index_or_floor(_caller_rank(session))
    if step > 0 and target > mine:
        session.send(f"You can't promote anyone above your own rank — "
                     f"yours is {theme.rank(_caller_rank(session))}.")
        return
    if target == here:
        edge = "highest" if step > 0 else "lowest"
        session.send(f"{acct.name}'s rank is already {theme.rank(acct.rank)} — "
                     f"the {edge} you can set.")
        return
    new_rank = theme.order[target]

    session.accounts.set_rank(acct.name, new_rank)
    live = session.world.get_resident(acct.name)
    if live is not None:
        live.set_rank(new_rank, theme)
    _log_action(session, "promote" if step > 0 else "demote", acct.name)
    # ⚠ "is now a {rank}" put an article in front of an operator's noun, and the
    # shipped ladder's middle rung is `staff` — a mass noun. Rank names are the
    # operator's to choose and the engine does not conjugate them, so the rank
    # goes where any noun reads: after "is".
    session.send(f"{acct.name}'s rank is now {new_rank}.")
    if live is not None and live.session is not None:
        live.session.send(f"Your rank is now {new_rank}." if step > 0
                          else f"Your rank is {new_rank} again.")


def cmd_mailpurge(session, args: str):
    """Delete an entire mailbox for spam cleanup. Never reads it.

    ⛔ Since v1.41.0 this asks for the CALLER's OWN password, like `.deleteuser`
    and `.resetpassword`. It destroys someone's correspondence irreversibly —
    the one thing the covenant lets the server keep — and it was the only such
    command a single mistyped line could complete. It also takes the target-rank
    rule: strictly below the caller, never the arch.

    ⚠ A required password is ALWAYS the CALLER's own, never the target's and
    never "the arch's". That is the model across all three: it stays correct
    when a house reassigns the command, because the person being asked is always
    the person acting.

    The handler only validates and stages; the prompt is async, so the work is
    in `server._mailpurge`.
    """
    target = _resolve_account(session, args, "mailpurge")
    if target is None:
        return None
    if not _may_act_on(session, target, _stored_rank(session, target),
                       "purge the mail of"):
        return None
    session.pending_target = target
    return MAILPURGE


def cmd_accounts(session, args: str) -> None:
    """Owner-only: a plain roster of every account — name, RANK, created date —
    on a fixed grid, oldest first. A '*' marks those currently online (presence
    is already public via .who, so it reveals nothing new).

    ⚠ The column is headed `Rank` and shows a rank (v1.61.2). It said `Level`
    and showed a rank, which is the wrong one of the two words the engine
    reasons in: a LEVEL is one of four fixed bands, a RANK is a rung the
    operator named, and this roster shows the rung.

    Deliberately holds NO mailbox information of any kind — no counts, no sizes —
    and no profile/description: this is a roster, not a dossier. Do not add it.
    """
    accounts = session.accounts.all()
    online = {r.name.casefold() for r in session.world.all_connected()}
    owner_key = session.config.owner_name.casefold()

    def name_cell(a) -> str:
        return a.name + ("*" if a.name.casefold() in online else "")

    def rank_cell(a) -> str:
        # The OWNER is config-driven, not stored (its stored rank is usually the
        # bottom rung); show the EFFECTIVE rank, so the roster reads in the
        # house's own vocabulary — the stored value is unchanged.
        theme = session.theme
        rank = theme.owner_rank if a.name.casefold() == owner_key else a.rank
        return theme.rank(rank)

    # Oldest first by created date. A missing date sorts first with a blank cell
    # (the key's leading flag is False for "", so those come before any real date);
    # the casefolded name breaks ties for a stable order.
    accounts.sort(key=lambda a: (bool(a.created), a.created, a.name.casefold()))

    headers = ("Name", "Rank", "Created")
    w_name = max([len(name_cell(a)) for a in accounts] + [len(headers[0])])
    w_rank = max([len(rank_cell(a)) for a in accounts] + [len(headers[1])])

    def row(name: str, rank: str, created: str) -> str:
        return f"  {name:<{w_name}}  {rank:<{w_rank}}  {created}".rstrip()

    n = len(accounts)
    footer = f"{n} account{'' if n == 1 else 's'}"
    width = session.width
    if width <= NARROW_COLUMNS:
        # Three columns will not fit, and the name is the column that must never
        # be truncated — it is what every other staff command takes as its
        # argument. Name on its own line, rank and date beneath it.
        lines = []
        for a in accounts:
            if lines:
                lines.append("")
            lines.extend(wrap_at(name_cell(a), width))
            meta = ", ".join(x for x in (rank_cell(a), a.created) if x)
            if meta:
                lines.append(f"{NARROW_INDENT}{meta}")
        session.send_raw(framed("Accounts", "\n".join(lines), footer, width=width))
        return

    lines = [row(*headers)]
    for a in accounts:
        lines.append(row(name_cell(a), rank_cell(a), a.created))
    session.send_raw(framed("Accounts", "\n".join(lines), footer, width=width))


STAFF_ACTIONS_DEFAULT = 20      # a screenful; .staff_actions <n> asks for more
STAFF_ACTIONS_MAX = 200         # bounds one screen, not the store


def _event_when(session, ts: float) -> str:
    """One timestamp, rendered for a person rather than for a parser.

    Local to the SERVER, because that is the only clock the engine has and a
    person reading a moderation log is nearly always standing in the same
    timezone as the house. Date and minute; seconds would be noise on a screen
    whose whole job is "roughly when, and in what order".
    """
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cmd_staff_actions(session, args: str) -> None:
    """Arch-only: the recent staff actions, newest first.

    The FIRST view over the event store, and deliberately a thin one — it is
    `events.read(types=("staff_action",), newest_first=True)` and a layout.
    Every future view (connections, auth failures, an admin panel) is the same
    reader with a different filter, which is the reason the store exists at all
    rather than a second logfile beside journald.

    Shows what accountability actually needs: when, who did it, what they did,
    and to whom. Never why — no reason, no message content, because none is
    recorded. Bounded by the store's own retention window, so this screen fades
    on the same clock as everything else the engine remembers.
    """
    events = getattr(session, "events", None)
    limit = STAFF_ACTIONS_DEFAULT
    arg = args.strip()
    if arg:
        # ⛔ Same two guards as `.dmail`/`.dstaffmail` (v1.52.3, audit C3), and
        # this is the command that had NEITHER: `isascii()` because
        # `str.isdigit()` accepts `²`/`①` where `int()` raises, and the length
        # bound because `int()` also raises past CPython's 4300-digit limit —
        # which the range check below could not reach, since it runs int() to
        # find out. Latent rather than live (MAX_LINE_LENGTH caps the wire at
        # ~1009 digits), and fixed anyway: the reason it was unreachable was a
        # constant in another file.
        if not (arg.isascii() and arg.isdigit()) or len(arg) > 9 \
                or not (1 <= int(arg) <= STAFF_ACTIONS_MAX):
            session.send(f"Usage: .staff_actions [n]   (1-{STAFF_ACTIONS_MAX})")
            return
        limit = int(arg)

    rows = events.read(types=("staff_action",), newest_first=True,
                       limit=limit) if events is not None else []
    if not rows:
        session.send("No staff actions recorded.")
        return

    width = session.width
    days = getattr(session.config, "event_retention_days", 0)
    footer = f"{len(rows)} action{'' if len(rows) == 1 else 's'}"
    if days > 0:
        footer += f", last {days:g} day{'' if days == 1 else 's'}"

    def cells(e):
        return (_event_when(session, e.get("ts", 0.0)),
                str(e.get("actor", "?")),
                str(e.get("action", "?")),
                str(e.get("target", "")),
                str(e.get("outcome", "")))

    grid = [cells(e) for e in rows]
    # `outcome` is carried by `.ban_ip` alone (see `_log_action`), so on an
    # ordinary screen there is nothing to show and the column does not appear.
    # ⚠ It is not decoration: a cancelled `.ban_ip` and a confirmed one are the
    # same actor, action and target, so without this the log reads as though
    # every attempt landed. The word is what makes the row true.
    any_outcome = any(c[4] for c in grid)

    if width <= NARROW_COLUMNS:
        # Four columns cannot fit at 40. The action and its target are the thing
        # being read, so they lead; the time is the detail beneath — the reverse
        # of the wide layout, because a narrow screen is scanned top-down.
        lines = []
        for when, actor, action, target, outcome in grid:
            if lines:
                lines.append("")
            lines.extend(wrap_at(f"{actor} {action}"
                                 + (f" -> {target}" if target else ""), width))
            # ⚠ The outcome sits on the DETAIL line, beside the time, and not on
            # the lead line where it reads more naturally. `ban_ip[24h]` is a
            # long word: at 40 columns the lead line is already near full for
            # exactly the rows that carry an outcome, so appending there wrapped
            # a bare `(confirmed)` down to column zero — indistinguishable from
            # the next entry, which is the one thing this must never be. Here it
            # is indented under its own lead line, in the same place in every
            # block, and cannot wrap.
            lines.append(f"{NARROW_INDENT}{when}"
                         + (f"  {outcome}" if outcome else ""))
        session.send_raw(framed("Staff actions", "\n".join(lines), footer,
                                width=width))
        return

    w_when = max(len(c[0]) for c in grid)
    w_actor = max([len(c[1]) for c in grid] + [len("Who")])
    w_action = max([len(c[2]) for c in grid] + [len("Did")])
    w_target = max([len(c[3]) for c in grid] + [len("To")])
    header = f"  {'When':<{w_when}}  {'Who':<{w_actor}}  {'Did':<{w_action}}  "
    if any_outcome:
        # A fifth column, added only when something fills it. Every other row
        # keeps an empty cell, and the trailing rstrip below leaves it byte
        # for byte the line it was before — so a log with no `.ban_ip` in it
        # renders exactly as it always has.
        header += f"{'To':<{w_target}}  Outcome"
    else:
        header += "To"
    lines = [header.rstrip()]
    for when, actor, action, target, outcome in grid:
        line = (f"  {when:<{w_when}}  {actor:<{w_actor}}  "
                f"{action:<{w_action}}  ")
        line += f"{target:<{w_target}}  {outcome}" if any_outcome else target
        lines.append(line.rstrip())
    session.send_raw(framed("Staff actions", "\n".join(lines), footer, width=width))


# ---------------------------------------------------------------------------
# The counting views (v1.35.0) — `.security` and `.staff_calls`
#
# Both are what `.staff_actions` promised the store would make cheap: a filter
# over `events.read()` and a layout, with no new file, no new writer and no new
# idea of what "recent" means. Where they differ from `.staff_actions` is that
# they COUNT rather than list, and that is the whole of their privacy design:
#
#   `.security` shows six numbers and NOTHING else — no name, no address, no
#   timestamp. "Fourteen failed logins yesterday" is the entire question an
#   owner's glance is asking; who and from where is an investigation, and an
#   investigation belongs in the log where the operator has to go and look.
#
#   `.staff_calls` names residents, because a tally with the names taken out
#   cannot answer the question it exists for — which is "who is this?" when the
#   same person has called eleven times since breakfast. It names nothing else:
#   not what they said (never recorded), not when, and not where from.
#
# ⛔ NEITHER PRINTS AN ADDRESS, and neither reads a field that holds one. That
# is the v1.33.0 rule, and here it costs nothing at all: none of the counted
# types except `auth_fail` even carries an `ip`, and `auth_fail` is counted, not
# read.
# ---------------------------------------------------------------------------

# `.security`, row by row: (event type, field match or None, wide label, narrow
# label). A table rather than a pile of branches, because the screen IS the
# table — adding a row should be adding a row.
#
# ⚠ The match is a (FIELD, value) pair since v1.53.0. It used to be a bare word
# compared against `event`, which was fine while `account_security` was the only
# type that split into rows; `auth_ceiling` splits on `phase`, so the column now
# says which field it means rather than assuming.
#
# ⛔ `reconsent` is deliberately absent. It is an `account_security` event like
# the two credential ones, but it fires for EVERYONE on any agreement change, so
# a single edit to `agreement.txt` would put the house's whole population into a
# security screen and bury the two rows that matter. It is not a security signal;
# it is a rollout statistic wearing the same type's name (see events.py on why
# the type is a slight stretch).
SECURITY_ROWS = (
    ("auth_fail", None,
     "failed login attempts", "failed logins"),
    ("login_timeout", None,
     "login timeouts", "login timeouts"),
    # v1.53.0 — the server-wide door posture. ⚠ TRIPS ONLY: the matching
    # `cleared` is the recovery, not a signal, and counting both would double
    # every incident on a screen whose whole job is "is anything happening?".
    # It sits under the failure rows because that is what causes it.
    ("auth_ceiling", ("phase", "tripped"),
     "auth ceiling trips", "ceiling trips"),
    ("account_security", ("event", "password_reset_recovery"),
     "password resets (recovery code)", "password resets"),
    ("account_security", ("event", "recovery_codes_regenerated"),
     "recovery codes regenerated", "codes regenerated"),
    ("takeover", None,
     "session takeovers", "takeovers"),
    ("account_deleted", None,
     "accounts self-deleted", "self-deletions"),
    # v1.52.0 — frames the doors judged hostile and hung up on. ⚠ A COUNT, like
    # every other row: the sample and the reason live in the event, and reading
    # them is a deliberate act. This line exists so "somebody is probing" is
    # visible at a glance without anyone having to go looking.
    ("bad_frame", None,
     "rejected frames", "rejected frames"),
)

# The footer is the screen's other half: the counts say whether to look, and
# this says where. Two lengths, because a rule that has to back off to a word
# boundary at 40 would drop "investigate" and leave the pointer pointing nowhere.
SECURITY_FOOTER = "counts only — see the server log to investigate"
SECURITY_FOOTER_NARROW = "counts only — see the server log"


def _in_window(entry, cutoff) -> bool:
    return cutoff is None or float(entry.get("ts", 0.0)) >= cutoff


def cmd_security(session, args: str) -> None:
    """Arch-only: a security glance — counts over one window, and nothing else.

    Eight numbers over `security_window_hours` (24 by default, capped at the
    store's retention). No names, no addresses, no times: this screen answers
    "is anything happening?", and the footer says where to go when the answer is
    yes. A view that named the accounts would be a different screen with a
    different covenant question attached, and this is not it.
    """
    events = getattr(session, "events", None)
    cutoff, hours = window_cutoff(session, "security_window_hours")

    # One read for the whole screen: the rows share a window, so they may as
    # well share a pass over the file.
    wanted = {t for t, _e, _w, _n in SECURITY_ROWS}
    rows = events.read(types=tuple(wanted)) if events is not None else []
    rows = [e for e in rows if _in_window(e, cutoff)]

    counts = []
    for etype, match, wide, narrow in SECURITY_ROWS:
        n = sum(1 for e in rows
                if e.get("type") == etype
                and (match is None or e.get(match[0]) == match[1]))
        counts.append((wide, narrow, n))

    width = session.width
    narrow = width <= NARROW_COLUMNS
    title = f"Security — {window_phrase(hours)}"
    w_count = max(len(str(n)) for _w, _n, n in counts)
    # The narrow labels are hand-set shorter forms, not a truncation: "password
    # resets (recovery code)" cut to fit would read as a different row.
    picked = [(n if narrow else w, c) for w, n, c in counts]
    label_w = max(len(label) for label, _c in picked)
    lines = [f"  {label:<{label_w}}  {n:>{w_count}}" for label, n in picked]
    footer = SECURITY_FOOTER_NARROW if narrow else SECURITY_FOOTER
    session.send_raw(framed(title, "\n".join(lines), footer, width=width))


def cmd_staff_calls(session, args: str) -> None:
    """Staff: who has been contacting staff, and by which door.

    A per-resident tally of `.call_staff` and `.mail_staff` over
    `staff_calls_window_hours` (24 by default, capped at retention), busiest
    first — so the person who has called eleven times since breakfast is the
    first line on the screen rather than something you notice on the third read.

    The two columns are the two doors, kept apart because they mean different
    things: a call interrupts everyone at staff level who is awake, a note waits
    in the
    letterbox. Neither column holds a word of what was said — `.call_staff`
    records the caller and never the call, and the note is mail, read by the
    people it was addressed to.
    """
    events = getattr(session, "events", None)
    cutoff, hours = window_cutoff(session, "staff_calls_window_hours")
    rows = events.read(types=("staff_call", "staff_mail")) if events is not None else []

    # Casefold to key, first-seen display name to show: two spellings of one
    # person are one person, and `.who` and `.examine` have always agreed on that.
    tally: dict[str, list] = {}
    for e in rows:
        if not _in_window(e, cutoff):
            continue
        name = str(e.get("account", ""))
        if not name:
            continue
        slot = tally.setdefault(name.casefold(), [name, 0, 0])
        if e.get("type") == "staff_call":
            slot[1] += 1
        else:
            slot[2] += 1

    width = session.width
    narrow = width <= NARROW_COLUMNS
    # "& mail" is what the rule drops at 40 anyway, and a title backed off to
    # "Staff calls & mail — last 24" would lose the window instead. Shorter
    # title, whole meaning.
    title = (f"Staff calls — {window_phrase(hours)}" if narrow
             else f"Staff calls & mail — {window_phrase(hours)}")
    if not tally:
        session.send(f"No staff calls or mail — {window_phrase(hours)}.")
        return

    # Busiest first; ties broken by name so two equal rows do not swap places
    # between one reading and the next.
    people = sorted(tally.values(), key=lambda r: (-(r[1] + r[2]), r[0].casefold()))
    total = sum(r[1] + r[2] for r in people)
    footer = (f"{len(people)} resident{'' if len(people) == 1 else 's'}, "
              f"{total} contact{'' if total == 1 else 's'}")

    if narrow:
        # Three columns will not fit at 40 with a name in the first, so the
        # tally becomes a phrase under the name — the same shape `.who` takes
        # when it narrows.
        lines = []
        for name, called, mailed in people:
            if lines:
                lines.append("")
            lines.extend(wrap_at(name, width))
            lines.append(f"{NARROW_INDENT}called {called}, mailed {mailed}")
        session.send_raw(framed(title, "\n".join(lines), footer, width=width))
        return

    w_who = max([len(r[0]) for r in people] + [len("Who")])
    lines = [f"  {'Who':<{w_who}}  {'Called':>6}  {'Mailed':>6}"]
    for name, called, mailed in people:
        lines.append(f"  {name:<{w_who}}  {called:>6}  {mailed:>6}")
    session.send_raw(framed(title, "\n".join(lines), footer, width=width))


# ---------------------------------------------------------------------------
# `.load_report` (v1.36.0) — what the engine saw when it read the house
#
# Not a view over the event store: the event log holds DOMAIN facts about
# people, and this holds authoring facts about files. Same shape of screen,
# entirely different source — an in-memory buffer filled at boot and never
# written anywhere (see loadreport.py).
#
# It exists because a builder who mistypes a filename should not have to SSH to
# the box to find out. The house already knew; it just had nowhere to say so.
# ---------------------------------------------------------------------------

def _report_block(width: int, label: str, items) -> list[str]:
    """A labelled indented block: `label:` then one wrapped entry beneath each.

    Wrapped here rather than by `send`, for the reason the room roll is: send
    wraps FLAT, so a long warning's second line would fall back to column zero
    and read as a new entry.
    """
    lines = [f"  {label}:"]
    for item in items:
        # wrap_HARD: these lines carry filesystem paths, which are single
        # unbreakable tokens of arbitrary length. See _shared.wrap_hard.
        lines.extend(wrap_hard(item, width, "    "))
    return lines


def cmd_load_report(session, args: str) -> None:
    """Staff: the last load's summary and every warning it produced.

    The counts, the one-way passages, and ALL warnings — a retired header, an
    orphan narrow file, a malformed `[ranks]` block, an unreadable favicon, an
    account whose rank the ladder no longer has. Rebuilt fresh at every boot and
    held only in memory, so what it describes is always the process you are
    talking to.
    """
    report = getattr(session, "load_report", None)
    if report is None:
        session.send("No load report available.")
        return

    width = session.width
    lines = []
    for line in report.summary_lines():
        lines.extend(wrap_at(line, width, "  "))

    if report.one_way:
        # Align the arrows where there is room for it — a column of `->` is the
        # whole reason this reads as a list of passages rather than a paragraph.
        # At 40 the padding is dropped rather than allowed to force a wrap.
        pad = (max(len(a) for a, _b in report.one_way)
               if width > NARROW_COLUMNS else 0)
        if lines:
            lines.append("")
        lines.extend(_report_block(
            width, "one-way passages",
            [f"{a:<{pad}} -> {b}" for a, b in report.one_way]))

    # The last live reload, if there has been one since boot (v1.37.0). One
    # screen, both halves: "what did the engine make of your files" is the same
    # question whether they were read at boot or five minutes ago.
    last = getattr(report, "last_reload", None)
    if last is not None:
        if lines:
            lines.append("")
        lines.append(f"  reloaded {last.stamp()}:")
        for r in last.results:
            if not r.ok:
                lines.extend(wrap_hard(f"{r.source}: REJECTED, kept the old "
                                       f"— {'; '.join(r.errors)}", width, "    "))
                continue
            if not r.changes:
                lines.extend(wrap_hard(f"{r.source}: no change", width, "    "))
            for c in r.changes:
                lines.extend(wrap_hard(f"{r.source}: {c}", width, "    "))
            for n in r.notes:
                lines.extend(wrap_hard(n, width, "      "))

    if lines:
        lines.append("")
    if report.warnings:
        warnings = list(report.warnings)
        if report.dropped:
            # Say when the buffer bit, rather than quietly showing a prefix and
            # letting it read as the whole story.
            warnings.append(f"({report.dropped} more not kept — buffer full)")
        lines.extend(_report_block(width, "warnings", warnings))
    else:
        lines.append("  no warnings.")

    n_ways, n_warn = len(report.one_way), len(report.warnings)
    footer = ", ".join((
        f"{n_ways} one-way passage{'' if n_ways == 1 else 's'}"
        if n_ways else "no one-way passages",
        f"{n_warn} warning{'' if n_warn == 1 else 's'}"
        if n_warn else "no warnings",
    ))
    session.send_raw(framed(f"Last load — {report.stamp()}", "\n".join(lines),
                            footer, width=width))


def cmd_newusers(session, args: str) -> None:
    """Arch-only: open/close account creation."""
    arg = args.strip().lower()
    if not arg:
        state = "on" if session.state.allow_new_accounts else "off"
        session.send(f"Account creation is {state}.")
        return
    if arg not in ("on", "off"):
        session.send("Usage: .newusers [on|off]")
        return
    session.state.set_new_accounts(arg == "on")
    _log_action(session, "newusers", arg)
    session.send(f"Account creation is now {arg}.")


def cmd_logins(session, args: str) -> None:
    """Arch-only: open/close new logons (already-connected residents stay)."""
    arg = args.strip().lower()
    if not arg:
        state = "on" if session.state.allow_logins else "off"
        session.send(f"Logins are {state}.")
        return
    if arg not in ("on", "off"):
        session.send("Usage: .logins [on|off]")
        return
    session.state.set_logins(arg == "on")
    _log_action(session, "logins", arg)
    session.send(f"Logins are now {arg}.")


def cmd_logoutall(session, args: str):
    """Arch-only: disconnect everyone but the arch. The confirmation prompt and
    the disconnects are driven by the async session loop."""
    return LOGOUTALL


# ---------------------------------------------------------------------------
# The two account powers (arch only)
#
# Both are DELIBERATELY long verbs. `.deleteuser` erases someone permanently and
# `.resetpassword` sets a credential they did not choose; neither should be one
# fat-fingered keystroke away from a shorter word. Both take two gates and both
# reuse the machinery `.deleteme` already proved: an echo-off prompt from the
# session loop, `accounts.averify` on the ACTOR's own password, and the same
# delete/purge calls.
#
# The handler's job is only to VALIDATE and name the target; every prompt is
# async, so the work happens in the server's session loop (see _deleteuser /
# _resetpassword there). The resolved target rides on the session rather than in
# the sentinel, so the sentinels stay the plain string constants the loop already
# compares with `==`.
# ---------------------------------------------------------------------------

def _stored_rank(session, name: str) -> str:
    """What an ACCOUNT holds on disk. The account-acting commands work on people
    who may be offline, so the live `Resident.rank` is not available; an account
    that has somehow vanished between resolution and here reads as the floor,
    which is the fail-safe direction (it cannot make anyone harder to reach)."""
    acct = session.accounts.get(name)
    return getattr(acct, "rank", "") if acct is not None else ""


def _resolve_account(session, args: str, verb: str):
    """Shared front half: an existing account named unambiguously, or None (with
    the reason already sent). Uses the same exact-then-prefix resolution as every
    other name-taking command, so `.deleteuser cor` behaves like `.tell cor`."""
    name = args.strip()
    if not name:
        session.send(f"Usage: .{verb} <name>")
        return None
    display, ambiguous = session.accounts.resolve(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return None
    if display is None or session.accounts.get(display) is None:
        session.send(f"There's no account called {name}.")
        return None
    return display


def cmd_deleteuser(session, args: str):
    """Arch-only: erase an account permanently. Two gates, both in the session
    loop — retype the username, then the ARCH's own password."""
    target = _resolve_account(session, args, "deleteuser")
    if target is None:
        return None
    # The config-named arch may not be deleted: freeing that name would hand arch
    # to the next registrant. Since v1.41.0 the same check also refuses a target
    # at or above the caller's rank — see `_may_act_on`.
    if not _may_act_on(session, target, _stored_rank(session, target), "delete"):
        return None
    session.pending_target = target
    return DELETEUSER


def cmd_resetpassword(session, args: str):
    """Set someone's password — the locked-out backstop, for when a resident has
    lost both their password and their recovery codes. Arch by default.

    ⛔ The config-named arch may NOT be reset, exactly as they may not be
    deleted. `.deleteuser` has always refused them; this did not, and that was a
    privilege-escalation hole rather than an oversight in wording: the command is
    RANK-ASSIGNABLE, and the gate is the CALLER's own password. So a staff
    account holding `.resetpassword` could set the OWNER's password using its own
    password, and walk in as the arch. Fixed in v1.33.0.
    """
    target = _resolve_account(session, args, "resetpassword")
    if target is None:
        return None
    if not _may_act_on(session, target, _stored_rank(session, target),
                       "reset the password of"):
        return None
    session.pending_target = target
    return RESETPASSWORD


# Listing order: the world-wide presence trio, the room trio, the jail trio,
# then scope, then the two identity defaults. It reads as the house telling you
# what it says and when.
OWNERSET_KEYS = ("login_msg", "logout_msg", "jail_logon_msg",
                     "enter_msg", "exit_msg", "afk_msg",
                     "jail_msg", "release_msg", "jail_reply_msg",
                     "broadcast_scope", "desc", "profile",
                     "dim", "time", "helpstyle",
                     "raise_prompt_min", "raise_prompt_max")

# ⛔ THE FREE-TEXT KEYS TAKE THE PERSONAL COMMAND'S OWN CAP (v1.52.4, audit W4).
#
# Every key here is the HOUSE DEFAULT for something a resident can also set for
# themselves, and the personal side has always run its text through
# `clean_message` — one line, printable, whitespace collapsed, capped. The house
# side stored `parts[1].strip()` raw, so the phrase broadcast to EVERYONE on
# every login, logout, arrival and departure was the UNBOUNDED one while the
# phrase carried by one person's own comings and goings was capped at 60. The
# line more people see is the one that was not bounded.
#
# ⚠ The cap is per key, matching whichever command it shadows: the nine phrases
# take `.set <key>`'s MSG_MAX, and `desc` takes `.desc`'s DESC_MAX — capping the
# house description at 60 would make it the one description nobody could write.
# (No escape risk in any of this: output is control-stripped at the sink. It is
# bounds and consistency, not injection.)
OWNERSET_TEXT_CAPS = {"desc": DESC_MAX}


def _ownerset_display(key: str, value: str) -> str:
    """One line for the listing. A multi-line value is summarised, never dumped:
    the listing is one line per key, and a pasted profile would wreck it."""
    if key in MULTILINE_KEYS:
        if not value:
            return "(unset)"
        n = len(value.split("\n"))
        return f"<{n} line{'' if n == 1 else 's'}>"
    return value if value else "(empty)"


def cmd_ownerset(session, args: str) -> None:
    """Arch-only: view and tune the house-wide runtime strings — the login/logout
    and room enter/exit wording, the afk and jail messages, the broadcast scope,
    and the house defaults for a resident's own desc/profile. Changes take effect
    immediately and persist across restarts (state dir, not theme/defaults). Bare
    lists every key with its value and built-in default.
    """
    state = getattr(session, "state", None)
    args = args.strip()
    if not args:
        width = session.width
        rows = []
        for k in OWNERSET_KEYS:
            cur = house_setting(session, k)
            shown = _ownerset_display(k, cur)
            tail = ("" if cur == HOUSE_DEFAULTS[k]
                    else f"   (default: {_ownerset_display(k, HOUSE_DEFAULTS[k])})")
            rows.append((k, f"{shown}{tail}"))
        if width <= NARROW_COLUMNS:
            # Every value here is a phrase, and several carry a "(default: …)"
            # tail as well — stacked with a blank between is the only form that
            # keeps "which key am I looking at" legible at 40.
            body = "\n".join(narrow_stacked(rows, width))
        else:
            body = "\n".join(f"{k}: {v}" for k, v in rows)
        session.send_raw(framed(f"{session.theme.owner_rank} settings", body,
                                width=width))
        return

    parts = args.split(None, 1)
    key = parts[0].lower()
    if key not in HOUSE_DEFAULTS:
        session.send(f"Unknown setting {key!r}. Keys: {', '.join(OWNERSET_KEYS)}")
        return

    value = parts[1].strip() if len(parts) > 1 else ""

    # The multi-line key is set through the SAME editor `.profile` itself uses,
    # not as a command argument: a house profile is prose, and prose does not fit
    # on a command line. Bare shows it; `edit` opens the editor; `default` clears.
    if key in MULTILINE_KEYS:
        if not value:
            cur = house_setting(session, key)
            if cur:
                session.send_raw(framed(f"House {key}", wrap_page(cur, session.width),
                                        f"Replace with .ownerset {key} edit",
                                        width=session.width))
            else:
                session.send(f"No house {key} set. "
                             f"Write one with '.ownerset {key} edit'.")
            return
        if value.lower() == "edit":
            def save(sess, text: str) -> None:
                st = getattr(sess, "state", None)
                if st is not None:
                    if text.strip():
                        st.set_house_setting(key, text)
                    else:
                        st.reset_house_setting(key)
                _log_action(sess, "ownerset", key)
                sess.send(f"House {key} saved." if text.strip()
                          else f"House {key} cleared.")
            session.start_editor(key, session.config.profile_max_lines, save)
            return
        if value.lower() != "default":
            session.send(f"Usage: .ownerset {key} [edit|default]   "
                         f"({key} is multi-line — 'edit' opens the editor)")
            return
        # falls through to the shared `default` reset below

    if not value:
        session.send(f"{key}: {house_setting(session, key)}   "
                     f"(default: {_ownerset_display(key, HOUSE_DEFAULTS[key])})")
        return

    if value.lower() == "default":
        if state is not None:
            state.reset_house_setting(key)
        _log_action(session, "ownerset", f"{key}=default")
        if key in RAISE_BOUNDS_KEYS:
            push_raise_bounds_all(session.world)
        session.send(f"{key} reset to its default: "
                     f"{_ownerset_display(key, HOUSE_DEFAULTS[key])}")
        return

    # Validation before it goes live. The message keys are PHRASES (the engine
    # adds the name, the brackets and the asterisks), so no {name} is required —
    # and none is honoured either. The enum keys have a fixed vocabulary, and a
    # house setting that silently accepted "maybe" would be worse than one that
    # refuses it.
    allowed = ENUM_KEYS.get(key)
    if allowed is not None and value.lower() not in allowed:
        session.send(f"{key} must be " + " or ".join(f"'{a}'" for a in allowed) + ".")
        return
    if allowed is not None:
        value = value.lower()
    if key in INT_KEYS:
        try:
            n = int(value)
        except ValueError:
            session.send(f"{key} must be a whole number of lines.")
            return
        if n < 0:
            session.send(f"{key} cannot be negative.")
            return
        # The pair has to stay orderable: a floor above the ceiling leaves no
        # legal value at all, so it is refused rather than silently swapped.
        if key in RAISE_BOUNDS_KEYS:
            other = ("raise_prompt_max" if key == "raise_prompt_min"
                     else "raise_prompt_min")
            try:
                o = int(house_setting(session, other))
            except (TypeError, ValueError):
                o = int(HOUSE_DEFAULTS[other])
            lo, hi = (n, o) if key == "raise_prompt_min" else (o, n)
            if lo > hi:
                session.send(f"raise_prompt_min ({lo}) cannot exceed "
                             f"raise_prompt_max ({hi}).")
                return
        value = str(n)
    elif key not in MULTILINE_KEYS:
        # A PHRASE: the same helper the personal `.set` uses, at this key's own
        # cap. `profile` is excluded because it is prose written through the
        # editor, where a one-line cap would be nonsense — and it cannot reach
        # here anyway, since every multi-line path returns above.
        cleaned = clean_message(value, OWNERSET_TEXT_CAPS.get(key, MSG_MAX))
        if not cleaned:
            # Whitespace and control characters only. Storing that would leave
            # the house saying `[Ada ]` on every login, which is not what
            # anybody typing this meant — and clearing is a word, not an empty
            # line, exactly as it is on the personal side.
            session.send(f"{key} needs some words. "
                         f"Use '.ownerset {key} default' to reset it.")
            return
        value = cleaned

    if state is not None:
        state.set_house_setting(key, value)
    _log_action(session, "ownerset", key)   # a fact: which key, never contents beyond config
    if key in RAISE_BOUNDS_KEYS:
        # House-wide, so it reaches EVERYONE — unlike `.ignore`, which is one
        # person's own setting. A web client that is now out of range re-clamps
        # itself on receipt.
        push_raise_bounds_all(session.world)
    session.send(f"{key} set to: {value}")
