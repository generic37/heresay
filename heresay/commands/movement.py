# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations
from ..world import TOPIC_MAX, Invite
from ._shared import (
    JAILED_OUTBOUND, _describe_room, clean_message, framed, house_setting, resident_message,
)




def cmd_look(session, args: str) -> None:
    session.send(_describe_room(session, session.resident))


# ---------------------------------------------------------------------------
# Room topic — pure room-lifetime memory
# ---------------------------------------------------------------------------

def cmd_topic(session, args: str) -> None:
    """Set (or re-show) this room's topic. Anyone present may set it.

    It lives on the room in memory, evaporates when the last person leaves, and
    is gone on restart. Never stored, never logged — it is a note on the door of
    a gathering, not a record of one.
    """
    res = session.resident
    text = clean_message(args, TOPIC_MAX)
    if not text:
        current = session.world.topic_of(res.room_id)
        # Bare .topic with nothing set says nothing at all — deliberately. The
        # empty unit is dropped by the delivery layer, so no stray blank line.
        session.send(f"Topic: {current}" if current else "")
        return
    session.world.set_topic(res.room_id, text)
    session.world.broadcast_room(res.room_id, None, f"{res.name} sets the topic: {text}")


# The line shown when a resident is turned away from a staff-only room. The
# room-specific wording lives in the room file's `refusal:` header (deck.txt says
# "The cards won't deal you in."); this neutral line is the fallback when a
# `staff_only` room declares none.
REFUSAL_DEFAULT = "You may not enter."


def _refusal_for(world, room_id: str) -> str:
    room = world.room(room_id)
    return (room.refusal if room and room.refusal else REFUSAL_DEFAULT)


def cmd_go(session, args: str) -> None:
    query = args.strip()
    if not query:
        session.send("Go where?")
        return
    world = session.world
    res = session.resident
    if res.jailed:                         # jail = confinement, by the flag not the map
        session.send(JAILED_OUTBOUND)
        return
    target_id, ambiguous = world.resolve_room(query)
    if ambiguous:
        session.send("Which room do you mean?  Be more specific.")
        return
    if target_id is None:
        session.send("There's no way there from here.")
        return
    if target_id == res.room_id:
        session.send("You're already there.")
        return

    staff = res.is_staff()
    # The staff room turns residents away — in the room's own words.
    if target_id in world.staff_only_rooms and not staff:
        session.send(_refusal_for(world, target_id))
        return
    # Residents move only along the graph; staff travel anywhere directly.
    if not staff and not world.adjacent(res.room_id, target_id):
        session.send("There's no way there from here.")
        return

    from_room = res.room_id
    to_room = target_id
    to_name = world.room(to_room).name

    # A shut door stops residents; staff pass (announced normally).
    # The refusal says "locked" — what the door IS, in the word the person who
    # typed .lock used. The room's own announcements still read as gestures
    # ("shuts the door." / "opens the door."), because that is what they are.
    if world.is_locked(to_room) and not staff:
        session.send(f"The {to_name}'s door is locked.")
        return

    # Announce the departure to the room being left, then move, then arrival —
    # each in the resident's own words if they've set any. A custom exit_msg
    # replaces the whole line, so it drops the destination: their choice.
    own_out = (res.messages or {}).get("exit_msg")
    if own_out:
        leaving = own_out
    else:
        # The house wording names the destination via a <room> placeholder;
        # a resident's OWN exit_msg replaces the whole line and drops it (their
        # choice). The placeholder is substituted only here, at the point of use.
        leaving = house_setting(session, "exit_msg").replace("<room>", to_name)
    world.broadcast_room(from_room, None, f"{res.name} {leaving}",
                         exclude=[res.name], category="exit_msg")
    res.room_id = to_room
    world.room_emptied(from_room)   # a lock lifts, a topic lapses
    # ALWAYS-SHOW EXCEPTION: staff walking into a room that
    # is currently LOCKED is announced to everyone in it, `.ignore enter_msg`
    # or not. A shut door is people choosing to be alone, and staff passing
    # through it is the one arrival nobody may filter away — the covenant's "no
    # invisible watching", enforced against the noise filter too.
    staff_into_locked = staff and world.is_locked(to_room)
    world.broadcast_room(to_room, None,
                         f"{res.name} {resident_message(session, res, 'enter_msg')}",
                         exclude=[res.name], category="enter_msg",
                         always=staff_into_locked)
    session.send(_describe_room(session, res))


def cmd_map(session, args: str) -> None:
    # Printed verbatim — never wrapped, never generated. The frame goes around
    # the art; not one byte of the art itself is touched. A narrow session gets
    # map.narrow.txt where the house has drawn one, and the wide art otherwise.
    session.send_raw(framed(f"The map of {session.theme.instance_name}",
                            session.world.map_for(session.is_narrow),
                            width=session.width))


# ---------------------------------------------------------------------------
# Room locks (resident feature)
# ---------------------------------------------------------------------------

