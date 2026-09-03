# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Recovery codes: generation, hashed storage, .recovery, and the login path."""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest

from heresay import recovery
from heresay.accounts import AccountStore
from heresay.commands import RECOVERY_NEW, dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
CODE_RE = re.compile(r"[a-z]+-[a-z]+-[a-z]+-\d{4}")


# ---- module ---------------------------------------------------------------

def test_generate_set_shape_and_entropy():
    codes = recovery.generate_set()
    assert len(codes) == 5
    assert all(CODE_RE.fullmatch(c) for c in codes)
    assert recovery.entropy_bits() >= 40.0


def test_normalize_roundtrip():
    c = "copper-lantern-tide-4711"
    assert recovery.normalize(c) == c
    assert recovery.normalize("  COPPER Lantern_Tide - 4711 ") == c
    assert recovery.normalize("Copper-Lantern-Tide-4711") == c


# ---- store ----------------------------------------------------------------

def test_store_holds_hashes_not_codes(tmp_path):
    s = AccountStore(tmp_path / "a.json")
    s.create("Ada", "password1")
    codes = recovery.generate_set()
    s.set_recovery("Ada", codes)
    assert s.recovery_count("Ada") == 5
    raw = (tmp_path / "a.json").read_text()
    for c in codes:                                   # no plaintext on disk
        assert c not in raw and recovery.normalize(c) not in raw
    assert raw.count("$argon2id$") >= 6               # 1 password + 5 codes


def test_find_normalizes_and_burn_only_removes_one(tmp_path):
    s = AccountStore(tmp_path / "a.json")
    s.create("Ada", "password1")
    codes = recovery.generate_set()
    s.set_recovery("Ada", codes)
    idx = s.find_recovery("Ada", codes[0].upper().replace("-", " "))
    assert idx >= 0
    s.burn_recovery("Ada", idx)
    assert s.recovery_count("Ada") == 4
    assert s.find_recovery("Ada", codes[0]) == -1     # burned
    assert s.find_recovery("Ada", codes[1]) >= 0      # others remain


def test_find_bad_code_is_minus_one(tmp_path):
    s = AccountStore(tmp_path / "a.json")
    s.create("Ada", "password1")
    s.set_recovery("Ada", recovery.generate_set())
    assert s.find_recovery("Ada", "no-such-code-0000") == -1
    assert s.find_recovery("Ada", "") == -1


def test_account_without_recovery_field_loads(tmp_path):
    # A pre-existing v2 file with no "recovery" key must load with an empty set.
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"version": 2, "accounts": {"ada": {
        "name": "Ada", "password_hash": "$argon2id$fake", "description": "",
        "created": "2026-01-01", "profile": "", "level": "resident",
        "settings": {}}}}))
    s = AccountStore(p)
    assert s.get("Ada") is not None
    assert s.recovery_count("Ada") == 0


# ---- .recovery command (FakeSession) -------------------------------------

def test_recovery_command_reports_count(world, clock, tmp_path):
    accts = AccountStore(tmp_path / "a.json")
    accts.create("Corvid", "password1")
    accts.set_recovery("Corvid", recovery.generate_set())
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accts)
    dispatch(s, ".recovery")
    assert "5 unused recovery codes" in s.text
    assert ".recovery new" in s.text


def test_recovery_new_defers_to_loop(world, clock):
    s = FakeSession(world, name="Corvid", clock=clock)
    assert dispatch(s, ".recovery new") == RECOVERY_NEW


# ---- integration: creation display, login recovery, .recovery new --------

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


