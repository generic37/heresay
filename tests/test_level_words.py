# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The four LEVELS' display words, and the one rule the engine owes them.

⛔ **A level word is a NOUN THE OPERATOR CHOSE, and the engine never conjugates
it.** The shipped staff level is `staff` / `staff` (v1.61.0) — a MASS noun, so
`a {staff_word}` reads "a staff" and is not English. Every engine string that
names a level therefore sits in a position that works for a mass noun and a count
noun alike: `the {plural}`, `{singular} mail`, `by the {plural}`.

⚠ The sweep below is the rule rather than a spot check: it drives every command
that names a level under a theme whose words are the shipped defaults, and fails
on an article in front of one. A string added next year is caught by the same
pass, which is the whole reason it is written as a sweep.

⚠ The RANK words go the same way for the same reason: the shipped ladder's middle
rung is `staff` too, and `.promote`/`.examine` used to say "a {rank}".
"""

import re
from pathlib import Path

import pytest

from heresay.commands import dispatch, staff_footer, help_text
from heresay.theme import (DEFAULT_LEVEL_DISPLAY, DEFAULT_THEME, Theme,
                           load_theme)

from conftest import FakeSession

SKEL = Path(__file__).resolve().parent.parent / "skel"

# "a staff", "an staff", "A staff", capital or not, on a word boundary. This is
# the shape the whole file exists to forbid.
ARTICLE = re.compile(r"\b[Aa]n?\s+(staff|owner|resident|newb)\b")

# ⚠ The sweeps below run against a theme that SPELLS the mass noun out rather
# than against `DEFAULT_THEME`, deliberately: the rule is about the POSITION a
# level word is put in, and a test that only bites while the defaults happen to
# be mass nouns would go quiet the day somebody re-themed the fixture. Written
# this way it also fails on the pre-v1.61.0 engine, which is what proved it.
MASS = Theme(order=("resident", "staff", "owner"), staff="staff",
             level_display={"newb": ("newb", "newbs"),
                            "resident": ("resident", "residents"),
                            "staff": ("staff", "staff"),
                            "owner": ("owner", "owners")})


# ---------------------------------------------------------------------------
# 1. The shipped words
# ---------------------------------------------------------------------------

def test_the_staff_level_ships_as_staff_singular_AND_plural():
    """⛔ The maintainer's call, not open: `staffer` is gone. One person is
    staff, several people are staff, and the two fields hold the same word
    rather than the engine inventing "staffs"."""
    assert DEFAULT_LEVEL_DISPLAY["staff"] == ("staff", "staff")
    assert (DEFAULT_THEME.staff_word, DEFAULT_THEME.staff_plural) == \
        ("staff", "staff")


def test_the_plural_of_a_default_word_is_not_the_naive_one():
    """⚠ The path that actually matters: a house with NO `[levels]` block at all
    takes its level word from the anchor RANK, and the shipped ladder's staff
    rung is named `staff`. The naive fallback would say "staffs" — so the engine
    knows the plurals of its own default words, and only of those."""
    bare = Theme()                      # no level_display whatsoever
    assert bare.staff_plural == "staff"
    # An operator's own word is NOT a default, so it still pluralises naively.
    staff = Theme(order=("resident", "staff", "owner"), staff="staff")
    assert staff.staff_plural == "staff"
    # ...and an explicit plural still wins over both.
    odd = Theme(order=("resident", "staff", "owner"), staff="staff",
                level_display={"staff": ("staff", "the watch")})
    assert odd.staff_plural == "the watch"


def _skeleton_theme():
    """`skel/house/theme.toml` here; `house/theme.toml` in the built tree.

    ⚠ The skeleton IS the house on the other side — `skel/` is a build SOURCE
    and does not ship — so the file this test is about lives at two paths and
    the test asks the tree which one."""
    a = SKEL / "house" / "theme.toml"
    return a if a.exists() else SKEL.parent / "house" / "theme.toml"


def test_the_skeleton_spells_all_four_levels_out():
    """⚠ Every value in `skel/house/theme.toml` matches the built-in default, so
    the block changes nothing — it is there to make the mechanism VISIBLE. Until
    v1.61.0 an operator met it for the first time when something read wrong."""
    text = _skeleton_theme().read_text(encoding="utf-8")
    for level in ("newb", "resident", "staff", "owner"):
        assert f"[levels.{level}]" in text, level
    t = load_theme(_skeleton_theme())
    for level, (singular, plural) in DEFAULT_LEVEL_DISPLAY.items():
        assert (t.level_singular(level), t.level_plural(level)) == \
            (singular, plural), level
    # The ladder names its rungs after the levels they anchor — legal, and the
    # shape the docs describe.
    assert t.order == ("resident", "staff", "owner")


# ---------------------------------------------------------------------------
# 2. The rule: no article, anywhere
# ---------------------------------------------------------------------------

def _generic(world, clock, **kw):
    return FakeSession(world, clock=clock, theme=MASS, **kw)


def test_no_engine_string_puts_an_article_before_a_level_word(world, clock, tmp_path):
    """⚠ Drives the commands that name a level and reads every line they emit.

    The gag/jail/ungag room lines, the `.call_staff` family, the jail and gag
    refusals, and the promote/demote confirmations — under the shipped words,
    where an article would read "a staff"."""
    from heresay.state import StateStore
    from heresay.accounts import AccountStore

    state = StateStore(tmp_path / "state.json")
    accounts = AccountStore(tmp_path / "accounts.json", owner_name="Owner")
    accounts.create("Bob", "correct horse battery")
    accounts.create("Sam", "correct horse battery")
    accounts.set_rank("Sam", "staff")

    sam = _generic(world, clock, name="Sam", rank="staff", state=state,
                   accounts=accounts)
    bob = _generic(world, clock, name="Bob", state=state, accounts=accounts)
    obs = _generic(world, clock, name="Obs", state=state, accounts=accounts)

    dispatch(sam, ".gag Bob")
    dispatch(bob, ".tell Obs a secret")     # gagged: the refusal names the level
    dispatch(sam, ".ungag Bob")
    dispatch(sam, ".jail Bob")
    dispatch(sam, ".free Bob")
    dispatch(bob, ".call_staff")            # the usage line
    dispatch(bob, ".call_staff the roof is on fire")
    dispatch(bob, ".examine Sam")           # the rank line
    dispatch(sam, ".help")

    for who in (sam, bob, obs):
        hit = ARTICLE.search(who.text)
        assert hit is None, (who.resident.name, hit.group(0), who.text)


def test_the_footer_and_the_help_descriptions_carry_no_article():
    text = staff_footer(MASS) + "\n" + help_text(MASS, "long", "owner")
    hit = ARTICLE.search(text)
    assert hit is None, (hit.group(0) if hit else "", text)


def test_promote_and_demote_do_not_conjugate_a_rank(world, clock, tmp_path):
    """⚠ RANK words, not level words — and the same rule, because the shipped
    ladder's middle rung is `staff` and an operator may name a rung anything."""
    from heresay.accounts import AccountStore
    accounts = AccountStore(tmp_path / "accounts.json", owner_name="Owner")
    accounts.create("Wren", "correct horse battery")
    owner = _generic(world, clock, name="Owner", rank="owner", accounts=accounts)
    wren = _generic(world, clock, name="Wren", accounts=accounts)

    dispatch(owner, ".promote Wren")
    assert "Wren's rank is now staff." in owner.text
    assert "Your rank is now staff." in wren.text
    owner.clear(); wren.clear()
    dispatch(owner, ".demote Wren")
    assert "Wren's rank is now resident." in owner.text
    for who in (owner, wren):
        assert ARTICLE.search(who.text) is None, who.text


