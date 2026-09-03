# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

""".topic — a note on the door of a gathering. Room-lifetime memory, nothing more."""

from pathlib import Path

import pytest

from heresay.commands import dispatch
from heresay.config import Config
from heresay.session import Session
from heresay.world import TOPIC_MAX, Resident, load_world

from conftest import FakeClock, FakeSession, RecordingTransport

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


# ---- setting and announcing ---------------------------------------------

def test_set_announces_to_the_room(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".topic what the badger said")
    assert "Corvid sets the topic: what the badger said" in b.text
    assert world.topic_of("atrium") == "what the badger said"


def test_anyone_present_may_set_it(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".topic first")
    dispatch(b, ".topic second")
    assert world.topic_of("atrium") == "second"


def test_it_is_per_room(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    dispatch(a, ".topic in the hole")
    dispatch(b, ".topic under the oak")
    assert world.topic_of("atrium") == "in the hole"
    assert world.topic_of("oak") == "under the oak"


def test_capped_at_sixty_and_control_stripped(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".topic " + "x" * 200)
    assert len(world.topic_of("atrium")) == TOPIC_MAX == 60
    dispatch(a, ".topic bells\x07 and \x00whistles\n\nagain")
    assert world.topic_of("atrium") == "bells and whistles again"


# ---- display -------------------------------------------------------------

def test_look_shows_the_topic_below_the_exits(world, clock):
    """The room first — what it is, who's in it, where it leads — and THEN the
    note this gathering pinned to the door."""
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".topic what the badger said")
    a.clear()
    dispatch(a, ".look")
    lines = [ln for ln in a.text.split("\n") if ln.strip()]
    assert lines[0] == "[ atrium ]"
    assert lines[-1] == "Topic: what the badger said"      # last, below...
    assert lines[-2].startswith("Exits: ")                    # ...the exits
    assert a.text.split("\n")[1] == ""          # the authored gap still follows the title


def test_no_topic_means_no_line_at_all(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".look")
    assert "Topic" not in a.text               # never a "No topic." placeholder
    assert a.text.split("\n")[1] == ""


def test_arrival_sees_the_topic(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".topic what the badger said")
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    dispatch(b, ".go atrium")             # .go ends with a room description
    assert "Topic: what the badger said" in b.text


def test_bare_topic_reshows_it(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".topic what the badger said")
    a.clear()
    dispatch(a, ".topic")
    assert a.text == "Topic: what the badger said"


def test_bare_topic_is_silent_when_unset(world, clock):
    """Deliberate: an empty unit is dropped by the delivery layer, so nothing —
    not even a blank line — reaches the screen."""
    tr = RecordingTransport()
    sess = Session(tr, world, None, Config(wrap_columns=78),
                   clock=FakeClock(), wall_clock=lambda: 0.0)
    sess.resident = Resident("Corvid", "", world.start_room)
    sess.resident.session = sess
    world.add_resident(sess.resident)
    dispatch(sess, ".topic")
    assert tr.frames == []                     # not one byte


# ---- lifetime ------------------------------------------------------------

def test_clears_when_the_last_person_leaves(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".topic what the badger said")
    dispatch(a, ".go oak")
    assert world.topic_of("atrium") == "what the badger said"   # Wren remains
    dispatch(b, ".go oak")                    # now it's empty
    assert world.topic_of("atrium") == ""


def test_clears_when_the_last_person_disconnects(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".topic fleeting")
    world.remove_resident(a.resident)
    world.room_emptied("atrium")
    assert world.topic_of("atrium") == ""


def test_gone_after_a_restart(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, ".topic set before the restart")
    fresh = load_world(WORLD, "atrium", "holding_cell")
    assert fresh.topic_of("atrium") == ""


def test_a_lock_and_a_topic_lapse_together(world, clock):
    a = FakeSession(world, name="Corvid", room="cabin", clock=clock)
    dispatch(a, ".topic private words")
    dispatch(a, ".lock")
    assert world.is_locked("cabin") and world.topic_of("cabin") == "private words"
    dispatch(a, ".go copse")
    assert not world.is_locked("cabin")
    assert world.topic_of("cabin") == ""


# ---- never written down --------------------------------------------------

@pytest.mark.asyncio
async def test_topic_never_reaches_any_state_file(tmp_path):
    import asyncio
    from heresay.server import TalkerServer

    canary = "zzqqxx-topic-canary-42"
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
    port = srv._servers[0].sockets[0].getsockname()[1]

    async def collect(r, t=0.4):
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(r.read(4096), t)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace")

    async def send(w, text):
        w.write((text + "\n").encode())
        await w.drain()

    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r)
        for line in ("Corvid", "yes", "swordfish-x9", "swordfish-x9", "y"):
            await send(w, line)
            await collect(r, 1.5)
        await send(w, f".topic {canary}")
        await collect(r)
        await send(w, ".look")
        assert canary in await collect(r)          # it IS shown
        w.close()
    finally:
        await srv.aclose()

    for p in [x for x in tmp_path.rglob("*") if x.is_file()]:
        assert canary.encode() not in p.read_bytes(), p.name
