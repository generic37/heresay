# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Self-takeover at login + TCP keepalives on the socket doors.

Real sessions over the localhost socket (disconnects, transport close, re-entry),
so these run against a started TalkerServer. All state on tmp paths — never the
real root files.

The scenario that motivates this: a half-open connection leaves a live ghost the
server believes is connected, so the account is stuck; an authenticated owner must
be able to take over their own session — including the arch, whose lockout needed
a root restart before this existed.
"""

import asyncio
import logging
import socket as _socket
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _config(tmp_path):
    return Config(
        plain_host="127.0.0.1",
        plain_port=0,
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
        events_file=str(tmp_path / "events.jsonl"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
    )


@pytest.fixture
async def env(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    seed_account(store, "Arch", "apw")
    seed_account(store, "Mara", "mpw")
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


async def _password_then(port, name, pw):
    """Connect, submit name + password, and return (reader, writer, output-so-far)
    positioned right after the takeover prompt (for an already-connected account)."""
    r, w = await connect(port)
    await collect(r)
    await send(w, name)
    await collect(r)
    await send(w, pw)
    out = await collect(r)
    return r, w, out


# ---- takeover: the happy path -------------------------------------------

@pytest.mark.asyncio
async def test_takeover_y_moves_in_and_closes_old(env):
    srv, port = await env()
    r1, w1, _ = await login(port, "Mara", "mpw")
    # Move the first session somewhere non-default so we can prove the takeover
    # lands in the SAME room, not a fresh arrival at the start room.
    await send(w1, ".go concourse")
    await collect(r1)
    assert srv.world.get_resident("Mara").room_id == "concourse"

    r2, w2, out = await _password_then(port, "Mara", "mpw")
    assert "take over" in out.lower()
    await send(w2, "y")
    intro = await collect(r2)
    # New session is in the old session's room (concourse), not atrium.
    assert "concourse" in intro.lower()
    assert srv.world.get_resident("Mara").room_id == "concourse"

    # The OLD socket got the notice and was closed.
    tail = await collect(r1, timeout=1.0)
    assert "logged in from elsewhere" in tail.lower()
    assert r1.at_eof() or await collect(r1, timeout=0.5) == ""

    # Exactly one live session for Mara, and it's the new one.
    mara = srv.world.get_resident("Mara")
    assert mara is not None and mara.session is not None
    connected = [r for r in srv.world.all_connected() if r.name == "Mara"]
    assert len(connected) == 1
    w2.close()


@pytest.mark.asyncio
async def test_takeover_is_silent_to_the_room(env):
    """No departure/arrival churn: a bystander in the same room hears nothing."""
    srv, port = await env()
    r1, w1, _ = await login(port, "Mara", "mpw")            # in atrium
    ra, wa, _ = await login(port, "Arch", "apw")            # same start room
    await collect(ra)                                       # drain Mara's arrival etc.

    r2, w2, out = await _password_then(port, "Mara", "mpw")
    assert "take over" in out.lower()
    await send(w2, "y")
    await collect(r2)

    # The bystander saw no "arrives"/"departs" churn from the takeover.
    heard = await collect(ra, timeout=0.6)
    assert "enters the world" not in heard.lower()
    assert "leaves the world" not in heard.lower()
    for w in (wa, w2):
        w.close()


@pytest.mark.asyncio
async def test_wrong_password_refused_and_counts_backoff(env):
    srv, port = await env()
    r1, w1, _ = await login(port, "Mara", "mpw")
    r2, w2 = await connect(port)
    await collect(r2)
    await send(w2, "Mara")
    await collect(r2)
    await send(w2, "wrongpw")
    out = await collect(r2)
    # Ordinary failure — never a takeover offer, never "already connected".
    assert "wrong password" in out.lower()
    assert "take over" not in out.lower()
    assert "already connected" not in out.lower()
    # The failure was recorded (auth backoff now imposes a delay for this IP).
    assert srv.auth_backoff.delay("127.0.0.1") > 0
    # The original session is untouched.
    assert srv.world.get_resident("Mara").session is not None
    for w in (w1, w2):
        w.close()


@pytest.mark.asyncio
async def test_takeover_during_ghost_mid_editor_does_not_wedge(env):
    """The live session is mid multi-line editor (a blocked read inside .profile);
    a takeover must still displace it cleanly and land the new session in-world."""
    srv, port = await env()
    r1, w1, _ = await login(port, "Mara", "mpw")
    await send(w1, ".profile")
    await collect(r1)
    await send(w1, "half-written line, never saved")   # buffered in the editor
    await collect(r1)

    r2, w2, out = await _password_then(port, "Mara", "mpw")
    assert "take over" in out.lower()
    await send(w2, "y")
    intro = await collect(r2)
    assert "atrium" in intro.lower()
    # New session is live and NOT stuck in an editor — a plain command works.
    await send(w2, ".who")
    who = await collect(r2)
    assert "Mara" in who
    # Old socket closed; the unsaved buffer evaporated (profile still empty).
    tail = await collect(r1, timeout=1.0)
    assert "logged in from elsewhere" in tail.lower()
    assert srv.accounts.get("Mara").profile == ""
    w2.close()


@pytest.mark.asyncio
async def test_arch_takeover_unbricks_lockout(env):
    """The motivating case: the arch is 'already connected' (a ghost). The arch
    logs in again and takes over — no root restart needed."""
    srv, port = await env()
    r1, w1, _ = await login(port, "Arch", "apw")
    assert srv.world.get_resident("Arch").session is not None

    r2, w2, out = await _password_then(port, "Arch", "apw")
    assert "take over" in out.lower()
    await send(w2, "y")
    intro = await collect(r2)
    assert "atrium" in intro.lower()
    # Still the arch after takeover: an arch-only command is visible.
    await send(w2, ".accounts")
    assert "*** Accounts *" in await collect(r2)
    # Old session displaced.
    tail = await collect(r1, timeout=1.0)
    assert "logged in from elsewhere" in tail.lower()
    w2.close()


@pytest.mark.asyncio
async def test_takeover_recorded_as_a_fact(env, caplog):
    """The account and the address it was displaced FROM.

    v1.30.1 moved this out of journald into the event store (a takeover names a
    person, and the process log holds no names now). The property is unchanged:
    it is a fact — never a password, never a word anyone said.
    """
    srv, port = await env()
    r1, w1, _ = await login(port, "Mara", "mpw")
    caplog.set_level(logging.INFO, logger="heresay")
    r2, w2, out = await _password_then(port, "Mara", "mpw")
    await send(w2, "y")
    await collect(r2)

    rows = srv.events.read(types=("takeover",))
    assert [e["account"] for e in rows] == ["Mara"]
    assert rows[0]["from_ip"] == "127.0.0.1"
    assert set(rows[0]) == {"ts", "type", "account", "from_ip"}
    assert "mpw" not in srv.events.path.read_text(encoding="utf-8")
    # ...and nothing of it in the process log.
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("Mara" in m or "mpw" in m for m in msgs)
    for w in (w1, w2):
        w.close()


# ---- takeover on the web door -------------------------------------------

async def _ws_collect(ws, timeout=0.4):
    out = []
    while True:
        try:
            f = await asyncio.wait_for(ws.recv(), timeout)
        except Exception:
            break
        out.append(f if isinstance(f, str) else f.decode())
    return out


@pytest.mark.asyncio
async def test_takeover_works_on_the_web_door(env):
    srv, _port = await env()
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws1:
        await _ws_collect(ws1)                 # greeting + name prompt
        await ws1.send("Mara")
        await _ws_collect(ws1)                 # password prompt
        await ws1.send("mpw")
        await _ws_collect(ws1)                 # arrival
        assert srv.world.get_resident("Mara").session is not None

        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws2:
            await _ws_collect(ws2)
            await ws2.send("Mara")
            await _ws_collect(ws2)
            await ws2.send("mpw")
            frames = await _ws_collect(ws2)
            assert any("take over" in f.lower() for f in frames)
            await ws2.send("y")
            intro = await _ws_collect(ws2)
            assert any("atrium" in f.lower() for f in intro)

            # The first web session was displaced: it receives the notice and the
            # server closes the connection, so a further recv() raises.
            tail = await _ws_collect(ws1, timeout=1.0)
            assert any("logged in from elsewhere" in f.lower() for f in tail)
            with pytest.raises(ConnectionClosed):
                await asyncio.wait_for(ws1.recv(), timeout=1.0)

            # One live session for Mara.
            connected = [r for r in srv.world.all_connected() if r.name == "Mara"]
            assert len(connected) == 1


# ---- keepalives ----------------------------------------------------------

class _WriterStub:
    """Just the slice of asyncio.StreamWriter that _enable_keepalive touches."""
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, key):
        return self._sock if key == "socket" else None


@pytest.mark.asyncio
async def test_keepalive_options_set_on_accepted_connection(env):
    """End-to-end: the server enables keepalives on real accepted socket doors.

    We prove it by running the exact production helper against a genuinely
    accepted AF_INET TCP socket and reading the options back with getsockopt.
    """
    srv, _port = await env()

    lsock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(1)
    lport = lsock.getsockname()[1]

    client = _socket.create_connection(("127.0.0.1", lport))
    accepted, _addr = lsock.accept()
    try:
        # Before: keepalive is off on a freshly accepted socket.
        assert accepted.getsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE) == 0
        srv._enable_keepalive(_WriterStub(accepted))
        # After: SO_KEEPALIVE on, and each tuned knob at its configured default.
        assert accepted.getsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE) == 1
        if hasattr(_socket, "TCP_KEEPIDLE"):
            assert accepted.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPIDLE) == 120
        if hasattr(_socket, "TCP_KEEPINTVL"):
            assert accepted.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPINTVL) == 20
        if hasattr(_socket, "TCP_KEEPCNT"):
            assert accepted.getsockopt(_socket.IPPROTO_TCP, _socket.TCP_KEEPCNT) == 4
    finally:
        client.close()
        accepted.close()
        lsock.close()


@pytest.mark.asyncio
async def test_enable_keepalive_tolerates_missing_socket(env):
    """A writer with no underlying socket must not raise (best-effort tuning)."""
    srv, _port = await env()
    srv._enable_keepalive(_WriterStub(None))   # no exception


def test_keepalive_config_defaults():
    cfg = Config()
    assert cfg.tcp_keepidle == 120
    assert cfg.tcp_keepintvl == 20
    assert cfg.tcp_keepcnt == 4
