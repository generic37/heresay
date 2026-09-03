# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ban store + the .ban/.unban commands (connection/login refusal is integration)."""

from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.bans import BanStore
from heresay.commands import dispatch

from conftest import FakeSession


def _bans(tmp_path, wall=0.0):
    return BanStore(tmp_path / "bans.json", wall_clock=lambda: wall)


# ---- store ---------------------------------------------------------------

def test_ban_name_casefold_and_expiry(tmp_path):
    b = _bans(tmp_path, wall=1000.0)
    b.ban("Generic", "name", 1.0, now=1000.0)     # one hour
    assert b.is_banned_name("generic", now=1000.0)      # casefold match
    assert b.is_banned_name("GENERIC", now=1000.0 + 3599)
    assert not b.is_banned_name("Generic", now=1000.0 + 3601)   # expired


def test_ban_ip(tmp_path):
    b = _bans(tmp_path)
    b.ban("1.2.3.4", "ip", 24, now=0.0)
    assert b.is_banned_ip("1.2.3.4", now=0.0)
    assert not b.is_banned_ip("5.6.7.8", now=0.0)


def test_ban_persists_across_reload(tmp_path):
    b = _bans(tmp_path)
    b.ban("Bob", "name", 24, now=0.0)
    b2 = BanStore(tmp_path / "bans.json", wall_clock=lambda: 0.0)
    assert b2.is_banned_name("bob", now=0.0)


def test_unban(tmp_path):
    b = _bans(tmp_path)
    b.ban("Bob", "name", 24, now=0.0)
    assert b.unban("Bob", "name")
    assert not b.is_banned_name("bob", now=0.0)
    assert not b.unban("Bob", "name")   # already gone


def test_expired_bans_purged_on_load(tmp_path):
    b = BanStore(tmp_path / "bans.json", wall_clock=lambda: 0.0)
    b.ban("Old", "name", 1, now=0.0)    # until = 3600
    reloaded = BanStore(tmp_path / "bans.json", wall_clock=lambda: 10_000.0)
    assert not reloaded.is_banned_name("Old", now=10_000.0)
    assert reloaded.list_active(now=10_000.0) == []


# ---- commands ------------------------------------------------------------

def test_ban_command_by_name_boots_target(world, tmp_path, clock):
    accounts = AccountStore(tmp_path / "a.json")
    accounts.create("Bad", "pw")
    bans = _bans(tmp_path, wall=100.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, accounts=accounts, bans=bans,
                      level="wizard", wall_clock=lambda: 100.0)
    bad = FakeSession(world, name="Bad", clock=clock, accounts=accounts, bans=bans)
    dispatch(wiz, ".ban Bad 24")
    assert bans.is_banned_name("Bad", now=100.0)
    assert bad._closing                     # connected target booted on ban


def test_ban_no_longer_takes_an_ip(world, tmp_path, clock):
    """v1.32.0 split the command. `.ban` used to guess from the shape of its
    argument, which put two quite different powers behind one word: banning a
    person, and banning an address that may hold a household. An address given
    to `.ban` is now refused with a pointer, not silently obeyed."""
    bans = _bans(tmp_path, wall=100.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, bans=bans, level="wizard",
                      wall_clock=lambda: 100.0)
    wiz.clear()
    dispatch(wiz, ".ban 9.9.9.9")
    assert not bans.is_banned_ip("9.9.9.9", now=100.0)
    assert ".ban_ip" in wiz.text


def test_ban_default_hours(world, tmp_path, clock):
    bans = _bans(tmp_path, wall=0.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, bans=bans, level="wizard",
                      wall_clock=lambda: 0.0)
    dispatch(wiz, ".ban Bob")               # no hours -> default 24
    assert bans.is_banned_name("Bob", now=23 * 3600)
    assert not bans.is_banned_name("Bob", now=25 * 3600)


def test_unban_command(world, tmp_path, clock):
    """`.unban` still takes EITHER — an operator must be able to lift an address
    ban, and the address is what `bans.json` and the ban message give them. Only
    the placing of a ban was split (v1.32.0), not the lifting of one.

    (The ban is placed on the store directly: `.ban` no longer accepts an
    address, and going through `.ban_ip` would need a target account and a y/n.
    Driving it through the refused path would have made this test pass without
    ever exercising `.unban`.)
    """
    bans = _bans(tmp_path, wall=0.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, bans=bans, level="wizard",
                      wall_clock=lambda: 0.0)
    bans.ban("9.9.9.9", "ip", hours=24, now=0.0)
    assert bans.is_banned_ip("9.9.9.9", now=0.0)
    dispatch(wiz, ".unban 9.9.9.9")
    assert not bans.is_banned_ip("9.9.9.9", now=0.0)

    dispatch(wiz, ".ban Bob")
    assert bans.is_banned_name("Bob", now=0.0)
    dispatch(wiz, ".unban Bob")
    assert not bans.is_banned_name("Bob", now=0.0)