def cmd_lock(session, args: str) -> None:
    res = session.resident
    room = session.world.room(res.room_id)
    if room is None or not room.lockable:
        session.send("There's no lock on this door.")
        return
    if session.world.is_locked(res.room_id):
        session.send("The door is already shut.")
        return
    session.world.lock(res.room_id)
    session.world.broadcast_room(res.room_id, None, f"{res.name} shuts the door.")


def cmd_unlock(session, args: str) -> None:
    res = session.resident
    room = session.world.room(res.room_id)
    if room is None or not room.lockable:
        session.send("There's no lock on this door.")
        return
    if not session.world.is_locked(res.room_id):
        session.send("The door isn't shut.")
        return
    session.world.unlock(res.room_id)
    session.world.broadcast_room(res.room_id, None, f"{res.name} opens the door.")


# ---------------------------------------------------------------------------
# Invitations (resident feature)
# ---------------------------------------------------------------------------

def cmd_invite(session, args: str) -> None:
    name = args.strip()
    if not name:
        session.send("Invite whom?")
        return
    if session.resident.jailed:            # jail = isolation: no reach outside the room
        session.send(JAILED_OUTBOUND)
        return
    target, ambiguous = session.world.resolve_resident(name)
    if ambiguous:
        session.send(f"Who do you mean by '{name}'?  Be more specific.")
        return
    if target is None:
        session.send(f"There's no one called {name} online.")
        return
    if target is session.resident:
        session.send("You're already with yourself.")
        return
    if not session.invite_bucket.take(session.now()):
        session.send("You're inviting too fast — give it a moment.")
        return
    res = session.resident
    # .mute suppresses incoming invites from the muted person — silently, so the
    # inviter (like anyone muted) is none the wiser.
    if target.sees(res.name):
        ttl = session.config.invite_ttl_seconds
        target.pending_invite = Invite(inviter=res.name, room_id=res.room_id,
                                       expires_at=session.now() + ttl)
        if target.session is not None:
            target.session.send(
                f"{res.name} invites you to join them. Type .join {res.name.lower()}."
            )
    session.send(f"You invite {target.name} to join you.")


def cmd_join(session, args: str) -> None:
    """Accept an invitation — bare, or by the inviter's EXACT name (v1.52.4).

    ⛔ **A prefix is not a name here either.** This handler compared the typed
    text against the inviter's own name with `startswith`, so it was the one
    surviving prefix match on a person after v1.52.0 — outside that release's
    guard, which reads the two RESOLVERS and this never touched one. Being
    teleported by a name you did not finish typing is the same class of mistake
    as a mis-delivered `.tell`: you end up somewhere, in front of somebody,
    neither of which you asked for.

    ⚠ **The order is validate-then-match.** The invitation is checked for
    existence, expiry and lapse FIRST, so a name typed at an invitation that is
    no longer there gets the honest "No one has invited you." rather than a
    refusal naming somebody who has already walked off.

    **Bare `.join` takes the pending invitation**, which — the model being ONE
    slot per resident, replaced by each new invite — is the most recent one by
    construction. It used to answer "Join whom?", a question the house could
    already answer; and a resident who has to retype the name is a resident who
    reaches for an abbreviation.
    """
    name = args.strip()
    res = session.resident
    if res.jailed:                         # accepting a join would move you out of jail
        session.send(JAILED_OUTBOUND)
        return
    inv = res.pending_invite
    if inv is None:
        session.send("No one has invited you.")
        return
    if session.now() > inv.expires_at:
        res.pending_invite = None
        session.send("No one has invited you.")
        return
    inviter, _ = session.world.resolve_resident(inv.inviter)
    if inviter is None or inviter.room_id != inv.room_id:
        # Inviter left, or changed rooms — the invitation has lapsed.
        res.pending_invite = None
        session.send("No one has invited you.")
        return
    # ⛔ EXACT, casefold — never a prefix, and never a fall-through to the
    # invitation that happens to be pending. Somebody who names the wrong person
    # meant that person; they are told who is actually holding the door open and
    # the invitation is left where it is.
    if name and name.casefold() != inv.inviter.casefold():
        session.send(f"{inv.inviter} has invited you, not {name}. "
                     f"Type .join {inv.inviter.lower()} — or just .join.")
        return
    # The staff room won't admit a resident, even by invitation — same line
    # as .go, the room's own.
    if inviter.room_id in session.world.staff_only_rooms and not res.is_staff():
        session.send(_refusal_for(session.world, inviter.room_id))
        return

    from_room = res.room_id
    to_room = inviter.room_id
    res.pending_invite = None
    # Teleport through geography and locked doors alike; arrival announced as
    # always — and filterable as ordinary movement, because that is what it is.
    session.world.broadcast_room(from_room, None, f"{res.name} slips away.",
                                 exclude=[res.name], category="exit_msg")
    res.room_id = to_room
    session.world.room_emptied(from_room)
    staff_into_locked = res.is_staff() and session.world.is_locked(to_room)
    session.world.broadcast_room(to_room, None, f"{res.name} arrives.",
                                 exclude=[res.name], category="enter_msg",
                                 always=staff_into_locked)
    session.send(_describe_room(session, res))
