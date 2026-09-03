# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end: two concurrent sessions over the real localhost listener.

Covers login/account creation, arrival announcements, speech, .go with
movement announcements, .tell across rooms, .who, mute filtering, and the
flood throttle → escalation path.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.commands import THROTTLE_MSG
from heresay.config import Config
from heresay.server import TalkerServer

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
async def server(tmp_path):
    cfg = Config(
        plain_host="127.0.0.1",
        plain_port=0,  # ephemeral
        ws_host="127.0.0.1",
        ws_port=0,  # ephemeral WS door
        cert_file=str(tmp_path / "nope-cert.pem"),  # missing -> TLS skipped
        key_file=str(tmp_path / "nope-key.pem"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        # bans_file was missing here until v1.30.0 — harmless only because
        # BanStore writes lazily, so a repo-root bans.json never appeared. The
        # event store creates its file up front (0600 from the moment it exists),
        # which surfaced the gap. Both are named now; see CLAUDE.md's rule that
        # EVERY Config in a test points EVERY state file at tmp_path.
        bans_file=str(tmp_path / "bans.json"),
        events_file=str(tmp_path / "events.jsonl"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )
    srv = TalkerServer(cfg)
    await srv.start()
    port = srv._servers[0].sockets[0].getsockname()[1]
    try:
        yield srv, port
    finally:
        await srv.aclose()


async def collect(reader, timeout=0.4):
    """Read whatever arrives until a quiet gap, decode to text."""
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


async def send(writer, text):
    writer.write((text + "\n").encode())
    await writer.drain()


async def login_new(reader, writer, name, pw="swordfish-x9"):
    await collect(reader)              # greeting + name prompt
    await send(writer, name)
    await collect(reader)              # "Create it?"
    await send(writer, "yes")
    await collect(reader)              # "Choose a password:"
    await send(writer, pw)
    await collect(reader)              # "Confirm password:"
    await send(writer, pw)
    await collect(reader)              # agreement text + "Do you agree?"
    await send(writer, "y")
    return await collect(reader, timeout=1.5)       # welcome + codes + arrival + look


async def connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.asyncio
async def test_two_sessions_full_flow(server):
    srv, port = server

    r1, w1 = await connect(port)
    intro1 = await login_new(r1, w1, "Corvid")
    assert "atrium" in intro1.lower()

    # Second person arrives; the first should see the announcement.
    r2, w2 = await connect(port)
    intro2 = await login_new(r2, w2, "Wren")
    assert "atrium" in intro2.lower()
    seen_by_1 = await collect(r1)
    assert "[Wren enters the world]" in seen_by_1

    # Speech is seen by the other occupant.
    await send(w2, "the rain finally stopped.")
    assert "Wren says: the rain finally stopped." in await collect(r1)

    # .who shows both, with rooms.
    await send(w1, ".who")
    who = await collect(r1)
    assert "Corvid" in who and "Wren" in who and "atrium" in who

    # Wren moves to the gallery; Corvid (left behind) sees the exit.
    await send(w2, ".go gallery")
    left = await collect(r1)
    assert "slips out toward the gallery" in left
    moved = await collect(r2)
    assert "gallery" in moved.lower()

    # .tell works across rooms and is private.
    await send(w1, ".tell Wren still there?")
    tell = await collect(r2)
    assert ">> Corvid says: still there?" in tell

    # Mute: Corvid mutes Wren, then Wren shouts; Corvid should not hear it.
    await send(w1, ".mute Wren")
    await collect(r1)
    await send(w2, ".shout hello everyone")
    after_shout = await collect(r1)
    assert "hello everyone" not in after_shout

    for w in (w1, w2):
        w.close()


@pytest.mark.asyncio
async def test_flood_throttles_and_others_dont_see_it(server):
    srv, port = server

    r1, w1 = await connect(port)
    await login_new(r1, w1, "Flooder")
    r2, w2 = await connect(port)
    await login_new(r2, w2, "Bystander")
    await collect(r1)  # drain the arrival notice

    # Paste 40 lines in one burst.
    blob = "".join(f"flood line {i}\n" for i in range(40))
    w1.write(blob.encode())
    await w1.drain()

    flooder_view = await collect(r1, timeout=0.6)
    bystander_view = await collect(r2, timeout=0.6)

    # The flooder is told to slow down.
    assert THROTTLE_MSG in flooder_view

    # The bystander sees only the initial burst, not the whole paste.
    delivered = bystander_view.count("Flooder says:")
    assert delivered <= 7, f"bystander saw {delivered} flood lines"
    assert delivered >= 1

    # Sustained flooding drops the connection: the flooder's socket hits EOF.
    tail = await collect(r1, timeout=1.0)
    assert r1.at_eof() or tail == "" or "Goodbye" in tail

    for w in (w1, w2):
        w.close()
