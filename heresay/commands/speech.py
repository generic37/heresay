# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations
from .. import ratelimit
from ._shared import JAILED_OUTBOUND, house_setting, log



THROTTLE_MSG = "You're talking too fast — take a breath."
GAG_MSG = "You've been gagged — no one can hear you. (.call_staff <text> to appeal.)"


# ---------------------------------------------------------------------------
# Governed conversational verbs
# ---------------------------------------------------------------------------

def _governed(session) -> bool:
    """Run the shared speech check. Returns True if the line may be delivered.

    On denial the sender gets appropriate feedback (or, on escalation, is
    disconnected). The room never learns anything happened.
    """
    outcome = session.speech_check()
    if outcome == ratelimit.OK:
        return True
    if outcome == ratelimit.THROTTLED:
        session.send(THROTTLE_MSG)
        return False
    if outcome == ratelimit.MUTED:
        # Server-side cool-off. Tell the sender quietly, drop the line.
        session.send(THROTTLE_MSG)
        return False
    if outcome == ratelimit.DISCONNECT:
        session.request_disconnect("flood")
        return False
    return False


def _gagged(session) -> bool:
    """A gagged resident can't be heard by the room; tell them why (once/line)."""
    if session.resident.gagged:
        session.send(GAG_MSG)
        return True
    return False


def cmd_say(session, text: str) -> None:
    text = text.strip()
    if not text:
        return
    if _gagged(session):
        return
    if not _governed(session):
        return
    res = session.resident
    line = f"{res.name} says: {text}"
    session.world.broadcast_room(res.room_id, res.name, line, dim=False)
    # The entry room's fading memory (a no-op anywhere else). Memory only. Speech
    # here also resets the atmospherics quiet clock.
    now = session.now()
    session.world.record_review(res.room_id, res.name, line, now)
    session.world.note_entry_speech(res.room_id, now)


def cmd_emote(session, text: str) -> None:
    text = text.strip()
    if not text:
        session.send("Emote what?")
        return
    if _gagged(session):
        return
    if not _governed(session):
        return
    res = session.resident
    line = f"{res.name} {text}"
    session.world.broadcast_room(res.room_id, res.name, line, dim=False)
    now = session.now()
    session.world.record_review(res.room_id, res.name, line, now)
    session.world.note_entry_speech(res.room_id, now)


def cmd_think(session, text: str) -> None:
    text = text.strip()
    if not text:
        session.send("Think what?")
        return
    if _gagged(session):
        return
    if not _governed(session):
        return
    res = session.resident
    session.world.broadcast_room(res.room_id, res.name,
                                 f"{res.name} thinks . . oO( {text} )", dim=False)


NO_CORRESPONDENT = "You aren't talking with anyone yet."


def _record_revtell(session, owner: str, line: str) -> None:
    """Hold one private line for its own participant, in the wording they saw.

    Write-through and best-effort: a review buffer is a convenience, and a disk
    problem must never break a tell that has already been delivered. The
    exception is swallowed as a fact — the line itself never reaches a log.
    """
    store = getattr(session, "revtells", None)
    if store is None:
        return
    try:
        store.record(owner, line)
    except OSError:
        log.warning("could not write the review buffer")   # no name, no text


def _deliver_private(session, target, message: str, *, remote: bool) -> bool:
    """Deliver one private message (tell or remote). Returns True on a successful
    send (past gag + rate limit), which is what makes the target 'sticky'."""
    # Jail = isolation. A jailed SENDER can't reach anyone (not even staff — they
    # plead their case in-room to visiting staff, or via .call_staff). A jailed
    # TARGET is reachable only by staff; a staff sender then also sees the
    # jail_reply_msg, so they know the jailed resident can't reply this way.
    if session.resident.jailed:
        session.send(JAILED_OUTBOUND)
        return False
    if target.jailed and not session.resident.is_staff():
        session.send(f"{target.name} is in jail — only the "
                     f"{session.theme.staff_plural} can reach them.")
        return False
    # A gagged person keeps ONE channel: reaching staff, to appeal (and .call_staff,
    # the discoverable path, also works while gagged — see cmd_call_staff).
    if session.resident.gagged and not target.is_staff():
        session.send(f"You've been gagged — only the "
                     f"{session.theme.staff_plural} will hear you now.")
        return False
    if not _governed(session):
        return False
    res = session.resident
    # A tell reads as a say and a remote reads as an emote, because that is what
    # they are — the ">> " marker says "only the two of you". There is no
    # separate "You tell X:" confirmation: seeing your own ">> " line IS the
    # confirmation, precisely as it is when you speak aloud.
    #
    # A TELL is identical on both sides. A REMOTE is not, and deliberately: the
    # recipient reads a third-person action addressed to them, while the sender
    # would otherwise have no sign of WHERE it went — a remote can go to anyone
    # anywhere, and ".r" repeats to a sticky correspondent who may be someone
    # else than you last thought. So the sender's own copy carries a "(to Name)"
    # tag, in the target's display capitalization. The tag counts in the 78-column
    # wrap like any other text.
    recipient_line = (f">> {res.name} {message}" if remote
                      else f">> {res.name} says: {message}")
    sender_line = (f">> (to {target.name}) {res.name} {message}" if remote
                   else recipient_line)
    # EXACTLY ONCE, on every door, for every form. When you message yourself the
    # two deliveries below are the same screen, so only the sender's copy goes:
    # the symmetric line IS the echo, and a second send would print it twice.
    self_directed = target is res or target.session is session
    # A muted sender's messages are filtered out — and a line the recipient
    # never saw is never held for them either.
    if not self_directed and target.session is not None and target.sees(res.name):
        target.session.send(recipient_line, dim=False)
        _record_revtell(session, target.name, recipient_line)
    session.send(sender_line, dim=False)
    _record_revtell(session, res.name, sender_line)
    if target.afk:
        session.send(f"{target.name} is AFK: {target.afk_message}")
    if target.jailed:
        # Delivered (sender is staff, else we refused above) — but warn them the
        # jailed resident can't reply this way; visit the jail room for an answer.
        session.send(house_setting(session, "jail_reply_msg"))
    return True


