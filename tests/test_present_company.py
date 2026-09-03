# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.look`'s occupant roll — "Present company:".

The room's roll, and the shape of it is the point. It replaced a series of
announcements about other people —

    Corvid (a bird of considerable opinion) is here.
    Esoteric (given to cryptic remarks) is here.

— with a list you are ON:

    Present company:
       Corvid a bird of considerable opinion
       Esoteric given to cryptic remarks

Everyone in the room, yourself included, alphabetically, name then description.
No parentheses, no "is here", no trailing stop, and **no rank marking** — a
staff looks exactly like anyone else here; who holds what is an `.examine`
detail, not something the room advertises.

Every route that shows a room goes through the one function these test
(`_describe_room`), so walking in and typing `.look` cannot disagree.
"""

from pathlib import Path

import pytest

from conftest import FakeSession
from heresay.commands import dispatch
from heresay.commands._shared import _describe_room
from heresay.world import load_world

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"

HEADER = "Present company:"


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


def roll(session) -> list[str]:
    """The occupant block: the header and the indented lines under it."""
    block = _describe_room(session, session.resident)
    lines = block.split("\n")
    i = lines.index(HEADER)
    out = [HEADER]
    for ln in lines[i + 1:]:
        if not ln.strip():
            break
        out.append(ln)
    return out


def peopled(world, *people, width=78):
    """A watcher plus companions; returns the watcher."""
    me = FakeSession(world, name="Corvid", width=width)
    me.resident.description = "a bird of considerable opinion"
    for name, desc in people:
        other = FakeSession(world, name=name)
        other.resident.description = desc
    return me


# ---------------------------------------------------------------------------
# The shape
# ---------------------------------------------------------------------------

def test_the_header_is_exact(world):
    me = peopled(world, ("Esoteric", "given to cryptic remarks"))
    assert roll(me)[0] == "Present company:"


def test_you_are_on_the_list(world):
    """The change in one assertion: a roll you are on, not announcements about
    everyone else."""
    me = peopled(world, ("Esoteric", "given to cryptic remarks"))
    assert "   Corvid a bird of considerable opinion" in roll(me)


def test_the_line_is_indent_name_space_description(world):
    me = peopled(world, ("Esoteric", "given to cryptic remarks"))
    line = next(l for l in roll(me) if "Esoteric" in l)
    assert line == "   Esoteric given to cryptic remarks"
    assert line.startswith("   ")           # exactly three
    assert not line.startswith("    ")
    assert "  " not in line[3:]             # a SINGLE space after the name


def test_no_parentheses_no_is_here_no_trailing_stop(world):
    me = peopled(world, ("Esoteric", "given to cryptic remarks"),
                 ("Wren", "quiet"))
    for line in roll(me)[1:]:
        assert "(" not in line and ")" not in line, line
        assert "is here" not in line, line
        assert not line.rstrip().endswith("."), line


def test_sorted_alphabetically_case_insensitively(world):
    me = peopled(world, ("zed", "last"), ("Ada", "first"), ("mallory", "middle"))
    names = [l.strip().split(" ")[0] for l in roll(me)[1:]]
    assert names == ["Ada", "Corvid", "mallory", "zed"]
    assert names == sorted(names, key=str.lower)


def test_someone_with_no_description_is_just_their_name(world):
    me = peopled(world, ("Wren", ""))
    assert "   Wren" in roll(me)
    line = next(l for l in roll(me) if "Wren" in l)
    assert line == "   Wren"                # no trailing space, nothing stands in


def test_alone_you_are_still_listed(world):
    """No "you are alone" line — just a roll of one."""
    me = FakeSession(world, name="Corvid", width=78)
    me.resident.description = "a bird of considerable opinion"
    assert roll(me) == [HEADER, "   Corvid a bird of considerable opinion"]


def test_alone_with_no_description(world):
    me = FakeSession(world, name="Corvid", width=78)
    assert roll(me) == [HEADER, "   Corvid"]


def test_rank_is_never_marked(world):
    """A staff and the owner read exactly like a resident. Who holds what
    is `.examine`'s business."""
    me = FakeSession(world, name="Corvid", width=78)
    FakeSession(world, name="Wizzy", level="wizard")
    FakeSession(world, name="Boss", level="arch")
    text = "\n".join(roll(me))
    assert "   Wizzy" in text and "   Boss" in text
    for word in ("staff", "Staff", "owner", "wizard", "arch", "*"):
        assert word not in text, word


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------

LONG = ("keeps an improbably long and discursive description that will "
        "certainly need to wrap more than once at forty columns")


def test_a_long_description_wraps_with_a_hanging_indent(world):
    me = peopled(world, ("Zed", LONG), width=40)
    lines = roll(me)
    body = [l for l in lines[1:] if "Zed" in l or l.startswith("     ")]
    assert len(body) > 1, "the sample must actually wrap"
    assert body[0].startswith("   Zed ")
    for cont in body[1:]:
        assert cont.startswith("     "), cont      # hanging, not back to margin
    assert all(len(l) <= 40 for l in lines), lines


def test_the_roll_fits_the_session_width(world):
    for width in (40, 78):
        me = peopled(world, ("Zed", LONG), ("Wren", ""), width=width)
        assert all(len(l) <= width for l in roll(me)), (width, roll(me))


# ---------------------------------------------------------------------------
# Everywhere the room is shown
# ---------------------------------------------------------------------------

def test_look_and_walking_in_agree(world):
    """One function renders every room view, so `.go` and `.look` cannot drift
    apart. Asserted by comparing the two outputs directly."""
    me = peopled(world, ("Esoteric", "given to cryptic remarks"))
    me.clear()
    dispatch(me, ".look")
    looked = me.out[-1]
    me.clear()
    dispatch(me, ".go orchard")               # arrival auto-looks
    arrived = me.out[-1]
    me.clear()
    dispatch(me, ".look")
    looked_again = me.out[-1]
    assert HEADER in arrived
    assert arrived == looked_again, "walking in and looking must render alike"
    assert HEADER in looked


def test_the_rest_of_look_is_untouched(world):
    """Only the occupant block changed: the title, the description and the
    exits line are where they were."""
    me = peopled(world, ("Esoteric", "given to cryptic remarks"))
    block = _describe_room(me, me.resident)
    lines = block.split("\n")
    assert lines[0] == "[ atrium ]"
    assert lines[1] == ""
    assert "Exits: " in block
    # ...and the roll sits between the description and the exits.
    assert block.index(HEADER) < block.index("Exits: ")
    assert block.index("[ atrium ]") < block.index(HEADER)


def test_one_blank_line_separates_the_roll(world):
    me = peopled(world, ("Esoteric", "given to cryptic remarks"))
    block = _describe_room(me, me.resident)
    assert "\n\nPresent company:\n" in block
    assert "\n\n\n" not in block, "no doubled blanks introduced"
