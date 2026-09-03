# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Invitations: deliver, join through locks/geography, expiry, replace, mute, bucket.

⛔ **The model is ONE pending invite per resident** (`Resident.pending_invite`,
a single slot): a second invitation REPLACES the first rather than queueing
beside it. So "the most recent" and "the only one" are the same invitation, and
bare `.join` has exactly one thing it can mean.
"""

from heresay.commands import dispatch

from conftest import FakeSession


def test_invite_delivers_and_confirms(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    dispatch(a, ".invite Wren")
    assert "Corvid invites you to join them. Type .join corvid." in b.text
    assert "You invite Wren to join you." in a.text


def test_join_through_lock_and_geography(world, clock):
    # Corvid locks the grotto; Wren (in the rampart, non-adjacent) still joins.
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    dispatch(a, ".lock")
    b = FakeSession(world, name="Wren", room="rampart", clock=clock)
    dispatch(a, ".invite Wren")
    a.clear()
    dispatch(b, ".join corvid")
    assert b.resident.room_id == "grotto"   # teleported past lock + geography
    assert "Wren arrives." in a.text


def test_join_without_invite(world, clock):
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".join corvid")
    assert "No one has invited you." in b.text


def test_invite_expiry_after_5_minutes(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    dispatch(a, ".invite Wren")
    clock.t += 301                      # past the 300s TTL
    dispatch(b, ".join corvid")
    assert "No one has invited you." in b.text
    assert b.resident.room_id == "oak"


def test_invite_expires_when_inviter_moves(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)   # in atrium
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    dispatch(a, ".invite Wren")
    dispatch(a, ".go gallery")    # inviter changes rooms
    dispatch(b, ".join corvid")
    assert "No one has invited you." in b.text


def test_repeat_invite_replaces(world, clock):
    """⚠ The property is "Corvid's invitation is gone", and it is asserted on
    what `.join corvid` DOES rather than on the sentence it says: since v1.52.4
    a name that does not match the pending inviter is refused BY NAME (`Cara has
    invited you, not corvid`) instead of with the generic line. Joining Corvid
    is what must not happen, and it does not."""
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    c = FakeSession(world, name="Cara", room="rampart", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    dispatch(c, ".invite Wren")         # replaces Corvid's pending invite
    b.clear()
    dispatch(b, ".join corvid")
    assert b.resident.room_id == "atrium"    # Corvid's is gone
    assert "Cara has invited you" in b.text       # and the live one is named


def test_mute_suppresses_invite(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".mute Corvid")
    b.clear()
    dispatch(a, ".invite Wren")
    assert "invites you" not in b.text.lower()
    assert b.resident.pending_invite is None
    assert "You invite Wren" in a.text          # inviter is none the wiser


def test_invite_bucket_limits(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    for n in ("Wren", "Cara", "Finch", "Robin"):
        FakeSession(world, name=n, clock=clock)
    for n in ("Wren", "Cara", "Finch"):         # burst of 3 ok
        dispatch(a, f".invite {n}")
    a.clear()
    dispatch(a, ".invite Robin")                # 4th at the same instant
    assert "inviting too fast" in a.text.lower()


# ---------------------------------------------------------------------------
# ⛔ The inviter's name is EXACT, and bare .join takes the pending one (v1.52.4)
#
# `.join` was the one surviving prefix match on a PERSON after v1.52.0 — the
# guard there reads the two resolvers, and this handler compared the typed text
# against the inviter's name itself, so it sat outside the sweep entirely.
# ---------------------------------------------------------------------------

def test_join_takes_an_exact_inviter_name(world, clock):
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    dispatch(b, ".join Corvid")
    assert b.resident.room_id == "oak"


def test_join_still_folds_case(world, clock):
    """Case is free, exactly as everywhere else — it is abbreviation that went."""
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    dispatch(b, ".join CORVID")
    assert b.resident.room_id == "oak"


def test_join_REFUSES_a_prefix_of_the_inviters_name(world, clock):
    """⛔ The fix. `.join corv` used to teleport you to Corvid."""
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    b.clear()
    dispatch(b, ".join corv")
    assert b.resident.room_id == "atrium"          # did not move
    assert "Corvid has invited you" in b.text          # named, and refused
    assert b.resident.pending_invite is not None        # and NOT spent


def test_a_non_matching_name_is_refused_and_does_not_fall_through(world, clock):
    """⚠ Naming somebody who did not invite you must not quietly join whoever
    did — that is the same class of mistake as a mis-delivered `.tell`."""
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    b.clear()
    dispatch(b, ".join Cara")
    assert b.resident.room_id == "atrium"
    assert "Corvid has invited you" in b.text
    assert b.resident.pending_invite is not None


def test_bare_join_takes_the_pending_invitation(world, clock):
    """One slot, so the most recent IS the only one. Bare `.join` used to ask
    "Join whom?" — a question the house could already answer."""
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    dispatch(b, ".join")
    assert b.resident.room_id == "oak"


def test_bare_join_takes_the_MOST_RECENT_when_a_second_invite_replaced_the_first(
        world, clock):
    """The single slot is what makes "most recent" well defined: Cara's invite
    replaces Corvid's, so bare `.join` goes to Cara — and `.join Corvid` is
    refused rather than falling through to the invitation that is really there."""
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    c = FakeSession(world, name="Cara", room="rampart", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    dispatch(c, ".invite Wren")                 # replaces Corvid's
    b.clear()
    dispatch(b, ".join Corvid")
    assert b.resident.room_id == "atrium"  # the stale name is not honoured
    dispatch(b, ".join")
    assert b.resident.room_id == "rampart"         # the most recent one


def test_bare_join_with_no_invitation_says_so(world, clock):
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".join")
    assert "No one has invited you." in b.text


def test_bare_join_on_an_EXPIRED_invitation_says_so(world, clock):
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".invite Wren")
    clock.t += 301
    b.clear()
    dispatch(b, ".join")
    assert "No one has invited you." in b.text
    assert b.resident.room_id == "atrium"
