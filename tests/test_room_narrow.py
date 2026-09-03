# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Room descriptions at 40 columns — auto-reflow, and `<id>.narrow.txt`.

Room files are hand-wrapped near 70, which is right for the wide screen and
wrong for a portrait phone: at 40 each of those lines wraps again a few words
from its end, and the paragraph arrives long/short/long/short. Three rules, and
the first is the one to protect:

* **78** — the file byte for byte, hard breaks and all. A `.narrow.txt` is not
  even consulted.
* **40, no `.narrow.txt`** — the prose is reflowed: soft breaks joined, blank
  lines kept as paragraph breaks, set once at the session's width.
* **40, `.narrow.txt` present** — that file verbatim; the author has set this
  room at 40 themselves and their version wins.

The third exists for rooms whose layout is deliberate. mirror_hall is written
in mirror writing, right-aligned; reflowing it would shuffle the mirror.
"""

import logging
from pathlib import Path

import pytest

from conftest import FakeSession
from heresay.commands import dispatch
from heresay.commands._shared import _describe_room, reflow, room_description
from heresay.world import load_world, parse_room_file

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"

# Hand-wrapped near 70, like every shipped room file: two paragraphs, and inside
# each one the breaks are only where the author's screen ended.
HARD_WRAPPED = """\
name: hall
exits:

The hall is long and panelled in dark oak, and the lamps along it have
been lit for so long that nobody now remembers who lights them or when
the lighting of them began.

