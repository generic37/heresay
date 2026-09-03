# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for v1.11.0: world-wide enter/leave broadcast, .ownerset
house-wide settings, and persistent jail + isolation."""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import HOUSE_DEFAULTS, StateStore

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
STRONG = "brass-otter-hum-9"


# ============================= unit helpers =============================

def _state(tmp_path):
    return StateStore(tmp_path / "state.json")


def _sess(world, tmp_path, name="Corvid", level="resident", room=None, state=None):
    return FakeSession(world, name=name, level=level, room=room,
                       state=state if state is not None else _state(tmp_path))


# ============================= .ownerset =============================

def test_ownerset_lists_values_and_defaults(world, tmp_path):
    s = _sess(world, tmp_path, name="Boss", level="arch")
    dispatch(s, ".ownerset")
    out = s.text
    for k in HOUSE_DEFAULTS:
        assert k in out
    assert "enters the world" in out


def test_ownerset_sets_and_resets(world, tmp_path):
    st = _state(tmp_path)
    s = _sess(world, tmp_path, name="Boss", level="arch", state=st)
    dispatch(s, ".ownerset login_msg arrives with a flourish")
    assert st.house_setting("login_msg") == "arrives with a flourish"
    dispatch(s, ".ownerset login_msg default")
    assert st.house_setting("login_msg") == HOUSE_DEFAULTS["login_msg"]


def test_ownerset_accepts_a_plain_phrase(world, tmp_path):
    # Phrases now: no {name} required (the engine adds the name + brackets).
    st = _state(tmp_path)
    s = _sess(world, tmp_path, name="Boss", level="arch", state=st)
    dispatch(s, ".ownerset logout_msg wanders off into the fog")
    assert st.house_setting("logout_msg") == "wanders off into the fog"


def test_ownerset_rejects_bad_scope(world, tmp_path):
    st = _state(tmp_path)
    s = _sess(world, tmp_path, name="Boss", level="arch", state=st)
    s.clear()
    dispatch(s, ".ownerset broadcast_scope everywhere")
    assert "world" in s.text and "entry" in s.text
    assert st.house_setting("broadcast_scope") == "world"


def test_ownerset_accepts_jail_msg_and_scope(world, tmp_path):
    st = _state(tmp_path)
    s = _sess(world, tmp_path, name="Boss", level="arch", state=st)
    dispatch(s, ".ownerset jail_msg You are confined to the well.")
    dispatch(s, ".ownerset broadcast_scope entry")
    assert st.house_setting("jail_msg") == "You are confined to the well."
    assert st.house_setting("broadcast_scope") == "entry"


def test_ownerset_persists_across_restart(world, tmp_path):
    st = _state(tmp_path)
    s = _sess(world, tmp_path, name="Boss", level="arch", state=st)
    dispatch(s, ".ownerset login_msg pops in with a hop")
    # A fresh store from the same file = a restart.
    st2 = StateStore(tmp_path / "state.json")
    assert st2.house_setting("login_msg") == "pops in with a hop"


def test_ownerset_refused_for_non_arch(world, tmp_path):
    for lvl in ("resident", "wizard"):
        s = _sess(world, tmp_path, name="Nope", level=lvl)
        s.clear()
        dispatch(s, ".ownerset")
        assert "Unknown command: .ownerset" in s.text   # invisible below arch


def test_pre_116_account_keys_migrate_on_load(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    seed_account(store, "Legacy", STRONG)
    # An old accounts file carried these names; loading renames them in place.
    store.set_setting("Legacy", "loginmsg", "used to say this")
    store.set_setting("Legacy", "logoutmsg", "and this")
    store.flush()                                 # pref writes are coalesced (M4b)
    reloaded = AccountStore(tmp_path / "accounts.json")
    acct = reloaded.get("Legacy")
    assert acct is not None
    assert acct.settings.get("login_msg") == "used to say this"   # renamed, kept
    assert acct.settings.get("logout_msg") == "and this"
    assert "loginmsg" not in acct.settings and "logoutmsg" not in acct.settings


# ============================= jail isolation (unit) =============================

def _jail_world():
    from heresay.world import load_world
    return load_world(WORLD, "atrium", "holding_cell")


def test_jailed_cannot_shout_tell_remote_invite(world, tmp_path):
    st = _state(tmp_path)
    jailed = _sess(world, tmp_path, name="Jailbird", state=st)
    _other = _sess(world, tmp_path, name="Free", state=st)
    jailed.resident.jailed = True
    for cmd in (".shout hey everyone", ".tell Free psst", ".remote Free waves",
                ".invite Free"):
        jailed.clear()
        dispatch(jailed, cmd)
        assert "jailed" in jailed.text.lower(), cmd


def test_jailed_can_still_say_and_emote_in_room(tmp_path):
    world = _jail_world()
    st = _state(tmp_path)
    jailed = FakeSession(world, name="Jailbird", room="holding_cell", state=st)
    cellmate = FakeSession(world, name="Staff", room="holding_cell",
                           level="wizard", state=st)
    jailed.resident.jailed = True
    cellmate.clear()
    dispatch(jailed, "let me out please")          # plain say
    dispatch(jailed, ".emote rattles the bars")
    assert "Jailbird says: let me out please" in cellmate.text
    assert "Jailbird rattles the bars" in cellmate.text


def test_staff_tell_to_jailed_delivers_with_notice(tmp_path):
    world = _jail_world()
    st = _state(tmp_path)
    jailed = FakeSession(world, name="Jailbird", room="holding_cell", state=st)
    staff = FakeSession(world, name="Wizzy", level="wizard", state=st)
    jailed.resident.jailed = True
    staff.clear(); jailed.clear()
    dispatch(staff, ".tell Jailbird hang tight")
    assert "hang tight" in jailed.text                          # delivered
    assert HOUSE_DEFAULTS["jail_reply_msg"] in staff.text     # sender warned


def test_non_staff_tell_to_jailed_is_refused(tmp_path):
    world = _jail_world()
    st = _state(tmp_path)
    jailed = FakeSession(world, name="Jailbird", room="holding_cell", state=st)
    resident = FakeSession(world, name="Nosy", state=st)
    jailed.resident.jailed = True
    resident.clear(); jailed.clear()
    dispatch(resident, ".tell Jailbird hey")
    assert "jail" in resident.text.lower()
    assert "hey" not in jailed.text                             # not delivered


def test_jail_reply_msg_is_ownerset_able(tmp_path):
    world = _jail_world()
    st = _state(tmp_path)
    st.set_house_setting("jail_reply_msg", "Visit the well to hear them.")
    jailed = FakeSession(world, name="Jailbird", room="holding_cell", state=st)
    staff = FakeSession(world, name="Wizzy", level="wizard", state=st)
    jailed.resident.jailed = True
    staff.clear()
    dispatch(staff, ".tell Jailbird ok")
    assert "Visit the well to hear them." in staff.text


# ============================= integration =============================

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


async def _new(port, name, pw=STRONG):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name)
    await collect(r); await send(w, "yes")
    await collect(r); await send(w, pw)
    await collect(r); await send(w, pw)
    await collect(r); await send(w, "y")
    await collect(r, timeout=1.5)
    return r, w


async def _login(port, name, pw=STRONG):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name)
    await collect(r); await send(w, pw)
    intro = await collect(r, timeout=1.5)
    return r, w, intro


@pytest.mark.asyncio
async def test_world_broadcast_reaches_multiple_rooms(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        ra, wa = await _new(port, "Ayla")            # in the entry room
        rb, wb = await _new(port, "Bram")
        await send(wb, ".go oak"); await collect(rb)  # Bram in a DIFFERENT room
        await collect(ra)                              # drain Bram's enter line at Ayla
        rc, wc = await _new(port, "Cass")              # third logs in
        assert "[Cass enters the world]" in await collect(ra, timeout=1.0)   # entry room
        assert "[Cass enters the world]" in await collect(rb, timeout=1.0)   # other room
        # ...and the entering user does NOT see their own line.
        assert "[Cass enters the world]" not in await collect(rc, timeout=0.5)
        await send(wc, ".quit"); await collect(rc); wc.close()
        assert "[Cass leaves the world]" in await collect(ra, timeout=1.0)
        assert "[Cass leaves the world]" in await collect(rb, timeout=1.0)
        for w in (wa, wb):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_broadcast_scope_entry_limits_reach(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    srv.state.set_house_setting("broadcast_scope", "entry")
    try:
        ra, wa = await _new(port, "Ayla")            # entry room
        rb, wb = await _new(port, "Bram")
        await send(wb, ".go oak"); await collect(rb)  # Bram NOT in the entry room
        await collect(ra)
        rc, wc = await _new(port, "Cass")
        assert "[Cass enters the world]" in await collect(ra, timeout=1.0)   # entry: yes
        assert "[Cass enters the world]" not in await collect(rb, timeout=0.6)  # other: no
        for w in (wa, wb, wc):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_reconnect_within_grace_makes_no_leave_enter_spam(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        ra, wa = await _new(port, "Ayla")            # the watcher
        rb, wb = await _new(port, "Bram")
        assert "[Bram enters the world]" in await collect(ra, timeout=1.0)
        wb.close()                                    # an unexpected drop (grace holds)
        await asyncio.sleep(0.2)
        rb2, wb2, _ = await _login(port, "Bram")      # reconnect within the grace window
        heard = await collect(ra, timeout=1.0)
        assert "leaves the world" not in heard        # no leave...
        assert "enters the world" not in heard        # ...and no re-enter spam
        for w in (wa, wb2):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_jail_persists_across_relog_and_confines(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    seed_account(store, "Wizzy", STRONG)
    seed_account(store, "Bram", STRONG)
    cfg = _cfg(tmp_path, owner_name="Wizzy")               # make Wizzy staff (arch)
    srv, port = await _start(cfg)
    try:
        rw, ww, _ = await _login(port, "Wizzy")
        rb, wb, _ = await _login(port, "Bram")
        await send(ww, ".jail Bram"); await collect(rw); await collect(rb)
        assert srv.state.is_jailed("Bram")            # persistent status set
        # Bram logs out and back in -> lands in the JAIL room, not entry.
        await send(wb, ".quit"); await collect(rb); wb.close()
        rb2, wb2, intro = await _login(port, "Bram")
        assert "holding" in intro.lower()             # the jail room, not the atrium
        assert srv.world.get_resident("Bram").jailed is True
        # A fresh store from disk still has the flag (survives a real restart).
        assert StateStore(tmp_path / "state.json").is_jailed("Bram")
        # Freed -> flag cleared, a later relog is normal.
        await send(ww, ".free Bram"); await collect(rw); await collect(rb2)
        assert not srv.state.is_jailed("Bram")
        await send(wb2, ".quit"); await collect(rb2); wb2.close()
        rb3, wb3, intro3 = await _login(port, "Bram")
        assert "atrium" in intro3.lower()        # back to the entry room
        for w in (ww, wb3):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_jailed_entry_uses_the_starred_jail_logon_line(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    seed_account(store, "Wizzy", STRONG)
    seed_account(store, "Bram", STRONG)
    srv, port = await _start(_cfg(tmp_path, owner_name="Wizzy"))
    try:
        rw, ww, _ = await _login(port, "Wizzy")       # watcher/staff in the entry room
        rb, wb, _ = await _login(port, "Bram")
        await send(ww, ".jail Bram"); await collect(rw); await collect(rb)
        await send(wb, ".quit"); await collect(rb); wb.close()
        await collect(rw)                             # drain
        rb2, wb2, _ = await _login(port, "Bram")      # jailed relog
        assert "[*** Bram arrives (jail) ***]" in await collect(rw, timeout=1.0)
        for w in (ww, wb2):
            w.close()
    finally:
        await srv.aclose()