def _private_command(session, args: str, *, remote: bool) -> None:
    """Shared body of .tell/.remote with a sticky 'current correspondent'.

    Resolution of ``<first-word> <rest>``:
      1. first word resolves to an online resident -> send <rest> to them, and
         make them the current correspondent;
      2. first word does NOT resolve and a correspondent is set -> send the whole
         text to the correspondent;
      3. otherwise -> "You aren't talking with anyone yet."
    """
    res = session.resident
    corr = res.correspondent
    ask = "Remote" if remote else "Tell"   # "Tell <Name> what?"

    if not args.strip():
        session.send(f"{ask} {corr} what?" if corr else NO_CORRESPONDENT)
        return

    parts = args.split(None, 1)
    first = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""

    target, ambiguous = session.world.resolve_resident(first)
    if ambiguous:
        # Never fall through on ambiguity — that could misdeliver.
        session.send(f"Who do you mean by '{first}'?  Be more specific.")
        return

    if target is not None:                 # (1) explicit, resolvable target
        if not rest:
            session.send(f"{ask} {target.name} what?")
            return
        if _deliver_private(session, target, rest, remote=remote):
            res.correspondent = target.name
        return

    # (2)/(3) first word didn't resolve -> fall back to the correspondent.
    #
    # ⛔ ...UNLESS THE FIRST WORD NAMES A REAL ACCOUNT (v1.52.0). Then they meant
    # a person, and quietly redirecting their words to a DIFFERENT person is
    # exactly the misdelivery this release exists to prevent — a private message
    # read by somebody it was never addressed to, with no undo.
    #
    # ⚠ This was already wrong before exact matching: `.tell Wren hello` with
    # Wren offline fell straight through and sent "Wren hello" to whoever you
    # last spoke to. Dropping abbreviation made it far easier to reach, which is
    # how it was noticed — so it is fixed here rather than left as the older bug
    # it technically is.
    #
    # ⚠ The fallback itself is KEPT for what it is for: `.t hello there` names
    # nobody, so it is plainly a message and goes to the correspondent. The
    # discriminator is whether the first word is somebody's name — the only
    # honest signal available, since nothing else distinguishes "I named a
    # person" from "I started a sentence".
    # ⚠ `get`, not `resolve`: "is there an account with exactly this name?" is
    # the precise question, and `get` is the exact casefold lookup that asks it.
    # Going through `resolve` would have been a roundabout way to the same
    # answer — and would have made this depend on the resolver's display-name
    # mapping rather than on the fact it actually needs.
    #
    # ⛔ NO `and rest` (external review, round 3). v1.52.0 wrote this guard as
    # `if named is not None and rest:`, which narrowed it to messages carrying a
    # BODY — so a bare `.tell Wren`, with Wren a real account that is offline and
    # a correspondent already set, skipped it and fell through, privately sending
    # Bob the word "Wren". The person was addressing Wren; somebody else got it.
    # A message with no body is still a message addressed to somebody.
    #
    # ⚠ Only the OFFLINE case reaches here at all: an online target is answered
    # above, and a bodyless one there still gets "Tell Bob what?". Dropping the
    # clause therefore changes nothing about anybody who is present.
    accounts = getattr(session, "accounts", None)
    named = accounts.get(first) if accounts is not None else None
    if named is not None:
        session.send(f"There's no one called {named.name} online.")
        return
    if corr is None:
        session.send(NO_CORRESPONDENT)
        return
    ctarget, _ = session.world.resolve_resident(corr)
    if ctarget is None:
        # Correspondent has gone offline; keep the slot (they may be back).
        session.send(f"There's no one called {corr} online.")
        return
    if _deliver_private(session, ctarget, args.strip(), remote=remote):
        res.correspondent = ctarget.name


def cmd_tell(session, args: str) -> None:
    _private_command(session, args, remote=False)


def cmd_remote(session, args: str) -> None:
    """A private emote to one person anywhere — a .tell that reads as an action.
    The recipient sees ">> <Sender> <text>"; the sender's own copy names where it
    went: ">> (to <Target>) <Sender> <text>"."""
    _private_command(session, args, remote=True)


def cmd_shout(session, text: str) -> None:
    text = text.strip()
    if not text:
        session.send("Shout what?")
        return
    if session.resident.jailed:            # jail = isolation: no reach outside the room
        session.send(JAILED_OUTBOUND)
        return
    if _gagged(session):
        return
    now = session.now()
    if not session.shout_limiter.allow(now):
        wait = int(session.shout_limiter.retry_after(now)) + 1
        session.send(f"Your voice is hoarse — wait {wait}s before shouting again.")
        return
    res = session.resident
    # Everyone else hears it subject to their `.ignore shout`; the shouter always
    # sees their own line, because seeing your own copy IS the confirmation that
    # you were heard (the same rule as say/tell). Still exactly one delivery per
    # participant.
    line = f"{res.name} shouts: {text}"
    session.world.broadcast_all(res.name, line, dim=False, category="shout",
                                exclude=[res.name])
    session.send(line, dim=False)
