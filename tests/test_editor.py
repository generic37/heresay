# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The shared multi-line line editor."""

from heresay.commands import dispatch
from heresay.session import EDITOR_HEADER

from conftest import FakeSession


def _editor_session(world, clock, max_lines=20):
    s = FakeSession(world, name="Corvid", clock=clock)
    saved = []
    s.start_editor("test", max_lines, lambda sess, text: saved.append(text))
    return s, saved


def test_header_shown_on_entry(world, clock):
    s, _ = _editor_session(world, clock)
    assert EDITOR_HEADER in s.text
    assert s.in_editor()


def test_end_saves_joined_text(world, clock):
    s, saved = _editor_session(world, clock)
    s.feed_editor("first line")
    s.feed_editor("second line")
    s.feed_editor(".end")
    assert saved == ["first line\nsecond line"]
    assert not s.in_editor()


def test_abort_discards(world, clock):
    s, saved = _editor_session(world, clock)
    s.feed_editor("throwaway")
    s.feed_editor(".abort")
    assert saved == []
    assert not s.in_editor()
    assert "Discarded." in s.text


def test_cap_enforced_on_save_keeps_editing(world, clock):
    s, saved = _editor_session(world, clock, max_lines=2)
    s.feed_editor("1")
    s.feed_editor("2")
    s.feed_editor("3")
    s.feed_editor(".end")
    assert saved == []            # nothing saved
    assert s.in_editor()          # still composing
    assert "limit is 2" in s.text
    # Under cap after trimming would save; abort to leave.
    s.feed_editor(".abort")
    assert not s.in_editor()


def test_commands_not_interpreted_while_editing(world, clock):
    s, saved = _editor_session(world, clock)
    # A line that looks like a command is buffered verbatim, not executed.
    s.feed_editor(".who")
    s.feed_editor(";bob hi")
    s.feed_editor(">bob hi")
    s.feed_editor(".end")
    assert saved == [".who\n;bob hi\n>bob hi"]
    # No who-listing or emote leaked into output.
    assert "*** Current users on" not in s.text


def test_room_output_still_arrives_mid_compose(world, clock):
    writer = FakeSession(world, name="Corvid", clock=clock)
    talker = FakeSession(world, name="Wren", clock=clock)
    writer.start_editor("test", 20, lambda sess, text: None)
    writer.clear()
    # Wren speaks; the composing writer still hears the room.
    dispatch(talker, "the kettle's on")
    assert "Wren says: the kettle's on" in writer.text
    assert writer.in_editor()   # unaffected