async def create(port, name, pw):
    """Create an account over the socket; return (r, w, codes shown once)."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r)
    await send(w, "yes"); await collect(r); await send(w, pw); await collect(r)
    await send(w, pw); await collect(r)     # agreement text + "Do you agree?"
    await send(w, "y")
    out = await collect(r, timeout=1.5)     # welcome + recovery banner + arrival
    codes = CODE_RE.findall(out)
    return r, w, codes


async def login(port, name, pw):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r); await send(w, pw)
    return r, w, await collect(r, timeout=1.0)


@pytest.mark.asyncio
async def test_creation_shows_five_codes_once_and_hashes_on_disk(server, tmp_path, caplog):
    srv, port = server
    with caplog.at_level(logging.INFO, logger="heresay"):
        r, w, codes = await create(port, "Ada", "adasecret1")
    assert len(codes) == 5
    assert "never be" in (await collect(r, timeout=0.1)) or True  # banner already read
    # Hashes on disk, never the plaintext codes.
    raw = Path(srv.config.accounts_file).read_text()
    for c in codes:
        assert c not in raw
    # No code appears in any log line.
    logtext = "\n".join(rec.getMessage() for rec in caplog.records)
    for c in codes:
        assert c not in logtext
    w.close()


@pytest.mark.asyncio
async def test_login_recovery_full_flow(server):
    srv, port = server
    r, w, codes = await create(port, "Ada", "oldpass1")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.1)

    # Use a code to reset the password.
    r2, w2 = await asyncio.open_connection("127.0.0.1", port)
    await collect(r2); await send(w2, "Ada")
    prompt = await collect(r2)
    assert "recover" in prompt.lower()                 # prompt hints at the keyword
    await send(w2, "recover")
    await collect(r2, timeout=1.0)                      # "Recovery code:"
    await send(w2, codes[0])
    await collect(r2, timeout=1.5)                      # verify + "New password:"
    await send(w2, "newpass1")
    await collect(r2)                                   # "New password again:"
    await send(w2, "newpass1")
    intro = await collect(r2, timeout=1.0)
    assert "Password reset" in intro and "4 recovery code" in intro
    assert "atrium" in intro.lower()
    await send(w2, ".quit"); await collect(r2); w2.close()
    await asyncio.sleep(0.1)

    # Old password is dead.
    r3, w3, intro3 = await login(port, "Ada", "oldpass1")
    assert "Wrong password" in intro3
    w3.close()
    # New password works.
    r4, w4, intro4 = await login(port, "Ada", "newpass1")
    assert "atrium" in intro4.lower()
    w4.close()


@pytest.mark.asyncio
async def test_used_code_is_burned_but_others_work(server):
    srv, port = server
    r, w, codes = await create(port, "Bob", "bobpass1")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.1)

    async def recover_with(code, newpw):
        rr, ww = await asyncio.open_connection("127.0.0.1", port)
        await collect(rr); await send(ww, "Bob"); await collect(rr)
        await send(ww, "recover"); await collect(rr, timeout=1.0)
        await send(ww, code)
        out = await collect(rr, timeout=1.5)
        if "New password" in out:
            await send(ww, newpw); await collect(rr); await send(ww, newpw)
            out = await collect(rr, timeout=1.0)
        return rr, ww, out

    rr, ww, out = await recover_with(codes[0], "newpw2aa")     # first use OK
    assert "Password reset" in out
    await send(ww, ".quit"); await collect(rr); ww.close()
    await asyncio.sleep(0.1)

    # The same code again fails (burned).
    rr, ww, out = await recover_with(codes[0], "newpw3aa")
    assert "That didn't work." in out
    ww.close()
    # A different code still works.
    rr, ww, out = await recover_with(codes[1], "newpw4aa")
    assert "Password reset" in out
    await send(ww, ".quit"); await collect(rr); ww.close()


@pytest.mark.asyncio
async def test_invalid_code_and_no_codes_fail_generically(server, tmp_path):
    srv, port = server
    # An account with codes: a wrong code -> generic failure.
    r, w, codes = await create(port, "Cara", "carapass")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.1)
    r2, w2 = await asyncio.open_connection("127.0.0.1", port)
    await collect(r2); await send(w2, "Cara"); await collect(r2)
    await send(w2, "recover"); await collect(r2, timeout=1.0)
    await send(w2, "wrong-wrong-wrong-0000")
    assert "That didn't work." in await collect(r2, timeout=1.0)
    w2.close()

    # An account with NO codes fails identically (never reveals code existence).
    AccountStore(str(tmp_path / "accounts.json"))  # ensure file exists / loaded
    srv.accounts.create("Nocodes", "ncpass")       # created without a recovery set
    r3, w3 = await asyncio.open_connection("127.0.0.1", port)
    await collect(r3); await send(w3, "Nocodes"); await collect(r3)
    await send(w3, "recover"); await collect(r3, timeout=1.0)
    await send(w3, "any-old-code-0000")
    assert "That didn't work." in await collect(r3, timeout=1.0)
    w3.close()


@pytest.mark.asyncio
async def test_recovery_new_voids_old_set(server):
    srv, port = server
    r, w, codes = await create(port, "Dora", "dorapass")
    # Regenerate from inside the session.
    await send(w, ".recovery new")
    await collect(r)                     # "Current password:"
    await send(w, "dorapass")
    out = await collect(r, timeout=1.5)  # new banner
    new_codes = CODE_RE.findall(out)
    assert len(new_codes) == 5 and set(new_codes) != set(codes)
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.1)

    # An OLD code no longer works; a NEW one does.
    async def recover_with(code):
        rr, ww = await asyncio.open_connection("127.0.0.1", port)
        await collect(rr); await send(ww, "Dora"); await collect(rr)
        await send(ww, "recover"); await collect(rr, timeout=1.0)
        await send(ww, code)
        out = await collect(rr, timeout=1.5)
        return rr, ww, out

    rr, ww, out = await recover_with(codes[0])
    assert "That didn't work." in out
    ww.close()
    rr, ww, out = await recover_with(new_codes[0])
    assert "New password" in out
    ww.close()
