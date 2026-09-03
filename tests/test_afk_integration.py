# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AFK over the real socket: any input clears it (self-only), cleared on reconnect."""

import asyncio
from pathlib import Path

import pytest

from heresay.config import Config
from heresay.server import TalkerServer

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


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


async def collect(reader, timeout=0.4):
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


async def send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def new_account(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r)
    await send(w, "yes"); await collect(r); await send(w, pw); await collect(r)
    await send(w, pw); await collect(r)                # agreement text
    await send(w, "y"); await collect(r, timeout=1.5)  # + recovery-code hashing
    return r, w


async def login(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r); await send(w, pw)
    return r, w, await collect(r)


@pytest.mark.asyncio
async def test_any_input_clears_afk_self_only(server):
    srv, port = server
    ra, wa = await new_account(port, "Corvid")
    rb, wb = await new_account(port, "Wren")
    await collect(ra)   # drain Wren's arrival

    await send(wa, ".afk resting")
    assert "You're now AFK: resting" in await collect(ra)

    await collect(rb)   # anything pending for Wren
    # Corvid's next input clears AFK — Corvid sees the confirmation...
    await send(wa, ".who")
    out_a = await collect(ra)
    assert "You're back." in out_a
    # ...and Wren does NOT (confirmation is to self only).
    assert "You're back." not in await collect(rb)

    for w in (wa, wb):
        w.close()


@pytest.mark.asyncio
async def test_afk_visible_to_others_in_who(server):
    srv, port = server
    ra, wa = await new_account(port, "Corvid")
    rb, wb = await new_account(port, "Wren")
    await send(wa, ".afk gardening")   # Corvid AFK; setting doesn't clear it
    await collect(ra)
    await send(wb, ".who")
    who = await collect(rb)
    assert "Corvid (AFK)" in who
    for w in (wa, wb):
        w.close()


@pytest.mark.asyncio
async def test_afk_cleared_on_reconnect(server):
    srv, port = server
    ra, wa = await new_account(port, "Corvid", "corvidpw")
    rb, wb = await new_account(port, "Wren")
    await send(wa, ".afk away a while")
    await collect(ra)
    await send(wa, ".quit")
    await collect(ra); wa.close()
    await asyncio.sleep(0.2)

    # Corvid logs back in fresh; AFK is gone.
    ra2, wa2, _ = await login(port, "Corvid", "corvidpw")
    await collect(rb)
    await send(wb, ".who")
    who = await collect(rb)
    assert "Corvid" in who
    assert "Corvid (AFK)" not in who
    for w in (wa2, wb):
        w.close()
