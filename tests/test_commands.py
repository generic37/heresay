# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

from heresay import commands
from heresay.commands import dispatch

from conftest import FakeSession


def test_bare_text_is_speech(session):
    dispatch(session, "the rain finally stopped.")
    assert "Corvid says: the rain finally stopped." in session.text


def test_emote_dot_e(session):
    dispatch(session, ".e stretches.")
    assert "Corvid stretches." in session.text


def test_emote_dot_em(session):
    dispatch(session, ".em stretches.")
    assert "Corvid stretches." in session.text


def test_emote_collapses_extra_spaces_after_verb(session):
    dispatch(session, ".e  waves")
    assert "Corvid waves" in session.text
    assert "Corvid  waves" not in session.text


def test_dot_me_is_no_longer_an_emote_alias(session):
    dispatch(session, ".me waves")
    assert "Unknown command: .me" in session.text
    assert "Corvid waves" not in session.text


def test_emote_semicolon_shortcut(session):
    dispatch(session, ";waves hello")
    assert "Corvid waves hello" in session.text


def test_semicolon_is_not_a_tell(session):
    # ';' is the emote shortcut: it reaches the room and never touches the
    # sticky correspondent slot.
    dispatch(session, ";waves")
    assert "Corvid waves" in session.text
    assert commands.NO_CORRESPONDENT not in session.text
    assert session.resident.correspondent is None


def test_semicolon_spacing_is_identical_to_no_space(session):
    # ';waves' == '; waves' == ';  waves' -> one space, by construction.
    for line in (";waves", "; waves", ";  waves"):
        session.clear()
        dispatch(session, line)
        assert "Corvid waves" in session.text, line
        assert "Corvid  waves" not in session.text, line


def test_bare_semicolon_matches_bare_dot_e(session):
    dispatch(session, ".e")
    dot_e = list(session.out)
    session.clear()
    dispatch(session, ";")
    assert session.out == dot_e == ["Emote what?"]


def test_think(session):
    dispatch(session, ".think hmm")
    assert "Corvid thinks . . oO( hmm )" in session.text


def test_unknown_command_is_friendly(session):
    dispatch(session, ".frobnicate")
    assert "Unknown command" in session.text


def test_help_lists_commands(session):
    dispatch(session, ".help")
    assert ".who" in session.text and ".quit" in session.text


def test_desc_sets_description(session):
    dispatch(session, ".desc staff of crows")
    assert session.resident.description == "staff of crows"
    assert session.accounts.descriptions["corvid"] == "staff of crows"


def test_go_adjacent(session):
    dispatch(session, ".go gallery")   # atrium -> gallery
    assert session.resident.room_id == "gallery"


def test_go_prefix(session):
    dispatch(session, ".go gall")            # unambiguous prefix
    assert session.resident.room_id == "gallery"


def test_go_nonadjacent_blocked(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)   # in atrium
    dispatch(s, ".go grotto")            # not adjacent to atrium
    assert s.resident.room_id == "atrium"
    assert "no way there" in s.text.lower()


def test_who_shows_room_and_idle(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    clock.t = 125.0
    s.resident.last_input = 0.0  # idle 125s -> 02:05
    dispatch(s, ".who")
    assert "Corvid" in s.text and "atrium" in s.text and "02:05" in s.text


def test_tell_targets_one_person(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    dispatch(a, ".tell Wren meet me by the oak")
    assert ">> Corvid says: meet me by the oak" in b.text
    assert ">> Corvid says: meet me by the oak" in a.text   # sender sees the same line


def test_tell_unresolved_first_word_no_correspondent(session):
    # With no current correspondent, an unresolvable first word falls through to
    # the (empty) sticky slot and reports that there's no conversation yet.
    dispatch(session, ".tell Nobody hello")
    assert "You aren't talking with anyone yet." in session.text


def test_speech_shared_bucket_with_verbs(session):
    # Burst of 5 across mixed verbs, then throttled — verbs don't evade it.
    verbs = ["hi", ".e nods", ".think hmm", ".e waves", "still here"]
    for v in verbs:
        dispatch(session, v)
    session.clear()
    dispatch(session, "one too many")
    assert commands.THROTTLE_MSG in session.text


def test_shout_reaches_all_rooms(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    c = FakeSession(world, name="Finch", room="rotunda", clock=clock)
    dispatch(a, ".shout dinner's ready")
    assert "Corvid shouts: dinner's ready" in b.text
    assert "Corvid shouts: dinner's ready" in c.text


def test_shout_rate_limited(session):
    dispatch(session, ".shout first")
    session.clear()
    dispatch(session, ".shout second")
    assert "hoarse" in session.text.lower()


def test_quit_returns_sentinel(session):
    assert dispatch(session, ".quit") == commands.QUIT
