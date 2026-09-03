# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Case-insensitive name matching across commands, with display case preserved."""

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.config import Config
from heresay.mail import MailStore

from conftest import FakeSession


def test_tell_different_case_and_display_preserved(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".tell wren meet me")          # lowercase target
    assert ">> Corvid says: meet me" in b.text   # sender display preserved
    assert ">> Corvid says: meet me" in a.text   # sender sees the identical line
    # The delivered line no longer names the target, so the display-capitalization
    # promise is checked where it still shows: the correspondent slot.
    assert a.resident.correspondent == "Wren"


def test_tell_no_longer_accepts_a_prefix(world, clock):
    """⛔ INVERTED in v1.52.0 — this test used to assert the opposite.

    `.tell wr hello` delivered to Wren. It does not any more: a mis-resolved
    name is a private message read by somebody it was never addressed to, and
    that has no undo. Case is still free; abbreviation is not.
    """
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".tell wr hello")
    assert ">> Corvid says: hello" not in b.text
    # ...and with no correspondent yet, nothing is sent anywhere.
    assert not b.text


def test_a_prefix_that_used_to_be_ambiguous_is_simply_not_a_name(world, clock):
    """⚠ The ambiguity branch is now unreachable — an exact casefold key is
    unique by construction, so `resolve_resident` can no longer return True.
    Two people whose names share a prefix are just two people; `wr` is nobody.
    """
    a = FakeSession(world, name="Corvid", clock=clock)
    w1 = FakeSession(world, name="Wren", clock=clock)
    w2 = FakeSession(world, name="Wraith", clock=clock)
    dispatch(a, ".tell wr hello")
    assert "Be more specific" not in a.text
    assert not w1.text and not w2.text          # and nobody was written to


def test_mute_different_case(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(b, ".mute CORVID")                # uppercase
    b.clear()
    dispatch(a, "you should not hear this")
    assert "you should not hear this" not in b.text
    dispatch(b, ".unmute corvid")              # lowercase unmute still matches
    b.clear()
    dispatch(a, "now you can")
    assert "Corvid says: now you can" in b.text


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw", description="the archetype")
    store.create("Wren", "pw", description="")
    return store


def test_examine_takes_any_case_but_not_a_prefix(world, accounts, clock):
    s = FakeSession(world, name="Wren", clock=clock, accounts=accounts)
    dispatch(s, ".examine generic")            # lowercase
    assert "Generic the archetype" in s.text
    s.clear()
    s.clear()
    dispatch(s, ".examine GENERIC")            # exact, different case
    assert "Generic the archetype" in s.text
    s.clear()
    dispatch(s, ".examine GEN")                # ⛔ a prefix no longer resolves
    assert "Generic the archetype" not in s.text
    assert "no one called" in s.text.lower()


def test_mail_different_case_targets_canonical_box(world, accounts, tmp_path, clock):
    mail = MailStore(tmp_path / "mb.json", tmp_path / "mk.key")
    sender = FakeSession(world, name="Wren", clock=clock, accounts=accounts, mail=mail)
    dispatch(sender, ".mail GENERIC hello there")   # uppercase target
    assert "Mail sent to Generic." in sender.text    # canonical display name
    # Delivered to the one canonical mailbox, readable in any case.
    assert mail.has_mail("generic") and mail.count("Generic") == 1
