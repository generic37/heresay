# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Full-width star frames around informational blocks.

The rule the whole thing exists to enforce: a block the SERVER is showing you is
framed; a person SPEAKING is not. So a listing can never be mistaken for someone
talking, and conversation can never be mistaken for a listing.
"""

import pytest

from heresay.accounts import AccountStore
from heresay.commands import FRAME_WIDTH, dispatch, frame_rule, framed

from conftest import FakeSession


# ---- the primitives ------------------------------------------------------

def test_a_bare_rule_is_78_stars():
    assert frame_rule() == "*" * 78
    assert len(frame_rule()) == FRAME_WIDTH


def test_a_titled_rule_sets_the_text_into_the_stars():
    rule = frame_rule("Talker commands")
    assert rule.startswith("*** Talker commands *")
    assert rule.endswith("*")
    assert len(rule) == 78


@pytest.mark.parametrize("title", ["x", "a title of quite reasonable length",
                                   "y" * 60, "z" * 120])
def test_every_rule_is_exactly_78_columns(title):
    rule = frame_rule(title)
    assert len(rule) == 78, rule
    assert rule.endswith("***"), "a visible run of stars always survives"


def test_framed_puts_the_body_between_two_rules():
    block = framed("Title", "one\ntwo")
    lines = block.split("\n")
    assert lines[0] == frame_rule("Title")
    assert lines[1] == ""
    assert lines[2:4] == ["one", "two"]
    assert lines[4] == ""
    assert lines[5] == frame_rule()          # no natural footer -> plain rule


def test_a_footer_is_embedded_in_the_bottom_rule():
    block = framed("Accounts", "row", "4 accounts")
    assert block.split("\n")[-1] == frame_rule("4 accounts")


# ---- what is framed ------------------------------------------------------

def _framed(session) -> bool:
    lines = [ln for ln in session.text.split("\n") if ln.strip()]
    return (bool(lines) and lines[0].startswith("***") and lines[-1].startswith("***")
            and len(lines[0]) == 78 and len(lines[-1]) == 78)


@pytest.mark.parametrize("cmd", [".who", ".help", ".help set", ".map", ".set"])
def test_informational_blocks_are_framed(world, clock, cmd, tmp_path):
    from heresay.server import _load_help_topics
    from pathlib import Path
    topics = _load_help_topics(Path(__file__).resolve().parent / "fixtures" / "house" / "help")
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw")
    s = FakeSession(world, name="Corvid", clock=clock, accounts=store,
                    help_topics=topics)
    s.clear()
    dispatch(s, cmd)
    assert _framed(s), f"{cmd} is not framed:\n{s.text}"


def test_the_map_art_itself_is_untouched(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    s.clear()
    dispatch(s, ".map")
    # Every line of the authored art still appears, byte for byte.
    for line in world.ascii_map.split("\n"):
        assert line in s.text.split("\n"), line


# ---- what is NOT framed, and that is the point ---------------------------

@pytest.mark.parametrize("cmd", [
    "hello there",              # a say
    ".e waves",                 # an emote
    ".think about it",          # a think
    ".shout hey",               # a shout
])
def test_conversation_is_never_framed(world, clock, cmd):
    s = FakeSession(world, name="Corvid", clock=clock)
    s.clear()
    dispatch(s, cmd)
    assert s.text.strip(), cmd
    assert "***" not in s.text, cmd


def test_a_tell_is_never_framed(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    a.clear(); b.clear()
    dispatch(a, ".tell Wren hello")
    assert ">> Corvid says: hello" in b.text
    assert "***" not in a.text and "***" not in b.text


def test_replayed_conversation_keeps_its_own_short_header(world, clock):
    """.rev and .revtell replay conversation, so they read as conversation —
    their existing header lines stay, and no frame goes round them."""
    from heresay.commands import REV_HEADER
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "something said aloud")
    a.clear()
    dispatch(a, ".rev")
    assert REV_HEADER in a.text
    assert not any(len(ln) == 78 and set(ln) == {"*"} for ln in a.text.split("\n"))


def test_one_line_errors_are_never_framed(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    s.clear()
    dispatch(s, ".nosuchcommand")
    assert s.text.strip()
    assert "***" not in s.text
