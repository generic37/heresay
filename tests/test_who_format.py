# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.who` — NUTS-style wholist: dated header, sentence rows (name + desc, or
"has no description"), aligned room/idle columns, a total footer; rows ≤ 78."""

import re

from heresay.commands import dispatch

from conftest import FakeSession


def _who_lines(session):
    dispatch(session, ".who")
    return session.text.split("\n")


def _user_rows(session):
    # User rows are the ones carrying the idle field (not header/footer/blank).
    return [ln for ln in _who_lines(session) if "idle" in ln]


def test_header_and_footer_shape(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    a.clear()
    lines = _who_lines(a)
    # The header is now a full-width rule with the date set into it.
    assert re.match(r"^\*\*\* Current users on .+ \*+$", lines[0])
    assert len(lines[0]) == 78
    assert lines[1] == ""                                   # blank after header
    assert lines[-1].startswith("*** Total of 1 user logged on *")  # singular
    assert len(lines[-1]) == 78
    assert lines[-2] == ""                                   # blank before footer


def test_footer_plural(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    FakeSession(world, name="Wren", clock=clock)
    a.clear()
    assert _who_lines(a)[-1].startswith("*** Total of 2 users logged on *")


def test_name_space_desc_no_dash(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    a.resident.description = "staff of crows"
    a.clear()
    rows = _user_rows(a)
    assert len(rows) == 1
    # leading space, name, single space, desc — reads as a sentence, no dash.
    assert rows[0].startswith(" Corvid staff of crows")
    assert "—" not in rows[0] and " - " not in rows[0]


def test_empty_desc_says_has_no_description(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)   # no description
    a.clear()
    rows = _user_rows(a)
    assert " Corvid has no description" in rows[0]
    assert rows[0].rstrip() == rows[0]                   # no trailing whitespace
    assert "atrium" in rows[0] and "idle" in rows[0]


def test_long_name_and_desc_stay_within_78(world, clock):
    a = FakeSession(world, name="Quartermaster16c", clock=clock)
    a.resident.description = ("a very long description that would happily run off "
                              "the right edge of any sensible terminal if we let it, "
                              "and then some more words for good measure")
    a.clear()
    rows = _user_rows(a)
    assert all(len(r) <= 78 for r in rows), [len(r) for r in rows]
    assert "a very long description" in rows[0]           # present but truncated
    assert rows[0].rstrip() == rows[0]


def test_room_and_idle_columns_align(world, clock):
    a = FakeSession(world, name="Al", clock=clock)                 # short name
    a.resident.description = "short"
    b = FakeSession(world, name="Quartermaster", clock=clock)       # long name
    b.resident.description = "longer one"
    a.clear()
    rows = _user_rows(a)
    # The room column starts at the same offset on every row (aligned).
    starts = [r.index("atrium") for r in rows]
    assert len(rows) == 2 and len(set(starts)) == 1, (rows, starts)


def test_afk_marker_and_desc_together(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    a.resident.description = "staff of crows"
    dispatch(a, ".afk")
    a.clear()
    rows = _user_rows(a)
    assert "Corvid (AFK)" in rows[0]
    assert "staff of crows" in rows[0]
    assert len(rows[0]) <= 78
