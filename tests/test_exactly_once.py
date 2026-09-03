# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Exactly once: every participant sees each conversation line ONE time.

The rule, stated: a conversation line is delivered by one broadcast (or one
symmetric pair), and that delivery IS the speaker's echo. There is no
confirmation copy anywhere, on any door, for any form — so counting, not
presence, is what these tests assert. Presence tests passed while a self-tell
printed twice; a count would not have.

Covers say, emote (.e/.em/;), think, shout, tell (.tell/.t/>), remote
(.remote/.r/<), messaging yourself, and the two real doors together.
"""

import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as ws_connect

from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession
from test_ws import (sock_collect, sock_connect, sock_login, sock_send,
                     ws_login, ws_text)

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def count(session, needle):
    """How many times `needle` appears in everything this session was sent."""
    return "\n".join(session.out).count(needle)


# ---- one room, two people -------------------------------------------------

@pytest.fixture
def pair(world, clock):
    a = FakeSession(world, name="Shady", clock=clock)
    b = FakeSession(world, name="Generic", clock=clock)
    a.clear(); b.clear()
    return a, b


@pytest.mark.parametrize("line,rendered", [
    ("hello there", "Shady says: hello there"),
    (".e waves", "Shady waves"),
    (".em waves", "Shady waves"),
    (".emote waves", "Shady waves"),
    (";waves", "Shady waves"),
    (".think aloud", "Shady thinks . . oO( aloud )"),
    (".shout hoy", "Shady shouts: hoy"),
])
def test_public_forms_reach_speaker_and_room_once_each(pair, line, rendered):
    a, b = pair
    dispatch(a, line)
    assert count(a, rendered) == 1, f"speaker saw it {count(a, rendered)}x"
    assert count(b, rendered) == 1, f"roommate saw it {count(b, rendered)}x"


@pytest.mark.parametrize("line", [".tell Generic hi", ".t Generic hi", ">Generic hi"])
def test_tell_forms_reach_sender_and_recipient_once_each(pair, line):
    a, b = pair
    dispatch(a, line)
    rendered = ">> Shady says: hi"
    assert count(a, rendered) == 1
    assert count(b, rendered) == 1


@pytest.mark.parametrize("line", [".remote Generic waves", ".r Generic waves",
                                  "<Generic waves"])
def test_remote_forms_reach_sender_and_recipient_once_each(pair, line):
    a, b = pair
    dispatch(a, line)
    assert count(a, ">> (to Generic) Shady waves") == 1
    assert count(b, ">> Shady waves") == 1
    assert count(a, "Shady waves") == 1         # one line on the sender's screen
    assert count(b, "(to ") == 0                # never the sender's wording


@pytest.mark.parametrize("line,rendered", [
    (".tell Shady note to self", ">> Shady says: note to self"),
    (">Shady note to self", ">> Shady says: note to self"),
    (".remote Shady waves", ">> (to Shady) Shady waves"),
    ("<Shady waves", ">> (to Shady) Shady waves"),
])
def test_messaging_yourself_prints_once(pair, line, rendered):
    """The bug the maintainer hit: symmetric delivery sent the target's copy and the
    sender's copy to one screen."""
    a, _ = pair
    dispatch(a, line)
    assert count(a, rendered) == 1
    assert len(a.out) == 1                      # one unit, not two


def test_a_third_person_in_the_room_never_sees_a_private_line(world, clock):
    a = FakeSession(world, name="Shady", clock=clock)
    b = FakeSession(world, name="Generic", clock=clock)
    obs = FakeSession(world, name="Wren", clock=clock)
    obs.clear()
    dispatch(a, ".tell Generic hi")
    dispatch(a, ".remote Generic waves")
    assert count(obs, "Shady") == 0


def test_speaking_to_an_empty_room_prints_once(world, clock):
    alone = FakeSession(world, name="Shady", clock=clock)
    alone.clear()
    dispatch(alone, "anyone about?")
    assert count(alone, "Shady says: anyone about?") == 1


# ---- the real doors, together ---------------------------------------------

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
        yield (srv,
               srv._servers[0].sockets[0].getsockname()[1],
               srv._ws_server.sockets[0].getsockname()[1])
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_say_and_tell_are_exactly_once_across_both_doors(server):
    """A browser and a socket in the same room. Whoever speaks, each screen
    shows the line once — the door makes no difference to the count."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_login(ws, "Shady")
        r, w = await sock_connect(plain_port)
        await sock_login(r, w, "Generic")
        await ws_text(ws)                       # drain the arrival notice

        # Browser speaks.
        await ws.send("the rain stopped")
        line = "Shady says: the rain stopped"
        assert (await ws_text(ws)).count(line) == 1          # the speaker
        assert (await sock_collect(r)).count(line) == 1      # the room

        # Socket speaks.
        await sock_send(w, "so it did")
        line = "Generic says: so it did"
        assert (await sock_collect(r)).count(line) == 1
        assert (await ws_text(ws)).count(line) == 1

        # A tell each way.
        await ws.send(".tell Generic just us")
        line = ">> Shady says: just us"
        assert (await ws_text(ws)).count(line) == 1
        assert (await sock_collect(r)).count(line) == 1

        await sock_send(w, ".tell Shady quite so")
        line = ">> Generic says: quite so"
        assert (await sock_collect(r)).count(line) == 1
        assert (await ws_text(ws)).count(line) == 1

        # A remote: tagged for the sender, plain for the recipient, once each.
        await ws.send(".remote Generic waves")
        mine = await ws_text(ws)
        assert mine.count(">> (to Generic) Shady waves") == 1
        assert mine.count("Shady waves") == 1
        theirs = await sock_collect(r)
        assert theirs.count(">> Shady waves") == 1
        assert "(to " not in theirs

        # And messaging yourself, on a real door.
        await ws.send(".tell Shady a note to self")
        assert (await ws_text(ws)).count(">> Shady says: a note to self") == 1
        await sock_send(w, ".remote Generic nods")
        assert (await sock_collect(r)).count(">> (to Generic) Generic nods") == 1

        w.close()
