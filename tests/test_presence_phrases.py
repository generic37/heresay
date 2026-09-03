# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""v1.14.0 — enter/leave lines are PHRASES the engine renders as [name phrase],
with a per-resident login_msg/logout_msg override of the owner default."""

import asyncio
import json
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import HOUSE_DEFAULTS, StateStore, house_phrase, phrase_or_none, presence_line

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
STRONG = "brass-otter-hum-9"


# ---- the render helper + legacy normalization (unit) ---------------------

def test_presence_line_and_legacy_normalization():
    assert presence_line("Shady", "tumbles in") == "[Shady tumbles in]"
    assert phrase_or_none("arrives with a flourish") == "arrives with a flourish"
    # Legacy full-template values are treated as absent (fall back to default):
    for legacy in ("[{name} enters the world]", "{name} arrives", "[bracketed]", ""):
        assert phrase_or_none(legacy) is None
    assert house_phrase(None, "login_msg") == "enters the world"       # built-in phrase


def test_house_phrase_ignores_a_legacy_stored_value(tmp_path):
    # A pre-1.14 state.json full-template value is dropped on load -> default phrase.
    (tmp_path / "state.json").write_text(json.dumps(
        {"version": 1, "settings": {"login_msg": "[{name} enters the world]"}}))
    st = StateStore(tmp_path / "state.json")
    assert house_phrase(st, "login_msg") == "enters the world"         # not double-bracketed
    assert "login_msg" not in st.settings                             # self-healed


# ---- .set login_msg / logout_msg (unit) ------------------------------------

def _store_sess(world, tmp_path, name="Corvid"):
    store = AccountStore(tmp_path / "accounts.json")
    store.create(name, "password1")
    st = StateStore(tmp_path / "state.json")
    return FakeSession(world, name=name, accounts=store, state=st), store


def test_set_login_msg_is_a_bracketed_phrase(world, tmp_path):
    s, store = _store_sess(world, tmp_path)
    dispatch(s, ".set login_msg tumbles down the hole")
    assert "[Corvid tumbles down the hole]" in s.text                 # preview = the render
    assert store.get("Corvid").settings.get("login_msg") == "tumbles down the hole"
    s.clear()
    dispatch(s, ".set login_msg")                                      # bare: shows own
    assert "[Corvid tumbles down the hole]" in s.text


def test_set_login_msg_default_reverts_to_house_phrase(world, tmp_path):
    s, store = _store_sess(world, tmp_path)
    dispatch(s, ".set login_msg tumbles in")
    s.clear()
    dispatch(s, ".set login_msg default")
    assert store.get("Corvid").settings.get("login_msg") is None       # override cleared
    assert "[Corvid enters the world]" in s.text                      # back to house phrase


def test_unset_login_msg_shows_the_house_phrase(world, tmp_path):
    s, _ = _store_sess(world, tmp_path)
    dispatch(s, ".set login_msg")
    assert "house default" in s.text and "[Corvid enters the world]" in s.text


# ---- render precedence, end to end ---------------------------------------

def _cfg(tmp_path, **over):
    base = dict(
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
    base.update(over)
    return Config(**base)


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def _c(r, t=0.4):
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


async def _s(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _new(port, name, pw=STRONG):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _c(r); await _s(w, name)
    await _c(r); await _s(w, "yes")
    await _c(r); await _s(w, pw)
    await _c(r); await _s(w, pw)
    await _c(r); await _s(w, "y")
    await _c(r, 1.5)
    return r, w


async def _login(port, name, pw=STRONG):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _c(r); await _s(w, name)
    await _c(r); await _s(w, pw)
    await _c(r, 1.5)
    return r, w


@pytest.mark.asyncio
async def test_default_login_renders_the_house_phrase(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        rw, ww = await _new(port, "Wren")
        rb, wb = await _new(port, "Bram")
        assert "[Bram enters the world]" in await _c(rw, 1.0)
        await _s(wb, ".quit"); await _c(rb); wb.close()
        assert "[Bram leaves the world]" in await _c(rw, 1.0)
        ww.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_caretaker_default_phrase_applies_to_unset_residents(tmp_path):
    seed_account(AccountStore(tmp_path / "accounts.json"), "Wizzy", STRONG)
    srv, port = await _start(_cfg(tmp_path, owner_name="Wizzy"))
    try:
        rw, ww = await _login(port, "Wizzy")
        await _s(ww, ".ownerset login_msg arrives with a flourish"); await _c(rw)
        rb, wb = await _new(port, "Bram")                              # unset resident
        assert "[Bram arrives with a flourish]" in await _c(rw, 1.0)
        for w in (ww, wb):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_personal_login_msg_overrides_house_phrase(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        rw, ww = await _new(port, "Wren")                             # watcher
        rb, wb = await _new(port, "Shady")
        await _s(wb, ".set login_msg tumbles in"); await _c(rb)
        await _s(wb, ".quit"); await _c(rb); await _c(rw); wb.close()
        rb2, wb2 = await _login(port, "Shady")                        # relog: personal phrase
        assert "[Shady tumbles in]" in await _c(rw, 1.0)
        # An unset resident still gets the house phrase.
        rc, wc = await _new(port, "Cass")
        assert "[Cass enters the world]" in await _c(rw, 1.0)
        for w in (ww, wb2, wc):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_jailed_entry_ignores_personal_login_msg(tmp_path):
    seed_account(AccountStore(tmp_path / "accounts.json"), "Wizzy", STRONG)
    srv, port = await _start(_cfg(tmp_path, owner_name="Wizzy"))
    try:
        rw, ww = await _login(port, "Wizzy")                          # arch/watcher
        rb, wb = await _new(port, "Shady")
        await _s(wb, ".set login_msg tumbles in"); await _c(rb)        # a personal phrase
        await _s(ww, ".jail Shady"); await _c(rw); await _c(rb)
        await _s(wb, ".quit"); await _c(rb); await _c(rw); wb.close()
        rb2, wb2 = await _login(port, "Shady")                        # jailed relog
        # Jailed -> the house jailed phrase, personal login_msg IGNORED.
        assert "[*** Shady arrives (jail) ***]" in await _c(rw, 1.0)
        for w in (ww, wb2):
            w.close()
    finally:
        await srv.aclose()
