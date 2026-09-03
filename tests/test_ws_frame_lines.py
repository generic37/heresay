# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The web door's two structural bounds (v1.51.0, independent review).

Both findings came from a review by a model that did not write this engine, and
both are about the same thing: **a bound that existed by habit rather than by
construction.**

⛔ **F1 — one frame is one line.** The socket doors get this for free: the
newline IS their delimiter, so a line cannot contain one. The web door's
delimiter is the FRAME, and `_recv_frame` stripped only a *trailing* `\\r\\n` —
so a hostile non-browser client could put a newline in the middle. Because
`strip_controls` deliberately keeps `\\n` and `send()` splits output on it, one
frame painted several physical lines on every screen in the room.

⚠ **It was live, not theoretical.** `cmd_say` runs its body through `.strip()`
and nothing else — no `clean_message`, unlike `.desc`. Measured before the fix:

    sent one frame: 'hello\\n[*** Wren enters the world ***]'
    the room saw:   Mallory says: hello
                    [*** Wren enters the world ***]

A forged system line, from an ordinary say. Line-spoofing rather than XSS — but
forging who-said-what is an integrity hole in a presence talker.

⛔ **F2 — the outbound queue is bounded.** `_out` was the one unbounded buffer
in the engine. This is our own audit-3 finding F2 ("safe by accident, not by
`maxsize`") rediscovered independently — two reviewers on one line.

⚠ **The property F1 protects is NOT "one physical line".** Splitting a pasted
frame into several *attributed* lines is the intended behaviour, and is exactly
what the socket doors do. The property is that **nothing appears that the sender
did not say** — so the tests below check attribution, not line counts.
"""

import asyncio

import pytest

import heresay.transport as T
from heresay.transport import (MAX_FRAME_LINES, MAX_LINE_LENGTH, MAX_OUT_QUEUE,
                               WebSocketTransport)


# ---------------------------------------------------------------------------
# Fakes: a WS connection that hands out queued frames and can stop reading.
# ---------------------------------------------------------------------------

class _Conn:
    def __init__(self, frames=(), stall=False):
        self._frames = list(frames)
        self.sent = []
        self.stall = stall          # True: send() never returns (a dead reader)
        self.closed = False

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        await asyncio.sleep(3600)

    async def send(self, frame):
        if self.stall:
            await asyncio.sleep(3600)
        self.sent.append(frame)

    async def close(self):
        self.closed = True


def _t(frames=(), stall=False):
    return WebSocketTransport(_Conn(frames, stall), "203.0.113.7")


async def _drain_lines(t, n, timeout=2.0):
    out = []
    for _ in range(n):
        out.append(await asyncio.wait_for(t.recv_line(), timeout))
    return out


# ---------------------------------------------------------------------------
# F1 — one frame is one line
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sep,name", [
    ("\n", "LF"), ("\r\n", "CRLF"), ("\r", "CR"),
    ("\u2028", "LINE SEPARATOR"), ("\x0b", "VT"), ("\x0c", "FF"),
])
@pytest.mark.asyncio
async def test_no_separator_survives_inside_a_dispatched_line(sep, name):
    """⛔ The invariant. Whatever the peer uses as a line break, the dispatcher
    never receives a line containing one.

    `str.splitlines()` rather than a `\\n` split is what buys the exotic ones —
    stricter than the output side needs, and free."""
    t = _t([f"hello{sep}[*** Wren enters the world ***]"])
    try:
        lines = await _drain_lines(t, 2)
        assert lines == ["hello", "[*** Wren enters the world ***]"], name
        for line in lines:
            for ch in ("\n", "\r", "\u2028", "\x0b", "\x0c"):
                assert ch not in line, (name, line)
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_the_split_lines_arrive_ONE_AT_A_TIME_in_order():
    """Each is its own dispatched line, so each spends its own governor token —
    which is why splitting cannot amplify: five lines cost five tokens whether
    they arrive in one frame or five."""
    t = _t(["one\ntwo\nthree"])
    try:
        assert await _drain_lines(t, 3) == ["one", "two", "three"]
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_each_split_line_is_still_length_capped():
    """The per-line cap applies to every line out of a frame, not just the
    first — otherwise splitting would be a way past `MAX_LINE_LENGTH`."""
    t = _t(["a" * 3000 + "\n" + "b" * 3000])
    try:
        for line in await _drain_lines(t, 2):
            assert len(line) == MAX_LINE_LENGTH
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_an_ordinary_single_line_frame_is_untouched():
    """⚠ The regression that matters most: our own client sends single lines
    (its `collapse()` flattens `\\s+` to a space before sending), so the common
    path must be exactly as it was."""
    t = _t(["just a normal thing to say", "", ".look"])
    try:
        assert await _drain_lines(t, 3) == [
            "just a normal thing to say", "", ".look"]
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_a_trailing_newline_does_not_invent_a_blank_line():
    """`"a\\n".splitlines()` is `['a']` — the same as the socket door, where a
    line ends AT the newline rather than being followed by an empty one."""
    t = _t(["a\n"])
    try:
        assert await _drain_lines(t, 1) == ["a"]
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_a_frame_of_absurdly_many_lines_CLOSES_the_connection():
    """⚠ Splitting is a kindness to a client that pastes, not an invitation to
    submit a script in one send. Past `MAX_FRAME_LINES` the peer is answered the
    way the socket door answers a peer producing nothing usable (the `_raw` cap,
    v1.48.0): hang up."""
    t = _t(["x\n" * (MAX_FRAME_LINES + 5)])
    try:
        assert await asyncio.wait_for(t.recv_line(), 2.0) is None
        assert t.is_closing()
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_a_frame_AT_the_line_limit_is_still_served():
    """The bound is generous on purpose — a real paste must never reach it."""
    t = _t(["\n".join(f"line{i}" for i in range(MAX_FRAME_LINES))])
    try:
        lines = await _drain_lines(t, MAX_FRAME_LINES)
        assert lines[0] == "line0" and lines[-1] == f"line{MAX_FRAME_LINES - 1}"
        assert not t.is_closing()
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_the_HELLO_path_goes_through_the_same_splitter():
    """⛔ Otherwise the rule has a hole exactly the width of the login window.

    `wait_for_hello` pushes a non-control frame back rather than consuming it;
    pushed back WHOLE, a multi-line frame would reach the dispatcher intact.
    """
    t = _t(["typed\nahead of the hello"])
    try:
        await t.wait_for_hello(0.3)
        assert await _drain_lines(t, 2) == ["typed", "ahead of the hello"]
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_control_frames_are_unaffected():
    """A `\\x01` frame is consumed as before and never becomes input."""
    t = _t(["\x01width 40", "\x01resume", "hello"])
    try:
        assert await _drain_lines(t, 1) == ["hello"]
        assert t.reported_width == 40
        assert t.pending_resume == ""
    finally:
        t._sender.cancel()


def test_the_socket_door_is_structurally_immune_and_unchanged():
    """⚠ The reason the fix belongs at the web transport alone: on a socket the
    newline IS the delimiter, so `recv_line` cannot return a line containing
    one. Nothing there needed changing, and this says why in a way that would
    notice if it stopped being true."""
    import inspect

    from heresay.transport import SocketTransport
    src = inspect.getsource(SocketTransport.recv_line)
    assert 'find(b"\\n")' in src        # the newline IS the delimiter


# ---------------------------------------------------------------------------
# F1 — end to end, through a real room
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nothing_unattributed_can_be_forged_into_the_room(tmp_path):
    """⛔ The finding, as the room sees it.

    The payloads that worked before: a fake `[*** … ***]` presence line and a
    fake second speaker. Both must now arrive attributed to the person who typed
    them — which is what makes them harmless.
    """
    import pathlib

    import websockets
    from heresay.config import Config
    from heresay.server import TalkerServer
    from conftest import seed_account

    HOUSE = pathlib.Path(__file__).resolve().parent / "fixtures" / "house"
    PW = "correct horse battery"
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        owner_name="Generic", house_dir=str(HOUSE),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        forbidden_names_file=str(tmp_path / "forbidden.txt"),
        control_socket_file=str(tmp_path / "control.sock"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=200, reload_poll_seconds=0,
        max_conns_per_ip=20, churn_max_in_window=100)
    srv = TalkerServer(cfg)
    await srv.start()
    port = srv._servers[0].sockets[0].getsockname()[1]
    ws_port = next(s.getsockname()[1] for s in srv._ws_server.sockets)
    for n in ("Generic", "Mallory", "Victim"):
        seed_account(srv.accounts, n, PW)

    async def collect(r, t=0.5):
        buf = b""
        while True:
            try:
                c = await asyncio.wait_for(r.read(65536), t)
            except asyncio.TimeoutError:
                break
            if not c:
                break
            buf += c
        return buf.decode(errors="replace")

    try:
        vr, vw = await asyncio.open_connection("127.0.0.1", port)
        await collect(vr, 0.4)
        vw.write(b"Victim\n"); await vw.drain(); await collect(vr, 0.3)
        vw.write(PW.encode() + b"\n"); await vw.drain()
        assert "atrium" in (await collect(vr, 0.9)).lower()

        ws = await websockets.connect(f"ws://127.0.0.1:{ws_port}")
        got = []

        async def pump():
            try:
                async for f in ws:
                    got.append(f)
            except Exception:
                pass
        pumper = asyncio.create_task(pump())
        await ws.send("\x01width 200"); await ws.send("\x01resume")
        await asyncio.sleep(0.3)
        await ws.send("Mallory"); await asyncio.sleep(0.4)
        await ws.send(PW); await asyncio.sleep(1.0)
        assert any("atrium" in f.lower() for f in got)

        await collect(vr, 0.3)
        await ws.send("hello\n[*** Wren enters the world ***]")
        await asyncio.sleep(0.7)
        seen = await collect(vr, 0.5)
        shown = [l for l in seen.replace("\r\n", "\n").split("\n")
                 if l.strip() and l.strip() != ">"]
        assert shown, "the victim saw nothing at all"
        for line in shown:
            assert line.startswith("Mallory says:"), \
                f"an unattributed line reached the room: {line!r}"
        pumper.cancel()
        await ws.close()
        vw.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# F2 — the outbound queue is bounded by construction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_outbound_queue_declares_a_maxsize():
    """⛔ The structural claim. Our own audit called this "safe by accident, not
    by `maxsize`" — the session loop happens to `drain()` before the queue can
    grow. That protection stays; this is the belt that does not depend on where
    a call sits."""
    t = _t()
    try:
        assert t._out.maxsize == MAX_OUT_QUEUE
        assert MAX_OUT_QUEUE > 0
        # Deep enough that the biggest single screen cannot reach it: `.help`
        # is about 80 lines and is emitted line by line.
        assert MAX_OUT_QUEUE >= 200
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_a_stalled_reader_hits_the_bound_and_is_CLOSED():
    """⚠ Closed, not blocked on. Every caller is a synchronous handler pushing
    text at a room, so a blocking put would stall the whole house behind one
    browser that stopped reading — a liveness bug traded for a memory leak."""
    t = _t(stall=True)                    # send() never completes
    try:
        for i in range(MAX_OUT_QUEUE * 2):
            t.send_text(f"line {i}\n")
            if t.is_closing():
                break
        assert t.is_closing(), "the peer was never hung up on"
        assert t._out.qsize() <= MAX_OUT_QUEUE
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_a_normal_reader_is_completely_unaffected():
    """The bound must be invisible to anybody actually reading."""
    t = _t()
    try:
        for i in range(MAX_OUT_QUEUE * 3):
            t.send_text(f"line {i}\n")
            await asyncio.sleep(0)        # let the sender drain
        assert not t.is_closing()
        assert len(t._conn.sent) >= MAX_OUT_QUEUE
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_the_drain_protection_is_still_there():
    """A belt, not a replacement — `drain()` still waits on the queue."""
    import inspect
    src = inspect.getsource(WebSocketTransport.drain)
    assert "_out.join()" in src


# ---------------------------------------------------------------------------
# A2/A3 — an embedded \x01 is hostile, and a rejected frame leaves a record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_control_prefix_on_a_LATER_line_closes_the_connection():
    """⛔ The control convention is a FRAME-level thing.

    A frame that BEGINS with \\x01 is out-of-band and handled as such. A \\x01 on
    the second or later line of a frame is something else entirely: an attempt
    to smuggle a control verb in behind ordinary text. Nothing legitimate does
    it — `collapse()` in the shipped client flattens `\\s+` before sending, so a
    browser cannot emit a multi-line frame at all.
    """
    t = _t(["hello\n\x01resume STOLEN"])
    try:
        assert await asyncio.wait_for(t.recv_line(), 2.0) is None
        assert t.is_closing()
        assert t.rejected[0] == "control_byte"
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_the_LEGITIMATE_hello_control_frames_still_work():
    """⚠ The distinction that had to be preserved. The width and resume frames
    are the client's ordinary hello and must be unaffected."""
    t = _t(["\x01width 40", "\x01resume abc123", "hello"])
    try:
        assert await _drain_lines(t, 1) == ["hello"]
        assert t.reported_width == 40
        assert t.pending_resume == "abc123"
        assert not t.is_closing()
        assert t.rejected is None
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_a_control_byte_MID_LINE_is_not_hostile_merely_stripped():
    """⚠ The edge of the rule, pinned. `hello\\x01there` is ONE line that does
    not START with the prefix, so it cannot become a control frame for anybody —
    `strip_controls` removes the byte at the output sink. Rejecting it would be
    stricter than the threat."""
    t = _t(["hello\x01there"])
    try:
        assert await _drain_lines(t, 1) == ["hello\x01there"]
        assert not t.is_closing()
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_too_many_lines_names_its_reason():
    t = _t(["x\n" * (MAX_FRAME_LINES + 5)])
    try:
        assert await asyncio.wait_for(t.recv_line(), 2.0) is None
        assert t.rejected[0] == "too_many_lines"
    finally:
        t._sender.cancel()


@pytest.mark.asyncio
async def test_the_socket_doors_too_large_rejection_names_its_reason(monkeypatch):
    """⚠ `too_large` is the socket door's `_raw` cap. Since v1.48.0 bounded the
    telnet look-ahead it is unreachable in practice — so it is exercised the way
    that release's own test exercises it, by putting the old broken splitter
    back for one call. The reason exists and is wired; it fires only if the
    guard it belongs to ever becomes necessary again."""
    from heresay.transport import SocketTransport

    class _R:
        def __init__(self, chunks):
            self._c = list(chunks)

        async def read(self, n):
            if self._c:
                return self._c.pop(0)
            await asyncio.sleep(3600)

    class _W:
        def __init__(self):
            self.closed = False

        def is_closing(self):
            return self.closed

        def write(self, d):
            pass

        def close(self):
            self.closed = True

        async def drain(self):
            pass

    monkeypatch.setattr(T, "strip_telnet", lambda buf: (b"", bytes(buf)))
    t = SocketTransport(_R([b"Q" * 4096] * 4), _W(), "203.0.113.9")
    assert await asyncio.wait_for(t.recv_line(), 2.0) is None
    assert t.is_closing()
    assert t.rejected[0] == "too_large"


def test_transport_py_still_contains_NO_log_calls():
    """⛔ Why the transport SIGNALS and the events layer RECORDS.

    This module holds raw user bytes before anything has sanitised them, and
    "transport.py contains zero log calls" is a grep-provable guarantee worth
    keeping. Writing the `bad_frame` record here would have spent it.
    """
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "heresay" / "transport.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "log." not in stripped, line
        assert "logging" not in stripped, line


# ---------------------------------------------------------------------------
# A3 — the record, end to end through a real server
# ---------------------------------------------------------------------------

async def _house(tmp_path):
    import pathlib

    from heresay.config import Config
    from heresay.server import TalkerServer
    from conftest import seed_account

    HOUSE = pathlib.Path(__file__).resolve().parent / "fixtures" / "house"
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        owner_name="Generic", house_dir=str(HOUSE),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        forbidden_names_file=str(tmp_path / "forbidden.txt"),
        control_socket_file=str(tmp_path / "control.sock"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=200, reload_poll_seconds=0,
        max_conns_per_ip=20, churn_max_in_window=100)
    srv = TalkerServer(cfg)
    await srv.start()
    for n in ("Generic", "Mallory"):
        seed_account(srv.accounts, n, "correct horse battery")
    return srv, next(s.getsockname()[1] for s in srv._ws_server.sockets)


async def _ws_send(ws_port, frames, login=None):
    import websockets
    ws = await websockets.connect(f"ws://127.0.0.1:{ws_port}")
    got = []

    async def pump():
        try:
            async for f in ws:
                got.append(f)
        except Exception:
            pass
    t = asyncio.create_task(pump())
    await ws.send("\x01width 100")
    await ws.send("\x01resume")
    await asyncio.sleep(0.35)
    if login:
        await ws.send(login)
        await asyncio.sleep(0.4)
        await ws.send("correct horse battery")
        await asyncio.sleep(1.0)
    for f in frames:
        await ws.send(f)
    await asyncio.sleep(0.8)
    t.cancel()
    try:
        await ws.close()
    except Exception:
        pass
    await asyncio.sleep(0.4)
    return got


@pytest.mark.asyncio
async def test_a_rejected_frame_writes_one_bad_frame_event(tmp_path):
    """⛔ The maintainer wants the signal: a rejected frame must leave a record.

    ⚠ Written by the EVENTS layer, from `_serve`'s finally — the one place both
    the pre-auth and post-auth paths leave through — never by the transport,
    which logs nothing by design.
    """
    srv, ws_port = await _house(tmp_path)
    try:
        await _ws_send(ws_port, ["hello\n\x01resume STOLEN"], login="Mallory")
        rows = srv.events.read(types=("bad_frame",))
        assert len(rows) == 1, rows
        e = rows[0]
        assert e["reason"] == "control_byte"
        assert e["account"] == "Mallory"        # logged in: the name is there
        assert e["ip"]
        assert "STOLEN" in e["sample"]
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_PRE_AUTH_rejection_has_no_account(tmp_path):
    """⚠ And the absence is itself the signal: somebody probing before they
    have logged in."""
    srv, ws_port = await _house(tmp_path)
    try:
        await _ws_send(ws_port, ["\n".join(f"l{i}" for i in range(40))])
        rows = srv.events.read(types=("bad_frame",))
        assert len(rows) == 1
        assert rows[0]["reason"] == "too_many_lines"
        assert "account" not in rows[0]
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_sample_is_ESCAPED_and_capped_in_the_file(tmp_path):
    """⛔ Raw attacker bytes written into a file somebody later `cat`s, greps or
    displays is how a payload gets a second shot. Escaped it is inert and still
    readable."""
    from heresay.events import SAMPLE_MAX
    srv, ws_port = await _house(tmp_path)
    try:
        await _ws_send(ws_port, ["a\n\x01resume \x1b[2J" + "P" * 400])
        raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
        bad = [l for l in raw.splitlines() if '"bad_frame"' in l]
        assert len(bad) == 1
        line = bad[0]
        # No LITERAL control byte, escape or newline anywhere in the record.
        assert "\x01" not in line
        assert "\x1b" not in line
        sample = srv.events.read(types=("bad_frame",))[0]["sample"]
        assert "\n" not in sample
        assert "\\x01" in sample                 # escaped, and still legible
        assert len(sample) <= SAMPLE_MAX + 1     # capped (+1 for the ellipsis)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_LEGITIMATE_message_never_writes_a_bad_frame(tmp_path):
    """⛔ WHAT KEEPS THE COVENANT EXCEPTION NARROW.

    There must be no path from anything a resident legitimately said to a
    `sample` field — not a long message, not odd characters, not a flood. If
    this test ever needs relaxing, the exception has stopped being narrow and
    the reasoning in events.py no longer covers it.
    """
    srv, ws_port = await _house(tmp_path)
    try:
        await _ws_send(ws_port, [
            "an ordinary thing to say",
            "a much longer one " * 40,
            "punctuation!? — em-dashes, \"quotes\", 'apostrophes' & ampersands",
            ".look",
            ".help",
        ], login="Mallory")
        assert srv.events.read(types=("bad_frame",)) == []
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_security_shows_the_rejected_frame_count(tmp_path):
    """Visible at a glance without reading a log — a COUNT, like every other row
    on that screen. The reason and the sample stay in the event."""
    import asyncio as _a
    srv, ws_port = await _house(tmp_path)
    try:
        await _ws_send(ws_port, ["a\n\x01resume X"])
        port = srv._servers[0].sockets[0].getsockname()[1]
        r, w = await _a.open_connection("127.0.0.1", port)

        async def collect(t=0.5):
            buf = b""
            while True:
                try:
                    c = await _a.wait_for(r.read(65536), t)
                except _a.TimeoutError:
                    break
                if not c:
                    break
                buf += c
            return buf.decode(errors="replace")

        await collect(0.4)
        w.write(b"Generic\n"); await w.drain(); await collect(0.3)
        w.write(b"correct horse battery\n"); await w.drain(); await collect(0.9)
        w.write(b".security\n"); await w.drain()
        screen = await collect(0.8)
        assert "rejected frames" in screen
        assert "1" in screen
        w.close()
    finally:
        await srv.aclose()
