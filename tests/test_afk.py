# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AFK: command-level behavior (setting, tell notice, .who marker, clear rule)."""

from heresay.commands import AFK_DEFAULT, clears_afk, dispatch

from conftest import FakeSession


def test_afk_default_message(session):
    dispatch(session, ".afk")
    assert session.resident.afk is True
    assert session.resident.afk_message == AFK_DEFAULT
    assert "You're now AFK" in session.text and AFK_DEFAULT in session.text


def test_afk_custom_message(session):
    dispatch(session, ".afk making tarts, all on a summer day")
    assert session.resident.afk is True
    assert session.resident.afk_message == "making tarts, all on a summer day"


def test_afk_is_announced_to_the_room(world, clock):
    """1.16.0: going AFK tells the room, in the same words a tell-time notice
    uses — so "away" reads the same wherever you meet it."""
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)   # same room
    b.clear()
    dispatch(a, ".afk")
    assert f"Corvid is AFK: {AFK_DEFAULT}" in b.text


def test_the_afk_person_hears_it_once_and_as_their_own_confirmation(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    FakeSession(world, name="Wren", clock=clock)
    a.clear()
    dispatch(a, ".afk")
    # Their own copy is the confirmation line, not the room announcement.
    assert a.text.count("You're now AFK") == 1
    assert "Corvid is AFK:" not in a.text


def test_tell_to_afk_informs_sender_and_still_delivers(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".afk feeding the badger")
    b.clear()
    dispatch(a, ".tell Wren you there?")
    assert ">> Corvid says: you there?" in b.text          # delivered
    assert "Wren is AFK: feeding the badger" in a.text       # sender informed


def test_who_shows_afk_marker(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".afk")
    a.clear()
    dispatch(a, ".who")
    assert "Corvid (AFK)" in a.text


def test_clears_afk_rule():
    assert clears_afk("the rain stopped")   # speech clears
    assert clears_afk(".who")               # a command clears
    assert clears_afk(";waves")             # an emote clears
    assert not clears_afk(".afk")           # (re)setting AFK does not clear
    assert not clears_afk(".afk busy now")
    assert not clears_afk("")               # a blank line does not clear
    assert not clears_afk("   ")