A second paragraph, kept apart from the first by a blank line, which is
the one break that means something.
"""

NARROW_ART = "a hall\n  set by hand\n    at forty\n"


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


def _house(tmp_path, room=HARD_WRAPPED, narrow=None):
    """A one-room world on disk, optionally with a `.narrow.txt` beside it."""
    rooms = tmp_path / "world" / "rooms"
    rooms.mkdir(parents=True)
    (rooms / "hall.txt").write_text(room, encoding="utf-8")
    if narrow is not None:
        (rooms / "hall.narrow.txt").write_text(narrow, encoding="utf-8")
    return load_world(tmp_path / "world", "hall")


def described(world, width):
    """The description block as one session of this width would receive it."""
    me = FakeSession(world, name="Corvid", width=width)
    return room_description(me, world.room(me.resident.room_id))


# ---------------------------------------------------------------------------
# 78: verbatim, always
# ---------------------------------------------------------------------------

def test_wide_serves_the_file_byte_for_byte(tmp_path):
    world = _house(tmp_path)
    body = HARD_WRAPPED.split("\n\n", 1)[1].rstrip("\n")
    assert described(world, 78) == body
    assert described(world, 78) == world.room("hall").description


def test_wide_keeps_the_authors_hard_breaks(tmp_path):
    """The author wrapped it; at 78 that wrapping is theirs and survives."""
    lines = described(_house(tmp_path), 78).split("\n")
    assert lines[0].endswith("have")          # exactly where the file breaks
    assert "" in lines                        # the paragraph break, still there


def test_a_stray_narrow_file_is_ignored_at_78(tmp_path):
    """A house that HAS drawn a narrow version must not have it leak into wide."""
    world = _house(tmp_path, narrow=NARROW_ART)
    assert described(world, 78) == world.room("hall").description
    assert "set by hand" not in described(world, 78)


def test_every_shipped_room_is_verbatim_at_78(world):
    """The whole house, not just a fixture: nothing changed for wide readers."""
    for rid, room in world.rooms.items():
        me = FakeSession(world, name=f"W{abs(hash(rid)) % 999}", width=78)
        assert room_description(me, room) == room.description, rid


# ---------------------------------------------------------------------------
# 40, no `.narrow.txt`: auto-reflow
# ---------------------------------------------------------------------------

def test_narrow_reflows_when_there_is_no_narrow_file(tmp_path):
    world = _house(tmp_path)
    out = described(world, 40)
    assert out != world.room("hall").description
    assert all(len(l) <= 40 for l in out.split("\n")), out


def test_the_reflow_is_not_ragged(tmp_path):
    """The bug being fixed: 70-column lines re-wrapped at 40 alternate long and
    short. After a reflow every line but a paragraph's last is FULL — the next
    word genuinely would not fit."""
    world = _house(tmp_path)
    paragraphs = described(world, 40).split("\n\n")
    assert len(paragraphs) == 2
    for para in paragraphs:
        lines = para.split("\n")
        for line, following in zip(lines, lines[1:]):
            next_word = following.split(" ")[0]
            assert len(line) + 1 + len(next_word) > 40, (line, next_word)


def test_single_breaks_are_joined_and_blank_lines_are_kept(tmp_path):
    world = _house(tmp_path)
    out = described(world, 40)
    # The author's first break was after "have"; the words either side of it are
    # now flowing text.
    assert "have\nbeen" not in out
    assert "have been" in " ".join(out.split("\n"))
    assert "\n\n" in out                       # the paragraph break survives
    assert out.split("\n\n")[1].startswith("A second paragraph")


def test_a_missing_narrow_file_is_never_an_error(tmp_path):
    """Absent is the ordinary case, not a fallback path with a warning in it."""
    world = _house(tmp_path)
    assert world.room("hall").description_narrow == ""
    assert described(world, 40)                # reflowed, no exception


# ---------------------------------------------------------------------------
# 40, `.narrow.txt`: verbatim override
# ---------------------------------------------------------------------------

def test_narrow_file_is_served_verbatim_at_40(tmp_path):
    world = _house(tmp_path, narrow=NARROW_ART)
    assert described(world, 40) == NARROW_ART.rstrip("\n")


def test_the_narrow_file_beats_the_reflow(tmp_path):
    """Its indentation would not survive a reflow — that is the whole point."""
    world = _house(tmp_path, narrow=NARROW_ART)
    out = described(world, 40)
    assert "  set by hand" in out
    assert out != reflow(world.room("hall").description, 40)


def test_mirror_hall_ships_one_and_it_holds_the_mirror(world):
    """The shipped example. Mirror writing is right-aligned by construction, so
    a reflow would shuffle it; the narrow file keeps it."""
    room = world.rooms["mirror_hall"]
    assert room.description_narrow, "mirror_hall.narrow.txt should ship"
    out = described_for(world, room, 40)
    assert out == room.description_narrow
    assert all(len(l) <= 40 for l in out.split("\n"))
    # Un-mirrored, it is still the same sentence the wide file tells.
    assert "You are in a Victorian drawing room" in _unmirror(out)
    assert "You are in a Victorian drawing room" in _unmirror(room.description)


def described_for(world, room, width):
    me = FakeSession(world, name="Looker", width=width)
    return room_description(me, room)


def _unmirror(text):
    return " ".join(l[::-1].strip() for l in text.split("\n"))


# ---------------------------------------------------------------------------
# `reflow` itself
# ---------------------------------------------------------------------------

def test_reflow_joins_a_paragraph_and_wraps_it():
    assert reflow("one two\nthree four", 9) == "one two\nthree\nfour"


def test_reflow_keeps_a_paragraph_break():
    assert reflow("a b\n\nc d", 20) == "a b\n\nc d"


def test_reflow_collapses_a_run_of_blank_lines_to_one():
    """The gap is the signal, not its size."""
    assert reflow("a\n\n\n\nb", 20) == "a\n\nb"


def test_reflow_drops_leading_and_trailing_blank_lines():
    assert reflow("\n\na b\n\n", 20) == "a b"


def test_reflow_of_nothing_is_nothing():
    assert reflow("", 40) == ""
    assert reflow("\n \n", 40) == ""


def test_reflow_never_splits_a_long_word():
    long = "supercalifragilistic"
    assert reflow(f"hi {long}", 8) == f"hi\n{long}"


# ---------------------------------------------------------------------------
# The loader: a narrow variant is content, never a room
# ---------------------------------------------------------------------------

def test_a_narrow_variant_is_not_loaded_as_a_room(tmp_path):
    """`*.narrow.txt` matches `*.txt`. Unfiltered, each would be parsed as a room
    of its own — no `name:` header, and the house would refuse to start."""
    world = _house(tmp_path, narrow=NARROW_ART)
    assert set(world.rooms) == {"hall"}
    assert "hall.narrow" not in world.rooms


def test_the_narrow_text_lands_on_the_room(tmp_path):
    world = _house(tmp_path, narrow=NARROW_ART)
    assert world.room("hall").description_narrow == NARROW_ART.rstrip("\n")


def test_parse_room_file_picks_the_variant_up_directly(tmp_path):
    rooms = tmp_path / "rooms"
    rooms.mkdir()
    (rooms / "hall.txt").write_text(HARD_WRAPPED, encoding="utf-8")
    (rooms / "hall.narrow.txt").write_text(NARROW_ART, encoding="utf-8")
    room = parse_room_file(rooms / "hall.txt")
    assert room.description_narrow == NARROW_ART.rstrip("\n")


def test_an_orphan_narrow_file_is_named_but_never_fatal(tmp_path, caplog):
    """A narrow file whose room does not exist is doing nothing, which is what a
    typo'd filename looks like. Facts only — a filename, never room text."""
    rooms = tmp_path / "world" / "rooms"
    rooms.mkdir(parents=True)
    (rooms / "hall.txt").write_text(HARD_WRAPPED, encoding="utf-8")
    (rooms / "haII.narrow.txt").write_text(NARROW_ART, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        world = load_world(tmp_path / "world", "hall")
    assert set(world.rooms) == {"hall"}
    assert "haII.narrow.txt" in caplog.text
    assert "set by hand" not in caplog.text


# ---------------------------------------------------------------------------
# Everywhere a room renders, and live width
# ---------------------------------------------------------------------------

def test_walking_in_and_looking_agree_at_40(world):
    """One function renders every room view; asserted rather than assumed."""
    me = FakeSession(world, name="Corvid", width=40)
    me.clear()
    dispatch(me, ".go orchard")               # arrival auto-looks
    arrived = me.out[-1]
    me.clear()
    dispatch(me, ".look")
    assert arrived == me.out[-1]
    assert room_description(me, world.room("orchard")) in arrived


def test_arrival_and_look_agree_on_the_overridden_room(world):
    me = FakeSession(world, name="Corvid", width=40)
    me.resident.room_id = "gallery"
    me.clear()
    dispatch(me, ".go mirror hall")
    arrived = me.out[-1]
    me.clear()
    dispatch(me, ".look")
    assert arrived == me.out[-1]
    assert world.rooms["mirror_hall"].description_narrow in arrived


def test_rotating_the_phone_re_sets_the_next_view(tmp_path):
    """`width` is live, so nothing is snapshotted at login."""
    world = _house(tmp_path)
    me = FakeSession(world, name="Corvid", width=40)
    room = world.room("hall")
    narrow = room_description(me, room)
    me.width = 78
    assert room_description(me, room) == room.description
    me.width = 40
    assert room_description(me, room) == narrow


def test_every_shipped_description_fits_at_40(world):
    """The whole house, reflowed or overridden, comes out inside the width.

    Only the DESCRIPTION: the title, the roster and the `Exits:` line are
    generated, and the exits line is left to `Session.send` to wrap as it always
    was — out of scope here, deliberately.
    """
    for rid, room in world.rooms.items():
        me = FakeSession(world, name="Looker", width=40)
        out = room_description(me, room)
        assert all(len(l) <= 40 for l in out.split("\n")), (rid, out)


def test_the_title_and_exits_are_untouched(world):
    """Scope: the authored description only."""
    me = FakeSession(world, name="Corvid", width=40)
    block = _describe_room(me, me.resident)
    assert block.split("\n")[0] == "[ atrium ]"
    assert "Present company:" in block
    assert "Exits: " in block


# ---------------------------------------------------------------------
# Removed from the generic suite: each of these pins the authored ART of
# one particular house — the exact bytes of a map, a mirror-written room,
# a file of atmospheric lines. Another house's art is not a translation
# of that one, so there is nothing to rewrite them into. Every test of
# the MECHANISM around them is kept. (tools/build-generic.py)
del test_mirror_hall_ships_one_and_it_holds_the_mirror
del test_arrival_and_look_agree_on_the_overridden_room
del test_the_title_and_exits_are_untouched
