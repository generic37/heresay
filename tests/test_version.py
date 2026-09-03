# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.version` — what is running, in one line, at every level.

The number lives in exactly one place (`heresay.__version__`); these tests read
it from there rather than repeating it, so a bump is a one-line change and not a
scavenger hunt.
"""

import re

import pytest

import heresay
from heresay.commands import NO_TOPIC, dispatch, help_text, unknown_command
from heresay.theme import DEFAULT_THEME as T

from conftest import FakeSession


def test_the_package_carries_a_release_version():
    assert heresay.__version__ == "1.63.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+", heresay.__version__)


@pytest.mark.parametrize("level", ["resident", "wizard", "arch"])
def test_version_is_one_line_at_every_level(world, clock, level):
    s = FakeSession(world, name="Corvid", clock=clock, level=level)
    s.clear()
    dispatch(s, ".version")
    assert s.out == [f"Commons, Heresay version {heresay.__version__}"]


def test_version_reads_the_package_not_a_copy(world, clock, monkeypatch):
    """One source of truth: change the package's number and the line follows."""
    monkeypatch.setattr("heresay.commands.info.__version__", "9.9.9")
    s = FakeSession(world, name="Corvid", clock=clock)
    s.clear()
    dispatch(s, ".version")
    assert s.out == ["Commons, Heresay version 9.9.9"]


def test_version_ignores_arguments(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    s.clear()
    dispatch(s, ".version please")
    assert s.out == [f"Commons, Heresay version {heresay.__version__}"]


# ---- where it appears -----------------------------------------------------

def test_version_sits_in_the_administrative_section_of_both_forms():
    long_form = help_text(T)
    # The administrative section: the rows between the account section and the
    # misc one, separated by blank lines like every other group. `.agreement`
    # joined them in v1.36.1 and `.source` in v1.50.0 — the section is named
    # here rather than merely counted, so a new row is a deliberate edit and not
    # a silent one.
    #
    # ⚠ `.source` sits beside `.version` because it answers the other half of
    # the same question: `.version` says WHAT is running, `.source` says where
    # to get it. The AGPL cares about the pair, not either alone.
    groups = long_form.split("\n\n")
    admin = next(g for g in groups if g.startswith(".reboot"))
    rows = admin.split("\n")
    assert [r.split()[0] for r in rows] == [
        ".reboot", ".version", ".source", ".agreement"]
    assert "server version" in admin

    assert ("administrative  .reboot, .version, .source, .agreement"
            in help_text(T, "short"))


def test_the_version_page_ships_and_says_what_the_number_means(world, clock):
    from heresay.server import _load_help_topics
    from pathlib import Path

    topics = _load_help_topics(Path(__file__).resolve().parent / "fixtures" / "house" / "help")
    s = FakeSession(world, name="Corvid", clock=clock, help_topics=topics)
    dispatch(s, ".help version")
    out = s.text
    assert out.lstrip("\n").startswith("*** .help version *")
    assert "what is running" in out
    assert "fixes bump the last digit" in out
    assert "covenant itself the first" in out
    # It is a page like any other: framed, one unit, inside 78 columns.
    assert len(s.out) == 1
    assert all(len(ln) <= 78 for ln in out.split("\n"))


def test_a_version_typo_is_a_typo_not_a_missing_page(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock, help_topics={})
    dispatch(s, ".help verison")
    assert s.out[-1] == unknown_command("verison")
    s.clear()
    dispatch(s, ".help version")                        # real verb, no pages loaded
    assert s.out[-1] == NO_TOPIC
