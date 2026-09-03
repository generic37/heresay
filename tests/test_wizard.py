# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Staff tier: gag, jail, mailpurge (boot/ban disconnects are integration-tested).

⚠ The FILE keeps its old name deliberately — it is a developer-facing
identifier, like the `level="wizard"` fake-session kwarg it uses, and
renaming it is churn nobody reads. The wording pass (v1.45.0) is about what
a PERSON reads; see tests/test_wording.py for the line.
"""

from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.mail import MailStore
from heresay.world import load_world

from conftest import FakeClock, FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _jail_world():
    return load_world(WORLD, "atrium", "holding_cell")


# ---- gag -----------------------------------------------------------------

def test_gag_blocks_all_verbs_and_is_visible(world, clock):
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    obs = FakeSession(world, name="Obs", clock=clock)
    dispatch(wiz, ".gag Bob")
    assert bob.resident.gagged
    assert "Bob has been silenced by the staff." in obs.text   # visible to the room (themed)

    obs.clear()
    dispatch(bob, "hello there")
    dispatch(bob, ".e waves")
    dispatch(bob, ".think hmm")
    dispatch(bob, ".shout hey all")
    assert "Bob says: hello there" not in obs.text
    assert "waves" not in obs.text
    assert "oO(" not in obs.text
    assert "shouts" not in obs.text


def test_gag_tell_to_wizard_allowed_to_resident_blocked(world, clock):
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    res = FakeSession(world, name="Res", clock=clock)
    dispatch(wiz, ".gag Bob")
    wiz.clear()
    dispatch(bob, ".tell Wiz please help")
    assert ">> Bob says: please help" in wiz.text        # appeal channel works
    dispatch(bob, ".tell Res a secret")
    assert ">> Bob says: a secret" not in res.text        # blocked to a resident
    assert "only the staff" in bob.text.lower()


def test_ungag_restores_voice(world, clock):
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    obs = FakeSession(world, name="Obs", clock=clock)
    dispatch(wiz, ".gag Bob")
    dispatch(wiz, ".ungag Bob")
    assert not bob.resident.gagged
    obs.clear()
    dispatch(bob, "back again")
    assert "Bob says: back again" in obs.text


# ---- jail ----------------------------------------------------------------

def test_jail_moves_and_can_talk_inside():
    world = _jail_world()
    clock = FakeClock()
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    cell = FakeSession(world, name="Cell", room="holding_cell", clock=clock)
    dispatch(wiz, ".jail Bob")
    assert bob.resident.room_id == "holding_cell"
    assert "jailed" in bob.text.lower()
    cell.clear()
    dispatch(bob, "let me out")
    assert "Bob says: let me out" in cell.text   # the jail is still a room


def test_jail_has_no_exit_and_is_unreachable():
    world = _jail_world()
    clock = FakeClock()
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    dispatch(wiz, ".jail Bob")
    dispatch(bob, ".go atrium")
    assert bob.resident.room_id == "holding_cell"
    assert "jailed" in bob.text.lower()   # v1.12.0: confinement is by the flag, not the map

    # And a free resident cannot walk into it, even via concourse.
    free = FakeSession(world, name="Free", clock=clock)
    dispatch(free, ".go holding cell")
    assert free.resident.room_id == "atrium"
    assert "no way there" in free.text.lower()


def test_jail_on_map_but_off_the_movement_graph():
    # holding_cell is drawn on the authored map (flavor) yet excluded from .go.
    world = _jail_world()
    clock = FakeClock()
    s = FakeSession(world, name="Bob", clock=clock)
    dispatch(s, ".map")
    assert "Holding_cell" in s.text            # shown on the map, as authored
    assert "holding_cell" not in world.exits_of("atrium")   # but not an exit


def test_free_returns_to_start_room():
    world = _jail_world()
    clock = FakeClock()
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    dispatch(wiz, ".jail Bob")
    dispatch(wiz, ".free Bob")
    assert bob.resident.room_id == "atrium"   # the arrival room
    # 1.16.0: the release line is a .ownerset key (release_msg), the mirror
    # of jail_msg, rather than a hardcoded "You've been freed."
    assert "released" in bob.text.lower()


def test_who_shows_jail_honestly():
    world = _jail_world()
    clock = FakeClock()
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    dispatch(wiz, ".jail Bob")
    wiz.clear()
    dispatch(wiz, ".who")
    assert "holding cell" in wiz.text.lower()   # honest about where Bob is


# ---- mailpurge (arch only) ----------------------------------------------

def test_mailpurge_stages_and_asks_for_the_callers_own_password(world, tmp_path, clock):
    """⛔ v1.41.0: `.mailpurge` destroys a whole mailbox irreversibly — the one
    thing the covenant lets the server keep — and until now a single mistyped
    line finished the job. It stages like `.deleteuser`/`.resetpassword` now,
    and the session loop asks for the CALLER's own password."""
    from heresay.commands import MAILPURGE
    accounts = AccountStore(tmp_path / "a.json")
    accounts.create("Generic", "pw")
    accounts.create("Spammer", "pw")
    mail = MailStore(tmp_path / "m.json", tmp_path / "m.key")
    mail.deliver("Spammer", "X", "junk one")
    mail.deliver("Spammer", "Y", "junk two")
    arch = FakeSession(world, name="Generic", clock=clock, accounts=accounts,
                       mail=mail, level="arch")
    assert dispatch(arch, ".mailpurge Spammer") == MAILPURGE
    assert arch.pending_target == "Spammer"
    # ⚠ NOTHING is purged by the handler — the password gate has not run.
    assert mail.count("Spammer") == 2


def test_mailpurge_wizard_refused(world, clock):
    w = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    dispatch(w, ".mailpurge Someone")
    assert "Unknown command: .mailpurge" in w.text
