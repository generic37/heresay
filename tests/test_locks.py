# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Room locks: lock/unlock, blocked .go, auto-unlock, wizard bypass.

Uses Commons rooms: grotto is lockable; spring is adjacent and not lockable.
"""

from heresay.commands import dispatch

from conftest import FakeSession


def test_lock_in_lockable_announces(world, clock):
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    b = FakeSession(world, name="Wren", room="grotto", clock=clock)
    b.clear()
    dispatch(a, ".lock")
    assert world.is_locked("grotto")
    assert "Corvid shuts the door." in b.text


def test_lock_non_lockable_refused(world, clock):
    a = FakeSession(world, name="Corvid", room="spring", clock=clock)
    dispatch(a, ".lock")
    assert "no lock on this door" in a.text.lower()
    assert not world.is_locked("spring")


def test_go_into_locked_blocked(world, clock):
    """The refusal names the state of the door, in the word the person who
    typed .lock used — and the word help/lock.txt quotes."""
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    dispatch(a, ".lock")
    b = FakeSession(world, name="Wren", room="spring", clock=clock)
    dispatch(b, ".go grotto")
    assert b.resident.room_id == "spring"
    assert b.out[-1] == "The grotto's door is locked."


def test_the_lock_announcements_are_still_gestures(world, clock):
    """Only the refusal changed: locking and unlocking are things a person does
    to a door, and the room sees them done."""
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    b = FakeSession(world, name="Wren", room="grotto", clock=clock)
    b.clear()
    dispatch(a, ".lock")
    assert "Corvid shuts the door." in b.text
    dispatch(a, ".unlock")
    assert "Corvid opens the door." in b.text
    assert "locked" not in b.text


def test_wizard_passes_lock_with_announced_arrival(world, clock):
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    dispatch(a, ".lock")
    a.clear()
    w = FakeSession(world, name="Warden", room="spring", clock=clock, level="wizard")
    dispatch(w, ".go grotto")
    assert w.resident.room_id == "grotto"
    assert "Warden arrives." in a.text   # no invisible observation


def test_auto_unlock_on_empty(world, clock):
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    dispatch(a, ".lock")
    assert world.is_locked("grotto")
    dispatch(a, ".go spring")              # last occupant leaves
    assert not world.is_locked("grotto")


def test_unlock_announces(world, clock):
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    b = FakeSession(world, name="Wren", room="grotto", clock=clock)
    dispatch(a, ".lock")
    b.clear()
    dispatch(a, ".unlock")
    assert not world.is_locked("grotto")
    assert "Corvid opens the door." in b.text


def test_unlock_when_not_shut(world, clock):
    a = FakeSession(world, name="Corvid", room="grotto", clock=clock)
    dispatch(a, ".unlock")
    assert "isn't shut" in a.text.lower()
