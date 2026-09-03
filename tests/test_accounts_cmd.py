# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.accounts` — the arch-only roster: name / level / created, online marker,
oldest-first, blank for a missing created date, ≤78 cols, and invisible below arch.

Deliberately no mailbox or profile data appears — that exclusion is a covenant
decision, so a test guards it.
"""

from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.config import Config

from conftest import FakeSession


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    store.create("Wren", "pw")
    store.create("Corvid", "pw")
    store.create("Mallow", "pw")
    # Fixed, distinct created dates so oldest-first ordering is unambiguous;
    # Mallow keeps no created date (an older/hand-edited record) to exercise the
    # blank-and-sort-first path.
    store.get("Generic").created = "2026-01-01"
    store.get("Wren").created = "2026-03-15"
    store.get("Corvid").created = "2026-06-30"
    store.get("Mallow").created = ""
    store.get("Wren").rank = "staff"
    return store


def _accounts_text(world, accounts, clock, online=()):
    # Bring the named accounts "online" by giving them a live session.
    for name in online:
        FakeSession(world, name=name, clock=clock, accounts=accounts)
    arch = FakeSession(world, name="Generic", clock=clock, accounts=accounts,
                       level="arch",
                       config=Config(wrap_columns=1000, owner_name="Generic"))
    arch.clear()
    dispatch(arch, ".accounts")
    return arch.text


def _rows(text):
    """Data rows: indented grid lines, excluding the 'Name  Rank  Created' header."""
    return [ln for ln in text.split("\n")
            if ln.startswith("  ") and not ln.strip().startswith("Name")]


def test_owner_sees_name_rank_created_and_footer(world, accounts, clock):
    text = _accounts_text(world, accounts, clock)
    assert text.startswith("*** Accounts *")
    # ⚠ The column is headed `Rank` (v1.61.2): it shows `theme.rank(...)`, and a
    # LEVEL is one of the four fixed bands, not a rung an operator named.
    assert "Name" in text and "Rank" in text and "Created" in text
    assert "Level" not in text
    # every account, its rank, and its created date are present
    for name in ("Generic", "Wren", "Corvid", "Mallow"):
        assert name in text
    # The Rank column shows this house's OWN words for the rungs.
    assert "staff" in text and "resident" in text and "owner" in text
    assert "wizard" not in text and "arch" not in text
    assert "2026-01-01" in text and "2026-06-30" in text
    # footer count (4 accounts in the store)
    assert "*** 4 accounts *" in text


def test_sorted_oldest_first_missing_created_first(world, accounts, clock):
    text = _accounts_text(world, accounts, clock)
    rows = _rows(text)
    names = [ln.split()[0].rstrip("*") for ln in rows]
    # Mallow (no date) sorts first with a blank; then the real dates ascending.
    assert names == ["Mallow", "Generic", "Wren", "Corvid"]
    # the missing-created row carries no date text, just name + level
    mallow_row = next(ln for ln in rows if ln.lstrip().startswith("Mallow"))
    assert "2026" not in mallow_row


def test_online_marker(world, accounts, clock):
    text = _accounts_text(world, accounts, clock, online=("Corvid",))
    rows = _rows(text)
    corvid = next(ln for ln in rows if ln.lstrip().startswith("Corvid"))
    assert "Corvid*" in corvid
    # Generic is the arch and also online, so it too is marked
    generic = next(ln for ln in rows if ln.lstrip().startswith("Generic"))
    assert "Generic*" in generic
    # an offline account gets no marker
    wren = next(ln for ln in rows if ln.lstrip().startswith("Wren"))
    assert "Wren*" not in wren


def test_all_lines_within_78_columns(world, accounts, clock):
    text = _accounts_text(world, accounts, clock, online=("Corvid", "Wren"))
    for ln in text.split("\n"):
        assert len(ln) <= 78, f"{len(ln)} cols: {ln!r}"


def test_no_mailbox_or_profile_data_leaks(world, accounts, clock):
    # The roster is deliberately not a dossier: no mail counts/sizes, no profiles.
    accounts.get("Wren").profile = "SECRETPROFILE"
    accounts.get("Wren").description = "SECRETDESC"
    text = _accounts_text(world, accounts, clock)
    assert "SECRETPROFILE" not in text
    assert "SECRETDESC" not in text
    for word in ("mail", "mailbox", "profile", "message"):
        assert word not in text.lower()


@pytest.mark.parametrize("level", ["resident", "wizard"])
def test_invisible_below_arch(world, accounts, clock, level):
    s = FakeSession(world, name="Wren", clock=clock, accounts=accounts, level=level)
    s.clear()
    dispatch(s, ".accounts")
    assert "Unknown command: .accounts" in s.text
    assert "*** Accounts *" not in s.text


def test_singular_footer_for_one_account(world, clock, tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Solo", "pw")
    arch = FakeSession(world, name="Solo", clock=clock, accounts=store, level="arch")
    arch.clear()
    dispatch(arch, ".accounts")
    assert "*** 1 account *" in arch.text
    assert "1 accounts" not in arch.text
