# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.deleteme` — self-service account deletion."""

import asyncio
from pathlib import Path

import pytest

from heresay.commands import DELETEME, dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


# ---- unit: the arch may not delete itself -------------------------------

def test_deleteme_returns_sentinel_for_resident(world):
    s = FakeSession(world, name="Bob", level="resident")
    assert dispatch(s, ".deleteme") == DELETEME


def test_deleteme_refuses_the_arch(world):
    s = FakeSession(world, name="Generic", level="arch",
                    config=Config(wrap_columns=1000, owner_name="Generic"))
    assert dispatch(s, ".deleteme") is None
    assert "The house cannot delete its owner. Demote or reconfigure first." in s.text


# ---- integration helpers -------------------------------------------------

def make_config(tmp_path, arch="Generic"):
    return Config(
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
        owner_name=arch,
    )


@pytest.fixture
async def server(tmp_path):
    srv = TalkerServer(make_config(tmp_path))
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


async def collect(reader, timeout=0.5):
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


async def create(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r)   # Create it?
    await send(w, "yes"); await collect(r)                     # Choose a password
    await send(w, pw); await collect(r)                        # Confirm password
    await send(w, pw); await collect(r)                        # agreement
    await send(w, "y")
    return r, w, await collect(r, timeout=1.5)


# ---- integration: the deletion itself ------------------------------------

@pytest.mark.asyncio
async def test_wrong_password_keeps_account(server):
    srv, port = server
    r, w, _ = await create(port, "Bob")
    await send(w, ".deleteme")
    warn = await collect(r)
    assert "permanently deletes your account" in warn  # the warning (may wrap at 78)
    assert "no undo" in warn
    assert "Password" in warn                          # then the echo-off prompt
    await send(w, "notmypassword")
    out = await collect(r)
    assert "nothing was deleted" in out
    assert srv.accounts.exists("Bob")                  # account intact
    # Session is still alive and usable.
    await send(w, ".look")
    assert "atrium" in (await collect(r)).lower()
    w.close()


@pytest.mark.asyncio
async def test_delete_removes_record_mailbox_and_frees_name(server):
    srv, port = server
    ar, aw, _ = await create(port, "Ada")            # observer + correspondent
    br, bw, _ = await create(port, "Bob")
    await collect(ar)                                    # drain "[Bob enters the world]"

    # Bob sends Ada mail (recipient-owned) and Ada sends Bob mail (Bob's box).
    await send(bw, ".mail Ada keep this one"); await collect(br)
    await send(aw, ".mail Bob you will lose this"); await collect(ar)
    assert srv.mail.count("Ada") == 1 and srv.mail.count("Bob") == 1

    # Bob deletes himself.
    await send(bw, ".deleteme"); await collect(br)
    await send(bw, "swordfish-x9")
    bye = await collect(br)
    assert "Your account is gone" in bye
    await asyncio.sleep(0.2)

    # Record gone from the on-disk bytes; mailbox gone; Ada's box survives.
    assert not srv.accounts.exists("Bob")
    raw = Path(srv.config.accounts_file).read_text()
    assert '"Bob"' not in raw and '"bob"' not in raw
    assert srv.mail.count("Bob") == 0                   # his inbox is gone
    assert srv.mail.count("Ada") == 1                 # mail he SENT survives
    # ⚠ One file per box since v1.52.5: "no Bob-keyed box on disk" is now the
    # absence of his FILE, which is a sharper form of the same check than
    # grepping one big document for his key.
    from heresay import boxfiles
    d = boxfiles.box_dir(Path(srv.config.mail_file))
    assert "bob" not in [k for k, _ in boxfiles.read_boxes(d)]
    assert not boxfiles.box_path(d, "bob").exists()
    assert "ada" in [k for k, _ in boxfiles.read_boxes(d)]   # hers survives

    # Departure announced to the room, and Bob's socket is disconnected.
    assert "[Bob leaves the world]" in await collect(ar)
    assert await collect(br) == ""                      # socket closed

    # The freed name is registrable again.
    r2, w2, out2 = await create(port, "Bob")
    assert "atrium" in out2.lower()
    assert srv.accounts.exists("Bob")
    aw.close(); w2.close()


@pytest.mark.asyncio
async def test_arch_cannot_delete_over_socket(server):
    srv, port = server
    ar, aw, _ = await create(port, "Generic")           # the arch
    await send(aw, ".deleteme")
    out = await collect(ar)
    assert "The house cannot delete its owner. Demote or reconfigure first." in out
    assert "Password" not in out                         # never even prompted
    assert srv.accounts.exists("Generic")
    aw.close()


@pytest.mark.asyncio
async def test_deleted_users_correspondent_slot_is_harmless(server):
    srv, port = server
    ar, aw, _ = await create(port, "Ada")
    br, bw, _ = await create(port, "Bob")
    await collect(ar)

    # Ada tells Bob -> Ada's sticky correspondent is now Bob.
    await send(aw, ".tell Bob hello"); await collect(ar); await collect(br)
    # Bob deletes himself.
    await send(bw, ".deleteme"); await collect(br)
    await send(bw, "swordfish-x9"); await collect(br)
    await asyncio.sleep(0.2)
    await collect(ar)                                    # drain "[Bob leaves the world]"

    # Ada's defaulted tell now just reads Bob as offline — no crash.
    await send(aw, ".t are you there")
    out = await collect(ar)
    assert "no one called Bob online" in out
    # Ada is still fine.
    await send(aw, ".look")
    assert "atrium" in (await collect(ar)).lower()
    aw.close()
