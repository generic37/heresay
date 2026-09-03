# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command aliases: exact-match, dispatch to the same handlers, no collisions."""

from pathlib import Path

from heresay.accounts import AccountStore
from heresay.commands import (
    QUIT, _COMMANDS, cmd_emote, cmd_examine, cmd_go, cmd_help, cmd_look,
    cmd_map, cmd_quit, cmd_who, dispatch,
)

from conftest import FakeSession


def test_alias_table_points_at_the_same_handlers():
    assert _COMMANDS["w"] is cmd_who
    assert _COMMANDS["g"] is cmd_go
    assert _COMMANDS["l"] is cmd_look          # pre-existing
    assert _COMMANDS["ex"] is cmd_examine
    assert _COMMANDS["exa"] is cmd_examine      # kept alongside
    assert _COMMANDS["h"] is cmd_help           # pre-existing
    assert _COMMANDS["q"] is cmd_quit           # pre-existing
    assert _COMMANDS["x"] is cmd_quit
    assert _COMMANDS["m"] is cmd_map


def test_m_is_map_and_does_not_shadow_the_mail_verbs():
    # Dispatch is exact-match on the verb, so '.m' can't swallow '.mail',
    # '.mail_staff', '.mailpurge' or '.mute' — each is its own exact key.
    assert _COMMANDS["m"] is cmd_map
    for verb in ("mail", "mail_staff", "mailpurge", "mute", "map"):
        assert _COMMANDS[verb] is not cmd_map or verb == "map"
        assert verb in _COMMANDS


def test_m_dispatches_to_map(session):
    dispatch(session, ".m")
    # The art's own title row is gone (the frame titles it); the boxes are the
    # map's most distinctive strokes.
    assert "*** The map of Commons *" in session.text
    assert "| Backroom |" in session.text   # the staff room, drawn as flavour


def test_e_stays_emote_distinct_from_ex_examine():
    assert _COMMANDS["e"] is cmd_emote
    assert _COMMANDS["ex"] is cmd_examine


def test_w_dispatches_to_who(session):
    dispatch(session, ".w")
    assert "*** Current users on" in session.text


def test_g_dispatches_to_go(session):
    dispatch(session, ".g gallery")       # atrium -> gallery
    assert session.resident.room_id == "gallery"


def test_x_and_q_quit(session):
    assert dispatch(session, ".x") == QUIT
    assert dispatch(session, ".q") == QUIT


def test_ex_dispatches_to_examine(world, clock, tmp_path):
    acc = AccountStore(tmp_path / "a.json")
    acc.create("Wren", "pw", description="small and loud")
    s = FakeSession(world, name="Corvid", clock=clock, accounts=acc)
    dispatch(s, ".ex Wren")
    assert "Wren small and loud" in s.text       # examine output, no dash
