# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""End-to-end moderation over the real localhost socket: boot and bans.

These need real sessions/transports (disconnects, refused connections, restart),
so they run against a started TalkerServer rather than FakeSession.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _config(tmp_path, plain_port=0):
    return Config(
        plain_host="127.0.0.1",
        plain_port=plain_port,
        ws_host="127.0.0.1",
        ws_port=0,
        owner_name="Arch",
        cert_file=str(tmp_path / "nope-cert.pem"),
        key_file=str(tmp_path / "nope-key.pem"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
    )


@pytest.fixture
async def env(tmp_path):
    # Pre-create the accounts the server will load. "Arch" is arch by config.
    store = AccountStore(tmp_path / "accounts.json")
    seed_account(store, "Arch", "apw")
    seed_account(store, "Victim", "vpw")
    started = []

    async def start():
        srv = TalkerServer(_config(tmp_path))
        await srv.start()
        started.append(srv)
        return srv, srv._servers[0].sockets[0].getsockname()[1]

    try:
        yield start
    finally:
        for srv in started:
            await srv.aclose()


# ---- socket helpers ------------------------------------------------------

async def connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


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


async def send(writer, text):
    writer.write((text + "\n").encode())
    await writer.drain()


async def login(port, name, pw):
    r, w = await connect(port)
    await collect(r)
    await send(w, name)
    await collect(r)
    await send(w, pw)
    intro = await collect(r)
    return r, w, intro


# ---- boot ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_boot_delivers_reason_and_disconnects(env):
    srv, port = await env()
    ar, aw, _ = await login(port, "Arch", "apw")
    vr, vw, _ = await login(port, "Victim", "vpw")
    await collect(ar)  # drain the arrival notice

    await send(aw, ".boot Victim being rude")
    tail = await collect(vr, timeout=1.0)
    assert "disconnected" in tail.lower() and "being rude" in tail
    # The socket is closed: a further read hits EOF.
    assert vr.at_eof() or await collect(vr, timeout=0.5) == ""
    aw.close()


@pytest.mark.asyncio
async def test_boot_frees_the_name(env):
    srv, port = await env()
    ar, aw, _ = await login(port, "Arch", "apw")
    vr, vw, _ = await login(port, "Victim", "vpw")

    await send(aw, ".boot Victim")
    await collect(vr, timeout=1.0)          # let the boot complete
    await asyncio.sleep(0.1)

    # The name is free again: a fresh login succeeds (not "already connected").
    r2, w2, intro = await login(port, "Victim", "vpw")
    assert "atrium" in intro.lower()
    assert "already connected" not in intro.lower()
    for w in (aw, w2):
        w.close()


@pytest.mark.asyncio
async def test_double_login_offers_takeover_and_n_leaves_ghost(env):
    srv, port = await env()
    vr, vw, _ = await login(port, "Victim", "vpw")
    # A second login for the same live account is no longer refused: it offers a
    # self-takeover after the correct password. Answering 'n' leaves the first
    # session untouched.
    r2, w2 = await connect(port)
    await collect(r2)
    await send(w2, "Victim")
    await collect(r2)
    await send(w2, "vpw")
    out = await collect(r2)
    assert "take over" in out.lower()
    await send(w2, "n")
    await collect(r2)
    # The original session is still in-world and usable.
    assert srv.world.get_resident("Victim").session is not None
    await send(vw, ".who")
    who = await collect(vr)
    assert "Victim" in who
    for w in (vw, w2):
        w.close()


# ---- bans ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_ban_by_name_refuses_login_and_persists(env, tmp_path):
    srv, port = await env()
    ar, aw, _ = await login(port, "Arch", "apw")
    await send(aw, ".ban Victim 24")
    await collect(ar)

    # Banned: login is refused even with the right password.
    r, w = await connect(port)
    await collect(r)
    await send(w, "Victim")
    await collect(r)
    await send(w, "vpw")
    assert "banned" in (await collect(r)).lower()
    w.close()
    aw.close()

    # Restart the server (same files): the ban persists.
    await srv.aclose()
    srv2, port2 = await env()
    r2, w2 = await connect(port2)
    await collect(r2)
    await send(w2, "Victim")
    await collect(r2)
    await send(w2, "vpw")
    assert "banned" in (await collect(r2)).lower()
    w2.close()


@pytest.mark.asyncio
async def test_ip_ban_refuses_new_connections(env):
    """An IP ban still refuses at the door — v1.32.0 changed how an address is
    FOUND, not what a ban on one does.

    ⚠ This used to ban 127.0.0.1 and assert the next connection was refused.
    Both halves are now deliberately wrong: `.ban` no longer takes an address
    (that is `.ban_ip <name>`), and loopback is NEVER IP-refused — it is the
    local console, the door the arch is bootstrapped through. So the ban goes on
    an ordinary address, and the loopback case is asserted the other way in
    test_ban_ip.py.
    """
    srv, port = await env()
    srv.bans.ban("198.51.100.7", "ip", hours=1)
    assert srv.bans.is_banned_ip("198.51.100.7")
    assert not srv._ip_ban_exempt("198.51.100.7")

    # ...and the local door, banned or not, always opens.
    srv.bans.ban("127.0.0.1", "ip", hours=1)
    r, w = await connect(port)
    assert "banned" not in (await collect(r)).lower()
    w.close()
