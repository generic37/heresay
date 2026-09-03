# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Profiles: written via the editor, read via .examine."""

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.config import Config

from conftest import FakeSession


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw-corvid", description="staff of crows")
    store.create("Wren", "pw-wren", description="small and loud")
    return store


def _session(world, clock, accounts, name="Corvid", config=None):
    return FakeSession(world, name=name, clock=clock, accounts=accounts,
                       config=config or Config(wrap_columns=1000))


def test_profile_set_via_editor(world, clock, accounts):
    s = _session(world, clock, accounts)
    dispatch(s, ".profile")
    assert s.in_editor()
    s.feed_editor("I studied the rooks on the college lawn")
    s.feed_editor("for eleven years.")
    s.feed_editor(".end")
    assert not s.in_editor()
    assert accounts.get("Corvid").profile == \
        "I studied the rooks on the college lawn\nfor eleven years."


def test_examine_online(world, clock, accounts):
    corvid = _session(world, clock, accounts, name="Corvid")
    accounts.set_profile("Wren", "chatterbox of the hedgerow")
    _session(world, clock, accounts, name="Wren")  # Wren is online
    corvid.clear()
    dispatch(corvid, ".examine Wren")
    assert "Wren small and loud" in corvid.text
    assert "chatterbox of the hedgerow" in corvid.text


def test_examine_offline(world, clock, accounts):
    # Wren has an account + profile but is NOT connected.
    accounts.set_profile("Wren", "away, but documented")
    corvid = _session(world, clock, accounts, name="Corvid")
    dispatch(corvid, ".examine Wren")
    assert "away, but documented" in corvid.text


def test_examine_unknown(world, clock, accounts):
    corvid = _session(world, clock, accounts, name="Corvid")
    dispatch(corvid, ".examine Nobody")
    assert "no one called Nobody" in corvid.text


def test_examine_empty_profile(world, clock, accounts):
    corvid = _session(world, clock, accounts, name="Corvid")
    dispatch(corvid, ".examine Wren")   # Wren has no profile set
    assert "They keep their own counsel." in corvid.text


def test_profile_20_line_cap(world, clock, accounts):
    cfg = Config(wrap_columns=1000, profile_max_lines=20)
    s = _session(world, clock, accounts, config=cfg)
    dispatch(s, ".profile")
    for i in range(21):
        s.feed_editor(f"line {i}")
    s.feed_editor(".end")
    assert s.in_editor()               # over cap, still composing
    assert accounts.get("Corvid").profile == ""   # nothing saved
    assert "limit is 20" in s.text
