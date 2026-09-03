# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""v1.12.0 — tighten jail isolation: block mail-send, .join, and ALL room
movement (by the jailed flag, not an exitless room). Staff appeal stays open."""

from pathlib import Path

from heresay.commands import dispatch
from heresay.commands._shared import JAILED_OUTBOUND
from heresay.mail import MailStore
from heresay.state import StateStore
from heresay.world import load_world

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _state(tmp_path):
    return StateStore(tmp_path / "state.json")


def _mail(tmp_path):
    return MailStore(tmp_path / "mailboxes.json", tmp_path / "mail.key")


def _exitful_jail_world(tmp_path):
    """A world whose JAIL room has a REAL exit — to prove confinement holds by the
    jailed flag, not because the jail happens to be exitless (Commons's is)."""
    root = tmp_path / "world"
    rooms = root / "rooms"
    rooms.mkdir(parents=True)
    (root / "map.txt").write_text("yard -- cell\n", encoding="utf-8")
    (rooms / "yard.txt").write_text(
        "name: yard\nexits: cell\n\nAn open yard.\n", encoding="utf-8")
    (rooms / "cell.txt").write_text(
        "name: cell\nexits: yard\n\nA cell with a wide-open door.\n",
        encoding="utf-8")
    return load_world(root, "yard", "cell")


# ---- movement is gated by the FLAG, even with a real exit ----------------

def test_jailed_go_refused_even_with_a_real_exit(tmp_path):
    world = _exitful_jail_world(tmp_path)
    assert "yard" in world.rooms["cell"].exits          # the exit genuinely exists
    s = FakeSession(world, name="Jailbird", room="cell", state=_state(tmp_path))
    s.resident.jailed = True
    s.clear()
    dispatch(s, ".go yard")
    assert JAILED_OUTBOUND in s.text                     # blocked by the flag...
    assert s.resident.room_id == "cell"                  # ...and did not move


def test_non_jailed_go_still_works_in_exitful_world(tmp_path):
    """Regression: gating jail must not break ordinary movement."""
    world = _exitful_jail_world(tmp_path)
    s = FakeSession(world, name="Free", room="cell", state=_state(tmp_path))
    dispatch(s, ".go yard")
    assert s.resident.room_id == "yard"                  # a free resident moves normally


# ---- .join is gated ------------------------------------------------------

def test_jailed_join_refused(tmp_path):
    world = load_world(WORLD, "atrium", "holding_cell")
    st = _state(tmp_path)
    inviter = FakeSession(world, name="Wren", room="oak", state=st)
    jailed = FakeSession(world, name="Jailbird", room="holding_cell", state=st)
    dispatch(inviter, ".invite Jailbird")                # a standing invitation
    jailed.resident.jailed = True
    jailed.clear()
    dispatch(jailed, ".join Wren")
    assert JAILED_OUTBOUND in jailed.text
    assert jailed.resident.room_id == "holding_cell"     # stayed put


# ---- .mail send is gated; reading/receiving is not -----------------------

def test_jailed_cannot_send_mail(tmp_path):
    world = load_world(WORLD, "atrium", "holding_cell")
    s = FakeSession(world, name="Jailbird", room="holding_cell", state=_state(tmp_path))
    s.resident.jailed = True
    for cmd in (".mail Wren psst come here", ".mail Wren"):   # one-line + compose
        s.clear()
        dispatch(s, cmd)
        assert JAILED_OUTBOUND in s.text, cmd
        assert not s.in_editor()                          # compose never opened


def test_jailed_can_still_read_and_receive_mail(tmp_path):
    world = load_world(WORLD, "atrium", "holding_cell")
    mail = _mail(tmp_path)
    mail.deliver("Jailbird", "Wren", "hang in there")     # mail arrives while jailed
    s = FakeSession(world, name="Jailbird", room="holding_cell",
                    state=_state(tmp_path), mail=mail)
    s.resident.jailed = True
    s.clear()
    dispatch(s, ".mail")                                  # bare = read
    assert JAILED_OUTBOUND not in s.text
    assert "hang in there" in s.text                      # received + read in jail


# ---- appeal channels stay OPEN -------------------------------------------

def test_jailed_keeper_and_mailkeeper_still_work(tmp_path):
    world = load_world(WORLD, "atrium", "holding_cell")
    mail = _mail(tmp_path)
    st = _state(tmp_path)
    staff = FakeSession(world, name="Wizzy", level="wizard", state=st, mail=mail)
    jailed = FakeSession(world, name="Jailbird", room="holding_cell",
                         state=st, mail=mail)
    jailed.resident.jailed = True
    jailed.clear()
    dispatch(jailed, ".call_staff please let me explain")
    assert JAILED_OUTBOUND not in jailed.text
    assert "gone out to the staff" in jailed.text.lower()
    jailed.clear()
    dispatch(jailed, ".mail_staff here is my appeal")
    assert JAILED_OUTBOUND not in jailed.text
    assert "Left with" in jailed.text


def test_jailed_say_and_emote_still_work(tmp_path):
    world = load_world(WORLD, "atrium", "holding_cell")
    st = _state(tmp_path)
    jailed = FakeSession(world, name="Jailbird", room="holding_cell", state=st)
    staff = FakeSession(world, name="Staff", room="holding_cell",
                         level="wizard", state=st)
    jailed.resident.jailed = True
    staff.clear()
    dispatch(jailed, "let me out please")
    dispatch(jailed, ".emote rattles the bars")
    assert "Jailbird says: let me out please" in staff.text
    assert "Jailbird rattles the bars" in staff.text
