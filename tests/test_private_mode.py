# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Private mode: .newusers / .logins toggles, .logoutall, and state.json."""

import asyncio
import json
from pathlib import Path

import pytest

from heresay.commands import LOGOUTALL, dispatch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import StateStore

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


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
        owner_name=arch, instance_name="Commons",
    )


# ---- unit: the toggle commands (FakeSession + a real StateStore) ----------

def _arch_session(world, tmp_path, name="Generic"):
    s = FakeSession(world, name=name, level="arch")
    s.state = StateStore(tmp_path / "state.json")
    return s


def test_newusers_command(world, tmp_path):
    s = _arch_session(world, tmp_path)
    dispatch(s, ".newusers")
    assert "Account creation is on." in s.text
    s.clear(); dispatch(s, ".newusers off")
    assert "Account creation is now off." in s.text and s.state.allow_new_accounts is False
    s.clear(); dispatch(s, ".newusers")
    assert "Account creation is off." in s.text
    s.clear(); dispatch(s, ".newusers on")
    assert s.state.allow_new_accounts is True
    s.clear(); dispatch(s, ".newusers maybe")
    assert "Usage: .newusers" in s.text


def test_logins_command(world, tmp_path):
    s = _arch_session(world, tmp_path)
    dispatch(s, ".logins")
    assert "Logins are on." in s.text
    s.clear(); dispatch(s, ".logins off")
    assert "Logins are now off." in s.text and s.state.allow_logins is False
    s.clear(); dispatch(s, ".logins junk")
    assert "Usage: .logins" in s.text


def test_logoutall_returns_sentinel(world, tmp_path):
    s = _arch_session(world, tmp_path)
    assert dispatch(s, ".logoutall") == LOGOUTALL


@pytest.mark.parametrize("level", ["resident", "wizard"])
def test_toggles_invisible_below_arch(world, tmp_path, level):
    s = FakeSession(world, name=f"U_{level}", level=level)
    s.state = StateStore(tmp_path / "state.json")
    for line in (".newusers", ".newusers off", ".logins", ".logins off", ".logoutall"):
        s.clear(); dispatch(s, line)
        assert "Unknown command" in s.text, line
    # A non-arch never flips the switches.
    assert s.state.allow_new_accounts is True and s.state.allow_logins is True


# ---- integration helpers --------------------------------------------------

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
    """Full creation incl. the agreement step; returns (r, w, final output)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r)   # Create it?
    await send(w, "yes"); await collect(r)                     # Choose a password
    await send(w, pw); await collect(r)                        # Confirm password
    await send(w, pw); await collect(r)                        # agreement
    await send(w, "y")
    return r, w, await collect(r, timeout=1.5)


async def name_only(port, name):
    """Connect and send just the name; return the server's next line."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name)
    return r, w, await collect(r)


