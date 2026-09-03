# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Arch-account safety: startup warning + reservation of the arch name.

While the configured arch account does not exist, its name may only be registered
from a loopback peer, so a remote stranger can't claim arch (config names the arch
by account name). All state here is on tmp paths — never the real root files.
"""

import asyncio
import logging
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as ws_connect

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import TalkerServer, _is_loopback

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _cfg(tmp_path, arch="Generic", trusted_proxy=""):
    return Config(
        plain_host="127.0.0.1",
        plain_port=0,
        ws_host="127.0.0.1",
        ws_port=0,
        trusted_proxy=trusted_proxy,
        owner_name=arch,
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


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1], \
        srv._ws_server.sockets[0].getsockname()[1]


# ---- socket / ws helpers -------------------------------------------------

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


async def ws_collect(ws, timeout=0.4):
    out = []
    while True:
        try:
            f = await asyncio.wait_for(ws.recv(), timeout)
        except Exception:
            break
        out.append(f if isinstance(f, str) else f.decode())
    return out


def _remote_ws(ws_port, ip="203.0.113.5"):
    # A connection that looks like it came through a trusted proxy from `ip`.
    return ws_connect(f"ws://127.0.0.1:{ws_port}",
                      additional_headers={"X-Forwarded-For": ip})


# ---- unit ----------------------------------------------------------------

def test_is_loopback():
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("127.0.0.5")
    assert _is_loopback("::1")
    assert not _is_loopback("203.0.113.5")
    assert not _is_loopback("?")       # unknown fails safe (not loopback)


# ---- startup warning -----------------------------------------------------

@pytest.mark.asyncio
async def test_startup_warns_when_arch_missing(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="heresay")
    srv, _, _ = await _start(_cfg(tmp_path))
    try:
        msgs = [r.getMessage() for r in caplog.records]
        assert any("owner account 'Generic' does not exist" in m for m in msgs)
        assert any("only be registered from the localhost listener" in m for m in msgs)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_no_startup_warning_when_arch_present(tmp_path, caplog):
    AccountStore(tmp_path / "accounts.json").create("Generic", "archpw")
    caplog.set_level(logging.WARNING, logger="heresay")
    srv, _, _ = await _start(_cfg(tmp_path))
    try:
        assert not any("does not exist" in r.getMessage() for r in caplog.records)
    finally:
        await srv.aclose()


# ---- reservation ---------------------------------------------------------

@pytest.mark.asyncio
async def test_arch_name_refused_from_remote(tmp_path):
    srv, _, ws_port = await _start(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    try:
        async with _remote_ws(ws_port) as ws:
            await ws_collect(ws)          # greeting + name prompt
            await ws.send("Generic")
            frames = await ws_collect(ws)
            assert any("That name isn't available." in f for f in frames)
            assert not any("Create it?" in f for f in frames)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_arch_name_from_loopback_creates_and_becomes_arch(tmp_path):
    srv, port, _ = await _start(_cfg(tmp_path))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r)                  # greeting + name prompt
        await send(w, "Generic")
        assert "Create it?" in await collect(r)   # allowed from loopback
        await send(w, "yes")
        await collect(r)
        await send(w, "archpwxyz")
        await collect(r)
        await send(w, "archpwxyz")
        await collect(r)                        # agreement text + "Do you agree?"
        await send(w, "y")
        intro = await collect(r, timeout=1.5)   # + recovery-code hashing
        assert "atrium" in intro.lower()      # created + arrived
        # It is arch at login: an arch/wizard-only command is visible.
        await send(w, ".accounts")
        assert "Accounts" in await collect(r)
        w.close()
        # And it was actually persisted as an account.
        assert srv.accounts.exists("Generic")
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_non_arch_name_unaffected_from_remote(tmp_path):
    srv, _, ws_port = await _start(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    try:
        async with _remote_ws(ws_port) as ws:
            await ws_collect(ws)
            await ws.send("Bob")
            frames = await ws_collect(ws)
            assert any("Create it?" in f for f in frames)   # ordinary name, allowed
            assert not any("reserved" in f.lower() for f in frames)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_existing_arch_account_not_reserved(tmp_path):
    # When the arch account already exists, the name isn't "reserved" — a remote
    # peer is simply asked to log in (existing-arch behavior is unchanged).
    AccountStore(tmp_path / "accounts.json").create("Generic", "archpw")
    srv, _, ws_port = await _start(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    try:
        async with _remote_ws(ws_port) as ws:
            await ws_collect(ws)
            await ws.send("Generic")
            frames = await ws_collect(ws)
            assert any("password" in f.lower() for f in frames)
            assert not any("reserved" in f.lower() for f in frames)
    finally:
        await srv.aclose()