# ---------------------------------------------------------------------------
# 3. A LEVEL word where a RANK word used to stand
# ---------------------------------------------------------------------------

@pytest.fixture
def split_theme():
    """A house whose staff RANK and staff LEVEL word are deliberately different
    words, so a string using the wrong one is visible."""
    return Theme(order=("citizen", "warden", "Steward"), staff="warden",
                 level_display={"staff": ("marshal", "marshals"),
                                "owner": ("Steward", "Stewards")})


def test_the_staff_letterbox_names_the_LEVEL_not_the_anchor_rank(
        world, clock, tmp_path, split_theme):
    """⛔ `.staffmail` / `.dstaffmail` are read by every account at the staff
    LEVEL and above, whatever rung they hold — so naming the anchor RUNG
    described the wrong set of people. It said "Warden mail" where the house
    calls those people marshals."""
    from heresay.mail import MailStore
    from heresay.commands import STAFF_BOX

    mail = MailStore(tmp_path / "mail.json", tmp_path / "mail.key")
    mail.deliver(STAFF_BOX, "Cit", "the tap is running")
    sam = FakeSession(world, name="Sam", clock=clock, rank="warden",
                      theme=split_theme, mail=mail)
    dispatch(sam, ".staffmail")
    assert "Marshal mail" in sam.text and "Warden mail" not in sam.text
    sam.clear()
    dispatch(sam, ".dstaffmail 1")
    assert "Marshal message 1 cleared for all." in sam.text


def test_call_staff_names_the_LEVEL_not_the_raw_anchor(
        world, clock, split_theme):
    """⚠ Both confirmations used to read `a {theme.staff}` — the raw `[ranks]
    staff` value, a RANK name, in an article slot."""
    cit = FakeSession(world, name="Cit", clock=clock, theme=split_theme)
    dispatch(cit, ".call_staff the tap is running")
    assert "No marshals awake just now." in cit.text
    assert "warden" not in cit.text.lower()

    # The rate refusal is the other half of the pair, and named the raw anchor
    # in the same way.
    cit.clear()
    dispatch(cit, ".call_staff again please")
    assert "you called the marshals only moments ago" in cit.text

    sam = FakeSession(world, name="Sam", clock=clock, rank="warden",
                      theme=split_theme)
    clock.t += 3600                      # past the call bucket's refill
    cit.clear()
    dispatch(cit, ".call_staff the tap is still running")
    assert "Your call has gone out to the marshals." in cit.text
    assert "Cit (atrium) calls for the marshals:" in sam.text
