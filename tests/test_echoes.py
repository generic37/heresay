# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Atmospherics — the entry room murmurs after a stretch of occupied quiet.

Lines come from world/echoes.txt; the silence gate, occupancy rule, no-repeat
guard and .rev entry all live in World.maybe_echo / note_entry_speech, driven by
an injected clock and rng. Pure room life: never on disk, never logged.
"""

import random
from pathlib import Path

import pytest

from heresay.commands import dispatch
from heresay.world import ECHO_SPEAKER, load_world

from conftest import FakeSession

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"
ECHOES = WORLD / "echoes.txt"


def _fixed(world, lines, interval=600.0, seed=1):
    """A world wired for deterministic echoes: fixed interval, seeded rng."""
    world.echoes = list(lines)
    world.echo_min = world.echo_max = interval
    world._echo_rng = random.Random(seed)
    return world


def _seed(world, clock):
    """The first tick after a room becomes occupied establishes the quiet clock
    (an arrival never walks into an instant echo). Mirror that in tests."""
    assert world.maybe_echo(clock.t) is None


# ---- the silence gate + occupancy ----------------------------------------

def test_fires_only_after_the_drawn_quiet_interval_with_occupants(world, clock):
    _fixed(world, ["a murmur", "a whisper"], interval=600.0)
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    world.note_entry_speech("atrium", clock.t)      # start the quiet clock
    clock.t += 599
    assert world.maybe_echo(clock.t) is None             # not yet
    clock.t += 2
    assert world.maybe_echo(clock.t) in world.echoes     # now


def test_never_fires_in_an_empty_room_and_records_nothing(world, clock):
    _fixed(world, ["a murmur"], interval=1.0)             # would fire instantly if occupied
    for t in range(0, 5000, 100):                         # advance with zero occupants
        clock.t = t
        assert world.maybe_echo(clock.t) is None
    assert world.review == []                             # and nothing entered .rev


def test_an_arrival_does_not_walk_into_an_immediate_echo(world, clock):
    """The clock is held at `now` while empty, so a long-empty room doesn't fire
    the instant someone shows up."""
    _fixed(world, ["a murmur"], interval=600.0)
    for t in range(0, 100000, 500):                       # empty for a long while
        clock.t = t
        world.maybe_echo(clock.t)
    a = FakeSession(world, name="Ada", room="atrium", clock=clock)
    assert world.maybe_echo(clock.t) is None              # not immediately
    clock.t += 601
    assert world.maybe_echo(clock.t) in world.echoes      # only after a quiet interval


def test_resident_speech_resets_the_gate(world, clock):
    _fixed(world, ["a murmur"], interval=600.0)
    a = FakeSession(world, name="Ada", room="atrium", clock=clock)
    world.note_entry_speech("atrium", clock.t)
    clock.t += 590
    dispatch(a, "still here")                             # a say resets the clock
    clock.t += 590                                        # 590 < 600 since the say
    assert world.maybe_echo(clock.t) is None
    clock.t += 20
    assert world.maybe_echo(clock.t) in world.echoes      # fires ~600s after the say


def test_speech_in_another_room_does_not_touch_the_gate(world, clock):
    _fixed(world, ["a murmur"], interval=600.0)
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    tree = FakeSession(world, name="Bob", room="oak", clock=clock)
    world.note_entry_speech("atrium", clock.t)
    clock.t += 599
    dispatch(tree, "chatter under the oak")             # not the entry room
    clock.t += 2
    assert world.maybe_echo(clock.t) in world.echoes      # the entry gate is unaffected


# ---- the line: no immediate repeat, from the file --------------------------

def test_no_immediate_repeat(world, clock):
    _fixed(world, ["one", "two", "three"], interval=600.0, seed=3)
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    _seed(world, clock)
    last = None
    for _ in range(20):
        clock.t += 601
        line = world.maybe_echo(clock.t)
        assert line is not None and line != last          # never twice running
        last = line


def test_a_single_echo_line_still_fires(world, clock):
    _fixed(world, ["the only murmur"], interval=600.0)
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    _seed(world, clock)
    clock.t += 601
    assert world.maybe_echo(clock.t) == "the only murmur"


def test_no_echoes_file_is_silent(world, clock):
    world.echoes = []                                     # absent/empty file
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    clock.t += 100000
    assert world.maybe_echo(clock.t) is None
    assert world.review == []


# ---- it enters .rev, fades, is not "speech" -------------------------------

def test_the_echo_enters_rev_and_fades_like_anything(world, clock):
    _fixed(world, ["a distant bell"], interval=600.0)
    world.review_window = 1800.0
    a = FakeSession(world, name="Ada", room="atrium", clock=clock)
    _seed(world, clock)
    clock.t += 601
    world.maybe_echo(clock.t)                             # fires and records
    assert "a distant bell" in world.review_lines(a.resident, clock.t)
    clock.t += 1801                                       # past the review window
    assert "a distant bell" not in world.review_lines(a.resident, clock.t)


def test_the_echo_does_not_reset_its_own_gate(world, clock):
    """An echo firing does not count as speech: continued silence brings the
    next one exactly one interval later, not one interval after 'someone spoke'."""
    _fixed(world, ["one", "two"], interval=600.0)
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    _seed(world, clock)
    clock.t += 601
    assert world.maybe_echo(clock.t) is not None          # first fires
    clock.t += 601
    assert world.maybe_echo(clock.t) is not None          # and the next, an interval on


def test_the_echo_speaker_cannot_be_muted_by_any_resident(world, clock):
    """The echo's stored speaker is a sentinel that is not, and cannot be, a real
    account name — so no `.mute` can ever hide the room's own murmur, however many
    people a viewer has muted."""
    from heresay.accounts import valid_name
    assert not valid_name(ECHO_SPEAKER)                    # can never be a real name
    _fixed(world, ["a murmur"], interval=600.0)
    a = FakeSession(world, name="Ada", room="atrium", clock=clock)
    b = FakeSession(world, name="Bob", room="atrium", clock=clock)
    a.resident.muted = {"bob"}                             # mute the only real co-speaker
    _seed(world, clock)
    clock.t += 601
    world.maybe_echo(clock.t)
    assert "a murmur" in world.review_lines(a.resident, clock.t)


# ---- the shipped file ------------------------------------------------------

def test_shipped_echoes_are_100_lines_all_within_78_columns():
    lines = ECHOES.read_text(encoding="utf-8").split("\n")
    lines = [ln for ln in lines if ln]                    # drop the trailing empty
    assert len(lines) == 100
    for ln in lines:
        assert len(ln) <= 78, (len(ln), ln)


def test_a_distinctive_shipped_line_is_present(world, clock):
    from heresay.server import _load_echoes
    echoes = _load_echoes(ECHOES)
    world.echoes = echoes
    assert 'a draught moves through the room and settles' in echoes
    world.echo_min = world.echo_max = 600.0
    world._echo_rng = random.Random(0)
    FakeSession(world, name="Ada", room="atrium", clock=clock)
    _seed(world, clock)
    clock.t += 601
    assert world.maybe_echo(clock.t) in echoes


# ---------------------------------------------------------------------
# Removed from the generic suite: each of these pins the authored ART of
# one particular house — the exact bytes of a map, a mirror-written room,
# a file of atmospheric lines. Another house's art is not a translation
# of that one, so there is nothing to rewrite them into. Every test of
# the MECHANISM around them is kept. (tools/build-generic.py)
del test_shipped_echoes_are_100_lines_all_within_78_columns
del test_a_distinctive_shipped_line_is_present
