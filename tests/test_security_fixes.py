# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the v1.8.0 security fixes:

  C1 — arch-name registration gated on the local console listener, not an IP
  H1 — X-Forwarded-For trusted at the rightmost hop, validated as an IP
  H2 — argon2 hashing offloaded from the event loop
  H3 — control-char/ANSI stripped at the output sink
  M1 — the config arch cannot self-delete

All state is on tmp paths; the plaintext door is the only local console.
"""

import asyncio
import time
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.session import Session, strip_controls
from heresay.transport import SocketTransport, WebSocketTransport

from conftest import FakeSession, RecordingTransport

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _cfg(tmp_path, arch="Generic", trusted_proxy=""):
    return Config(
        plain_host="127.0.0.1", plain_port=0,
        ws_host="127.0.0.1", ws_port=0,
        trusted_proxy=trusted_proxy, owner_name=arch,
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


# ---- socket / ws helpers (mirrors test_arch_safety) ----------------------

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


# =====================================================================
# H3 — control-char / ANSI stripping at the sink
# =====================================================================

def test_strip_controls_removes_terminal_controls():
    # Every control byte is removed; the printable remainder stays. Without the
    # leading ESC, a leftover "[2J" is inert literal text, not a screen-clear.
    for payload in ("a\x1b[2Jb", "x\x07y", "z\x08w", "n\x00m", "c\rd"):
        out = strip_controls(payload)
        assert not any(c in out for c in "\x1b\x07\x08\x00\r")
    assert strip_controls("a\x1b[2Jb") == "a[2Jb"   # ESC gone, letters remain (defanged)


def test_strip_controls_preserves_text_newlines_tabs_and_utf8():
    assert strip_controls("héllo — 日本語 🐇") == "héllo — 日本語 🐇"
    assert strip_controls("line1\nline2\tindented") == "line1\nline2\tindented"
    assert strip_controls("plain words") == "plain words"


def _real_session(world):
    s = Session(RecordingTransport(supports_ansi=True), world,
                None, Config(wrap_columns=1000))
    s.dim_system = False
    s.show_time = False
    return s


def test_session_send_strips_controls(world):
    s = _real_session(world)
    s.send("Corvid says: \x1b[2J\x07gotcha")
    txt = s.transport.text
    assert "\x1b" not in txt and "\x07" not in txt   # no ANSI/BEL reaches the screen
    assert "gotcha" in txt                            # the words survive


def test_session_send_keeps_multibyte(world):
    s = _real_session(world)
    s.send("Corvid says: 日本語 café 🐇")
    assert "日本語 café 🐇" in s.transport.text


def test_session_send_raw_strips_controls(world):
    s = _real_session(world)
    s.send_raw("map\x1b[1;31m line\x07")
    assert "\x1b" not in s.transport.text and "\x07" not in s.transport.text


# =====================================================================
# H1 — X-Forwarded-For rightmost hop + validation
# =====================================================================

class _FakeWSConn:
    def __init__(self, peer_ip, xff=None):
        self.remote_address = (peer_ip, 54321)
        class _Req:
            headers = {}
        self.request = _Req()
        if xff is not None:
            self.request.headers = {"X-Forwarded-For": xff}


def test_ws_client_ip_takes_rightmost_trusted_hop(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    # A client forges a leftmost value; Caddy appends the real hop on the right.
    conn = _FakeWSConn("127.0.0.1", xff="6.6.6.6, 203.0.113.9")
    assert srv._ws_client_ip(conn) == "203.0.113.9"      # rightmost, not the forged 6.6.6.6


def test_ws_client_ip_forged_loopback_is_ignored(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    conn = _FakeWSConn("127.0.0.1", xff="127.0.0.1, 203.0.113.9")
    assert srv._ws_client_ip(conn) == "203.0.113.9"      # can't claim loopback


def test_ws_client_ip_invalid_xff_falls_back_to_peer(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    conn = _FakeWSConn("127.0.0.1", xff="not-an-ip")
    assert srv._ws_client_ip(conn) == "127.0.0.1"        # peer, on garbage


def test_ws_client_ip_ignores_xff_when_no_trusted_proxy(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, trusted_proxy=""))
    conn = _FakeWSConn("127.0.0.1", xff="203.0.113.9")
    assert srv._ws_client_ip(conn) == "127.0.0.1"        # trust nothing by default


# =====================================================================
# C1 — arch reservation gated on the local console, never an IP/header
# =====================================================================

def test_transport_console_flags():
    assert WebSocketTransport.is_local_console is False           # web never console
    assert SocketTransport(None, None, "127.0.0.1").is_local_console is False
    assert SocketTransport(None, None, "127.0.0.1",
                           is_local_console=True).is_local_console is True


@pytest.mark.asyncio
async def test_arch_refused_on_ws_even_when_ip_is_loopback(tmp_path):
    # Default trusted_proxy="" -> a WS client's derived IP is the loopback socket
    # peer (127.0.0.1). Pre-fix that read as "loopback" and allowed arch. Now the
    # console flag (false for WS) is what gates it, so it is refused.
    srv, _, ws_port = await _start(_cfg(tmp_path, trusted_proxy=""))
    from websockets.asyncio.client import connect as ws_connect
    try:
        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
            await ws_collect(ws)
            await ws.send("Generic")
            frames = await ws_collect(ws)
            assert any("That name isn't available." in f for f in frames)
            assert not any("Create it?" in f for f in frames)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_arch_refused_on_ws_with_forged_loopback_xff(tmp_path):
    srv, _, ws_port = await _start(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    from websockets.asyncio.client import connect as ws_connect
    try:
        async with ws_connect(f"ws://127.0.0.1:{ws_port}",
                              additional_headers={"X-Forwarded-For": "127.0.0.1"}) as ws:
            await ws_collect(ws)
            await ws.send("Generic")
            frames = await ws_collect(ws)
            assert any("That name isn't available." in f for f in frames)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_arch_created_from_local_console(tmp_path):
    # The plaintext listener IS the local console: arch registration is allowed.
    srv, port, _ = await _start(_cfg(tmp_path))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r)
        await send(w, "Generic")
        assert "Create it?" in await collect(r)
        await send(w, "yes")
        await collect(r)
        await send(w, "archpwxyz")
        await collect(r)
        await send(w, "archpwxyz")
        await collect(r)
        await send(w, "y")
        intro = await collect(r, timeout=2.0)
        assert "atrium" in intro.lower()
        w.close()
        assert srv.accounts.exists("Generic")
    finally:
        await srv.aclose()


# =====================================================================
# M1 — the config arch cannot self-delete
# =====================================================================

@pytest.mark.asyncio
async def test_arch_cannot_self_delete(tmp_path, world):
    srv = TalkerServer(_cfg(tmp_path, arch="Generic"))
    fs = FakeSession(world, name="Generic")
    result = await srv._deleteme(fs)
    assert result is False
    assert "can't be deleted" in fs.text.lower()
    await srv.aclose()


# =====================================================================
# H2 — argon2 hashing does not block the event loop
# =====================================================================

@pytest.mark.asyncio
async def test_hashing_does_not_block_the_loop(tmp_path, monkeypatch):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "password")        # sync API, for setup

    import heresay.accounts as A

    class _SlowHasher:                         # argon2's real verify is read-only;
        def verify(self, h, p):                # swap the whole hasher instead.
            time.sleep(0.15)                   # the sleep runs in the worker thread
            return True
        def check_needs_rehash(self, h):
            return False
    monkeypatch.setattr(A, "_hasher", _SlowHasher())

    ticks = 0

    async def ticker():
        nonlocal ticks
        for _ in range(30):
            await asyncio.sleep(0.005)         # ~0.15s of loop time
            ticks += 1

    ok, _ = await asyncio.gather(store.averify("Corvid", "password"), ticker())
    assert ok is True                          # correct password still verifies
    assert ticks >= 15                         # the loop kept running during the hash


@pytest.mark.asyncio
async def test_averify_rejects_wrong_password(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "password")
    assert await store.averify("Corvid", "password") is True
    assert await store.averify("Corvid", "nope") is False
    assert await store.averify("Nobody", "password") is False