async def login_full(port, name, pw="swordfish-x9"):
    """Send name, then password only if a password prompt appears. Returns the
    last output (either a refusal at the name step, or the post-login output)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name)
    prompt = await collect(r)
    if "Password" not in prompt:
        return r, w, prompt          # blocked before the password was ever asked
    await send(w, pw)
    return r, w, await collect(r, timeout=1.0)


async def start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


# ---- integration: .newusers ----------------------------------------------

@pytest.mark.asyncio
async def test_newusers_off_blocks_creation_but_allows_arch_from_loopback(tmp_path):
    # Boot with creation already closed and no accounts yet (lockout scenario).
    (tmp_path / "state.json").write_text(json.dumps(
        {"allow_new_accounts": False, "allow_logins": True}))
    srv, port = await start(make_config(tmp_path))
    try:
        # An unknown, non-arch name: exact no-creation line, no "Create it?".
        r, w, out = await name_only(port, "Stranger")
        assert "New accounts aren't being created right now." in out
        assert "Create it?" not in out
        w.close()

        # The arch name from loopback stays creatable (lockout-proofing).
        r, w, out = await create(port, "Generic")
        assert "atrium" in out.lower()
        assert srv.accounts.exists("Generic")

        # Arch flips it on; now a stranger can create.
        await send(w, ".newusers on")
        assert "Account creation is now on." in await collect(r)
        r2, w2, out2 = await create(port, "Latecomer")
        assert srv.accounts.exists("Latecomer")
        w.close(); w2.close()
    finally:
        await srv.aclose()


# ---- integration: .logins -------------------------------------------------

@pytest.mark.asyncio
async def test_logins_off_blocks_others_but_not_arch_or_grace(tmp_path):
    srv, port = await start(make_config(tmp_path))
    try:
        ar, aw, _ = await create(port, "Generic")       # the arch
        br, bw, _ = await create(port, "Bob")           # a resident, stays connected
        cr, cw, _ = await create(port, "Cara")          # will fully log out
        await send(cw, ".quit"); await collect(cr); cw.close()
        await asyncio.sleep(0.1)

        await send(aw, ".logins off")
        assert "Logins are now off." in await collect(ar)

        # An existing non-arch account is refused at the name — password never asked.
        r, w, out = await login_full(port, "Cara")
        assert "Commons is closed just now." in out
        assert "Password" not in out
        w.close()

        # The already-connected resident is untouched by the toggle.
        await send(bw, ".look")
        assert "atrium" in (await collect(br)).lower()

        # The arch can log in fresh while off. (Log the current arch out first.)
        await send(aw, ".quit"); await collect(ar); aw.close()
        await asyncio.sleep(0.1)
        ar, aw, out = await login_full(port, "Generic")
        assert "atrium" in out.lower()

        # A grace-window reattach still works while logins are off.
        bw.close()                                       # abrupt drop -> grace
        await asyncio.sleep(0.3)
        r, w, out = await login_full(port, "Bob")
        assert "Reconnected" in out                      # reattached, not refused
        await send(w, ".quit"); await collect(r); w.close()

        # Fresh relogin (fully logged out, not in grace) is still blocked.
        r, w, out = await login_full(port, "Bob")
        assert "Commons is closed just now." in out
        w.close()

        # Flip back on (via the live arch session): Cara can log in again.
        await send(aw, ".logins on")
        assert "Logins are now on." in await collect(ar)
        r, w, out = await login_full(port, "Cara")
        assert "atrium" in out.lower()
        w.close(); aw.close()
    finally:
        await srv.aclose()


# ---- integration: .logoutall ----------------------------------------------

@pytest.mark.asyncio
async def test_logoutall_disconnects_everyone_but_arch(tmp_path):
    srv, port = await start(make_config(tmp_path))
    try:
        ar, aw, _ = await create(port, "Generic")
        wr, ww, _ = await create(port, "Wizzy")
        rr, rw, _ = await create(port, "Res")
        await send(aw, ".promote Wizzy"); await collect(ar)   # Wizzy -> wizard
        await collect(wr)                                     # "made a wizard"

        await send(aw, ".logoutall")
        prompt = await collect(ar)
        # ⚠ "account(s)" since v1.47.0, and no "including yourself" for the arch:
        # the sweep reaches every non-arch RANK, so "resident(s)" understated it.
        assert "Log out 2 account(s)? (y/n)" in prompt         # staff + resident
        assert "including yourself" not in prompt
        await send(aw, "y")
        await collect(ar)

        # Both non-arch sessions get the line and are disconnected.
        wout, rout = await collect(wr), await collect(rr)
        assert "The house is closing for now." in wout
        assert "The house is closing for now." in rout
        assert await collect(wr) == "" and await collect(rr) == ""   # sockets closed

        # The arch is still live.
        await send(aw, ".look")
        assert "atrium" in (await collect(ar)).lower()
        aw.close(); ww.close(); rw.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_logoutall_no_leaves_everyone(tmp_path):
    srv, port = await start(make_config(tmp_path))
    try:
        ar, aw, _ = await create(port, "Generic")
        rr, rw, _ = await create(port, "Res")
        await send(aw, ".logoutall")
        assert "Log out 1 account(s)? (y/n)" in await collect(ar)
        await send(aw, "n")
        assert "Never mind." in await collect(ar)
        # Res is still connected.
        await send(rw, ".look")
        assert "atrium" in (await collect(rr)).lower()
        aw.close(); rw.close()
    finally:
        await srv.aclose()


# ---- persistence ----------------------------------------------------------

@pytest.mark.asyncio
async def test_toggles_persist_across_restart(tmp_path):
    cfg = make_config(tmp_path)
    srv, port = await start(cfg)
    try:
        ar, aw, _ = await create(port, "Generic")
        await send(aw, ".logins off"); await collect(ar)
        await send(aw, ".newusers off"); await collect(ar)
        aw.close()
    finally:
        await srv.aclose()

    # A fresh instance over the same state file sees the toggles.
    srv2 = TalkerServer(make_config(tmp_path))
    try:
        assert srv2.state.allow_logins is False
        assert srv2.state.allow_new_accounts is False
    finally:
        pass


def test_missing_state_file_defaults_on(tmp_path):
    """No state.json means the house is fully open — the historical behaviour.

    A first boot still writes the file (the toggles and the jail set live here).
    What it no longer writes is the command table — that moved to
    permissions.toml in v1.37.0. What matters is unchanged: the two
    switches are on, and the file that appears says so rather than closing
    anything.
    """
    import json
    srv = TalkerServer(make_config(tmp_path))
    assert srv.state.allow_logins is True
    assert srv.state.allow_new_accounts is True
    written = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert written["allow_logins"] is True
    assert written["allow_new_accounts"] is True
    # ⚠ And NO command table: it moved to permissions.toml in v1.37.0, so that
    # nothing the daemon writes here can overwrite an operator's edit there.
    assert "commands" not in written
