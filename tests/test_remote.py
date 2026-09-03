# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.remote` (a private emote) and the '>' = tell / '<' = remote prefixes.

Also verifies the comma / dot-space shortcuts (commit 8cb6f05) were reverted.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.commands import THROTTLE_MSG, _COMMANDS, cmd_remote, dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


# ---- .remote -------------------------------------------------------------

def test_remote_delivers_to_target_only(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    obs = FakeSession(world, name="Obs", clock=clock)
    obs.clear()
    dispatch(a, ".remote Bob waves happily")
    assert ">> Corvid waves happily" in b.text              # target sees the emote
    # The sender's own copy names where it went — a remote can go to anyone
    # anywhere, so the tag is the only sign of the destination.
    assert a.out[-1] == ">> (to Bob) Corvid waves happily"
    assert "Corvid" not in obs.text                         # nobody else


def test_remote_sender_sees_the_to_tag_and_recipient_does_not(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    b.clear()
    dispatch(a, ".remote bob waves")
    assert b.out == [">> Corvid waves"]                     # unchanged for them
    assert a.out[-1] == ">> (to Bob) Corvid waves"          # display capitalization


def test_self_remote_is_tagged_with_your_own_name(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    a.clear()
    dispatch(a, ".remote Corvid waves")
    assert a.out == [">> (to Corvid) Corvid waves"]         # once, and tagged


def test_a_tell_is_still_identical_on_both_sides(world, clock):
    a = FakeSession(world, name="Shady", clock=clock)
    b = FakeSession(world, name="Generic", clock=clock)
    b.clear()
    dispatch(a, ".tell Generic hello")
    assert a.out[-1] == ">> Shady says: hello"
    assert b.out == [">> Shady says: hello"]                # no tag on tells, ever


def test_r_alias(world, clock):
    assert _COMMANDS["r"] is cmd_remote
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".r Bob nods")
    assert ">> Corvid nods" in b.text


def test_remote_takes_any_case_but_not_a_prefix(world, clock):
    """⛔ The prefix half INVERTED in v1.52.0; the case half is unchanged."""
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".remote BOB grins")    # exact, any case
    assert ">> Corvid grins" in b.text
    b.clear()
    dispatch(a, ".remote bo waves")     # a prefix is not a name
    assert ">> Corvid waves" not in b.text


def test_remote_usage_errors(session):
    # Bare .remote with no correspondent yet.
    dispatch(session, ".remote")
    assert "You aren't talking with anyone yet." in session.text
    session.clear()
    # A resolvable name (self) with no text -> "Remote <Name> what?".
    dispatch(session, ".remote Corvid")
    assert "Remote Corvid what?" in session.text


def test_remote_unknown_target(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".remote Nobody waves")   # unresolvable, no correspondent
    assert "You aren't talking with anyone yet." in a.text


def test_remote_shares_speech_bucket(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    FakeSession(world, name="Bob", clock=clock)
    for i in range(5):                    # burst of 5 remotes drains the bucket
        dispatch(a, f".remote Bob act {i}")
    a.clear()
    dispatch(a, "now try to speak")       # plain speech, same bucket
    assert THROTTLE_MSG in a.text


def test_remote_gag_rules(world, clock):
    b = FakeSession(world, name="Bob", clock=clock)
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    gil = FakeSession(world, name="Gil", clock=clock)
    gil.resident.gagged = True
    dispatch(gil, ".remote Bob waves")
    assert ">> Gil waves" not in b.text            # blocked toward a resident
    assert "only the staff" in gil.text
    dispatch(gil, ".remote Wiz help")
    assert ">> Gil help" in wiz.text                # allowed toward a wizard


def test_remote_mute_is_filtered(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(b, ".mute Corvid")
    b.clear()
    dispatch(a, ".remote Bob waves")
    assert ">> Corvid waves" not in b.text          # muter doesn't receive it
    assert ">> (to Bob) Corvid waves" in a.text     # sender still sees their own


def test_remote_afk_notice(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(b, ".afk resting")
    b.clear()
    dispatch(a, ".remote Bob waves")
    assert ">> Corvid waves" in b.text               # still delivered
    assert "Bob is AFK: resting" in a.text                    # sender informed


# ---- '>' = tell, '<' = remote -------------------------------------------

def test_gt_prefix_is_tell(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ">Bob the rain stopped")
    assert ">> Corvid says: the rain stopped" in b.text
    a.clear(); b.clear()
    dispatch(a, "> Bob hi there")          # optional space
    assert ">> Corvid says: hi there" in b.text


def test_lt_prefix_is_remote(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, "<Bob waves happily")
    assert ">> Corvid waves happily" in b.text
    a.clear(); b.clear()
    dispatch(a, "< Bob nods slowly")       # optional space
    assert ">> Corvid nods slowly" in b.text


def test_bare_prefixes_with_no_correspondent(session):
    dispatch(session, ">")
    assert "You aren't talking with anyone yet." in session.text
    session.clear()
    dispatch(session, "<")
    assert "You aren't talking with anyone yet." in session.text


# ---- the reverted shortcuts ---------------------------------------------

def test_comma_is_plain_speech_again(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ",bob hi")
    assert "Corvid says: ,bob hi" in b.text     # a comma line is just speech


def test_dot_space_is_unknown_command_again(session):
    dispatch(session, ". hello")
    assert "Unknown command: .hello" in session.text


# ---- editor -------------------------------------------------------------

def test_editor_buffers_prefix_lines_literally(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    saved = []
    s.start_editor("test", 20, lambda sess, text: saved.append(text))
    s.feed_editor(">Bob just text")
    s.feed_editor("<Bob also text")
    s.feed_editor(".end")
    assert saved == [">Bob just text\n<Bob also text"]


# ---- cross-door (real socket) -------------------------------------------

@pytest.fixture
async def server(tmp_path):
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )
    srv = TalkerServer(cfg)
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


async def _collect(reader, timeout=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _new(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name); await _collect(r)
    await _send(w, "yes"); await _collect(r); await _send(w, pw); await _collect(r)
    await _send(w, pw); await _collect(r)                 # agreement text
    await _send(w, "y"); await _collect(r, timeout=1.5)   # + recovery hashing
    return r, w


@pytest.mark.asyncio
async def test_remote_cross_door(server):
    srv, port = server
    ra, wa = await _new(port, "Corvid")
    rb, wb = await _new(port, "Bob")
    await _collect(ra)

    await _send(wa, ".remote Bob waves happily")
    assert ">> Corvid waves happily" in await _collect(rb)   # target only
    mine = await _collect(ra)
    assert ">> (to Bob) Corvid waves happily" in mine        # sender: tagged
    assert mine.count("waves happily") == 1                  # and exactly once

    # the '<' prefix over the wire, too
    await _send(wa, "<Bob bows")
    assert ">> Corvid bows" in await _collect(rb)
    assert ">> (to Bob) Corvid bows" in await _collect(ra)

    for w in (wa, wb):
        w.close()
