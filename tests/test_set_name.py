# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""v1.13.0 — .set name <NewCapitalization>: restyle your OWN name's display case.
Identity/login/lookup/mail stay casefold; only the shown capitalization changes."""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore, _key
from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
STRONG = "brass-otter-hum-9"


def _sess(world, tmp_path, name, store=None):
    store = store or AccountStore(tmp_path / "accounts.json")
    if store.get(name) is None:
        store.create(name, "password1")
    s = FakeSession(world, name=name, accounts=store)
    return s, store


# ---- the restyle itself --------------------------------------------------

def test_recase_updates_display_and_keeps_casefold_identity(world, tmp_path):
    s, store = _sess(world, tmp_path, "shady")
    dispatch(s, ".set name Shady")
    assert "shows as Shady" in s.text
    # Live resident + persisted account both show the new case...
    assert s.resident.name == "Shady"
    assert store.get("shady").name == "Shady"           # found by the OLD case
    assert store.get("SHADY").name == "Shady"           # ...and any case
    # ...while the casefold identity key is unchanged.
    assert _key("shady") == _key("Shady")
    assert AccountStore(store.path).get("shady").name == "Shady"   # persisted


def test_recase_shows_in_who_and_room_lookup(world, tmp_path):
    s, store = _sess(world, tmp_path, "shady")
    onlooker = FakeSession(world, name="Wren", accounts=store)
    dispatch(s, ".set name Shady")
    onlooker.clear()
    dispatch(onlooker, ".who")
    assert "Shady" in onlooker.text and "shady" not in onlooker.text.replace("Shady", "")
    # Room/world lookup by any case still finds the same live resident.
    assert world.get_resident("SHADY") is s.resident
    r, ambiguous = world.resolve_resident("shady")
    assert r is s.resident and r.name == "Shady"


def test_recase_rejects_different_letters(world, tmp_path):
    s, store = _sess(world, tmp_path, "shady")
    s.clear()
    dispatch(s, ".set name Shadey")                     # an extra letter — a rename
    assert "only change the capitalization" in s.text
    assert s.resident.name == "shady"                   # unchanged
    assert store.get("shady").name == "shady"


@pytest.mark.parametrize("styled", ["SHADY", "ShAdY", "shadY"])
def test_recase_accepts_any_capitalization(world, tmp_path, styled):
    s, store = _sess(world, tmp_path, "shady")
    dispatch(s, f".set name {styled}")
    assert s.resident.name == styled
    assert store.get("shady").name == styled


def test_casefold_uniqueness_survives_a_recase(world, tmp_path):
    s, store = _sess(world, tmp_path, "shady")
    dispatch(s, ".set name Shady")
    with pytest.raises(ValueError):                     # can't create a casefold-collision
        store.create("SHADY", "password1")
    assert store.exists("shady") and store.exists("SHADY")


def test_tell_to_the_old_case_reaches_the_recased_resident(world, tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("shady", "password1")
    store.create("wren", "password1")
    shady = FakeSession(world, name="shady", accounts=store)
    wren = FakeSession(world, name="wren", accounts=store)
    dispatch(shady, ".set name Shady")
    shady.clear()
    dispatch(wren, ".tell SHADY hi there")              # address by a different case
    assert "hi there" in shady.text                     # still reaches them


def test_existing_set_keys_still_work(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)   # FakeAccounts is fine here
    dispatch(s, ".set dim on")
    assert "dim on" in s.text and s.dim_system is True
    s.clear()
    dispatch(s, ".set enter_msg strides in.")
    assert "strides in." in s.text


# ---- integration: casefold login + addressing after a recase -------------

def _cfg(tmp_path):
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
        owner_name="Generic",
    )


async def _collect(r, t=0.4):
    buf = b""
    while True:
        try:
            c = await asyncio.wait_for(r.read(4096), t)
        except asyncio.TimeoutError:
            break
        if not c:
            break
        buf += c
    return buf.decode(errors="replace")


async def _send(w, s):
    w.write((s + "\n").encode())
    await w.drain()


@pytest.mark.asyncio
async def test_relog_with_old_case_after_recase(tmp_path):
    seed_account(AccountStore(tmp_path / "accounts.json"), "shady", STRONG)
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    port = srv._servers[0].sockets[0].getsockname()[1]
    try:
        # Log in as 'shady', restyle to 'Shady', quit.
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await _collect(r); await _send(w, "shady")
        await _collect(r); await _send(w, STRONG)
        await _collect(r, 1.0)
        await _send(w, ".set name Shady"); assert "shows as Shady" in await _collect(r)
        await _send(w, ".quit"); await _collect(r); w.close()
        # Relog typing the UPPER case — same account, display is 'Shady'.
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        await _collect(r2); await _send(w2, "SHADY")
        await _collect(r2); await _send(w2, STRONG)
        intro = await _collect(r2, 1.0)
        assert "atrium" in intro.lower()           # logged in fine under any case
        await _send(w2, ".who")
        assert "Shady" in await _collect(r2)            # shown in the restyled case
        w2.close()
        assert srv.accounts.get("shady").name == "Shady"
    finally:
        await srv.aclose()
