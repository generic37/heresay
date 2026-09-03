# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

from heresay.commands import dispatch

from conftest import FakeSession


def test_mute_filters_speech(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)

    # Wren mutes Corvid.
    dispatch(b, ".mute Corvid")
    b.clear()

    dispatch(a, "you can't hear this")
    assert a.text  # speaker sees own line
    assert "you can't hear this" not in b.text  # muter does not

    # Unmute restores it.
    dispatch(b, ".unmute Corvid")
    b.clear()
    dispatch(a, "now you can")
    assert "Corvid says: now you can" in b.text


def test_mute_is_session_scoped_and_one_way(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".mute Corvid")
    b.clear()
    a.clear()

    # Wren can still be heard BY Corvid (mute is one-directional).
    dispatch(b, "wren speaks")
    assert "Wren says: wren speaks" in a.text


def test_mute_also_filters_emotes_and_tells(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".mute Corvid")
    b.clear()

    dispatch(a, ".e dances")
    assert "dances" not in b.text

    dispatch(a, ".tell Wren psst")
    assert "psst" not in b.text  # muted target receives no tell
