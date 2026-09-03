# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The telnet line parser — and the cost of absorbing what a stranger sends.

⛔ **The property under test is not "it parses"; it is that absorbing N bytes
costs O(N)** (v1.48.0, audit A1). Before this, an ``IAC SB`` with no ``IAC SE``
was an OPEN sequence forever: every byte after it came back as leftover, was
re-scanned on the next read, and re-scanned again. One pre-auth connection on
the public ``:2323`` door took time-to-greeting from 1 ms to 9-15 SECONDS for
the length of the whole login window, at a cost to the attacker of about 12 MB.

Measured then and now, absorbing an unterminated flood in 4 KB reads:

    v1.47.0   7.8 MB -> 504.5s cumulative CPU; one call at the end took 515 ms
    v1.48.0  15.6 MB ->   1.07s cumulative CPU; every call ~0.25 ms, leftover 0

⚠ The fix is in TWO places on purpose and they are not redundant. Bounding the
look-ahead (``MAX_SUBNEG_BYTES``) is the real repair: it removes the quadratic
behaviour at the source, so no future caller can reintroduce it. The ``_raw``
cap in ``recv_line`` is a BELT — with the parser fixed it cannot be reached
through any path known today, which is exactly the reason to keep it, since
that is a claim about today's parser rather than about every parser this file
will ever have. It is tested by simulating the regression it exists for.

⚠ And every previously-correct case has to STAY correct. A bound that also
broke a real client's ``IAC SB … IAC SE`` would be a worse bug than the one it
fixed, so the preservation tests below are not padding.
"""

import asyncio
import statistics
import time

import pytest

from heresay import transport as T
from heresay.transport import (IAC, IAC_SE, MAX_LINE_LENGTH, MAX_SUBNEG_BYTES,
                               SB, SE, SocketTransport, strip_telnet)

SB_OPEN = bytes([IAC, SB])              # begins a subnegotiation
WILL_ECHO = bytes([IAC, 251, 1])


# ---------------------------------------------------------------------------
# Fakes: just enough of a StreamReader/StreamWriter to drive recv_line.
# ---------------------------------------------------------------------------

class _FakeReader:
    """Hands out queued chunks, then blocks forever (a live, quiet peer)."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(3600)


class _FakeWriter:
    def __init__(self):
        self.closed = False
        self.written = bytearray()

    def is_closing(self):
        return self.closed

    def write(self, data):
        self.written.extend(data)

    def close(self):
        self.closed = True

    async def drain(self):
        pass


def _transport(chunks):
    return SocketTransport(_FakeReader(chunks), _FakeWriter(), "203.0.113.9")


# ---------------------------------------------------------------------------
# 1. ⛔ The cost. This is the finding.
# ---------------------------------------------------------------------------

def _feed(total_calls, chunk=b"A" * 4096, prefix=SB_OPEN):
    """Drive strip_telnet exactly as recv_line does, returning (times, leftovers)."""
    buf = bytearray(prefix)
    times, leftovers = [], []
    for _ in range(total_calls):
        buf.extend(chunk)
        t0 = time.perf_counter()
        _clean, left = strip_telnet(buf)
        times.append(time.perf_counter() - t0)
        leftovers.append(len(left))
        buf = bytearray(left)
    return times, leftovers


def test_an_unterminated_subnegotiation_leaves_NO_leftover_to_rescan():
    """⛔ The deterministic half of "linear", and the mechanism behind it.

    Quadratic cost came from carrying the whole tail forward as an open
    sequence. If the leftover stays bounded no matter how much arrives, there
    is nothing to re-scan and the cost cannot be superlinear. This assertion
    has no clock in it, so it cannot flake.
    """
    _times, leftovers = _feed(400)
    assert max(leftovers) <= MAX_SUBNEG_BYTES + 2, leftovers[:10]
    # In fact the resync consumes it outright.
    assert leftovers[-1] == 0


def test_absorbing_bytes_is_LINEAR_not_quadratic():
    """The audit's own measurement, as a test.

    Per-call cost must not grow with how much has already been absorbed. Under
    the old parser call 400 cost ~40x call 10 (and kept climbing); the bar here
    is deliberately loose — a shared CI box is noisy and the defect it must
    catch is a factor of hundreds, not of two.
    """
    _feed(20)                                   # warm up
    times, _ = _feed(400)
    early = statistics.median(times[5:25])
    late = statistics.median(times[-20:])
    assert late < early * 8, (
        f"per-call cost grew {late/early:.1f}x across the run "
        f"(early {early*1e6:.0f}us, late {late*1e6:.0f}us) — "
        f"absorbing input is superlinear again")


def test_repeated_SB_markers_cannot_make_each_one_rescan_a_window():
    """⚠ The trap in the fix itself.

    Bounding the look-ahead is not enough on its own: if an exhausted window
    only skipped the two `IAC SB` bytes, a peer sending `IAC SB` over and over
    would make every marker scan a fresh window — linear in N, but with a
    500x constant. Consuming the window is what charges the scan against the
    bytes it looked at. This is the input that tells the two apart.
    """
    times, leftovers = _feed(200, chunk=SB_OPEN * 2048, prefix=b"")
    assert max(leftovers) <= MAX_SUBNEG_BYTES + 2
    early = statistics.median(times[5:25])
    late = statistics.median(times[-20:])
    assert late < early * 8, f"grew {late/early:.1f}x on repeated SB markers"


# ---------------------------------------------------------------------------
# 2. ⚠ Everything that was already correct stays correct.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,raw,clean,leftover", [
    ("plain text",       b"hello\n",                          b"hello\n", 0),
    ("escaped 0xff",     bytes([IAC, IAC]) + b"x\n",          bytes([IAC]) + b"x\n", 0),
    ("WILL ECHO",        WILL_ECHO + b"hi\n",                 b"hi\n", 0),
    ("DO then DONT",     bytes([IAC, 253, 3, IAC, 254, 3]) + b"hi\n", b"hi\n", 0),
    ("complete SB",      bytes([IAC, SB, 24, 65, 66]) + IAC_SE + b"hi\n", b"hi\n", 0),
    ("SB around text",   b"a" + bytes([IAC, SB, 31, 0, 80]) + IAC_SE + b"b\n", b"ab\n", 0),
    ("NUL survives",     b"a\x00b\n",                         b"a\x00b\n", 0),
    ("bare CR survives", b"a\rb\n",                           b"a\rb\n", 0),
    ("ANSI survives",    b"\x1b[31mred\x1b[0m\n",             b"\x1b[31mred\x1b[0m\n", 0),
    ("lone IAC at tail", b"hi" + bytes([IAC]),                b"hi", 1),
    ("WILL cut short",   b"hi" + bytes([IAC, 251]),           b"hi", 2),
])
def test_previously_clean_cases_are_unchanged(name, raw, clean, leftover):
    got_clean, got_left = strip_telnet(bytearray(raw))
    assert got_clean == clean, name
    assert len(got_left) == leftover, name


def test_a_SHORT_subnegotiation_still_WAITS_for_the_rest():
    """⚠ The case the bound must not break.

    An `IAC SB` whose close simply has not arrived yet is a normal split read,
    not an attack, and it must still be held as leftover — otherwise a real
    client's negotiation would be chopped in half by a packet boundary. The
    line between this and the flood is the WINDOW, nothing else.
    """
    partial = bytes([IAC, SB, 24]) + b"B" * 40
    clean, left = strip_telnet(bytearray(b"hi\n" + partial))
    assert clean == b"hi\n"
    assert len(left) == len(partial)            # held, not discarded

    # ... and it completes when the rest turns up.
    clean2, left2 = strip_telnet(bytearray(left + IAC_SE + b"ok\n"))
    assert clean2 == b"ok\n"
    assert left2 == b""


def test_a_subnegotiation_right_AT_the_window_edge_still_completes():
    """The boundary itself: a payload that only just fits is a real one."""
    payload = b"C" * (MAX_SUBNEG_BYTES - 4)
    clean, left = strip_telnet(
        bytearray(bytes([IAC, SB, 24]) + payload + IAC_SE + b"fits\n"))
    assert clean == b"fits\n"
    assert left == b""


def test_a_subnegotiation_PAST_the_window_is_discarded_not_carried():
    """Past the window it is not a subnegotiation, whatever it claims to be.

    ⚠ The payload is discarded rather than spoken: the bytes we looked at are
    the bytes we consume. What follows the discarded run is ordinary input.
    """
    payload = b"D" * (MAX_SUBNEG_BYTES * 3)
    clean, left = strip_telnet(
        bytearray(bytes([IAC, SB, 24]) + payload + IAC_SE + b"after\n"))
    assert b"after\n" in clean
    assert clean.count(b"D") < len(payload)     # the window was eaten
    assert len(left) <= MAX_SUBNEG_BYTES + 2


# ---------------------------------------------------------------------------
# 3. The whole door: a flood drains, it does not stall.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_flood_behind_an_open_SB_drains_as_ordinary_input():
    """End to end through recv_line: the connection keeps answering.

    Before the fix this input produced NOTHING readable, ever — every byte sat
    in `_raw` being re-scanned. Now it drains as (junk) lines, which the login
    layer refuses like any other junk, and the buffer stays empty.
    """
    chunks = [SB_OPEN] + [b"E" * 4096 for _ in range(50)] + [b"\n"]
    t = _transport(chunks)
    line = await asyncio.wait_for(t.recv_line(), 5)
    assert line is not None and len(line) <= MAX_LINE_LENGTH
    assert len(t._raw) <= MAX_SUBNEG_BYTES + 2
    assert not t.is_closing()                   # ordinary junk, not a kill


@pytest.mark.asyncio
async def test_a_complete_subnegotiation_never_reaches_the_chat_core():
    """The negotiation is consumed; only the typed line comes back."""
    t = _transport([bytes([IAC, SB, 31, 0, 80, 0, 24]) + IAC_SE + b"Corvid\n"])
    assert await asyncio.wait_for(t.recv_line(), 5) == "Corvid"


# ---------------------------------------------------------------------------
# 4. The belt, tested as a belt.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_raw_cap_CLOSES_a_peer_that_yields_nothing(monkeypatch):
    """⛔ The `_raw` cap, exercised by simulating the regression it guards.

    With the look-ahead bounded, no input reaches this cap any more — so the
    only honest way to test it is to put the old behaviour back for one call:
    a parser that consumes nothing and hands everything forward. That is
    precisely the shape of the bug, and the door must now hang up on it rather
    than buffer without bound.
    """
    monkeypatch.setattr(T, "strip_telnet", lambda buf: (b"", bytes(buf)))
    t = _transport([b"F" * 4096 for _ in range(4)])
    assert await asyncio.wait_for(t.recv_line(), 5) is None
    assert t.is_closing()


@pytest.mark.asyncio
async def test_the_raw_cap_does_NOT_fire_while_real_input_is_arriving():
    """⚠ The other half: a peer sending a long line is not abusive.

    The cap is "kilobytes in, nothing readable out". Bytes that DO become
    clean input must never trip it, however many of them there are — that is
    what `_clean`'s own cap is for, and it truncates rather than disconnects.
    """
    t = _transport([b"G" * 4096 for _ in range(4)])
    line = await asyncio.wait_for(t.recv_line(), 5)
    assert line is not None and len(line) <= MAX_LINE_LENGTH
    assert not t.is_closing()


def test_the_window_is_a_named_constant_and_stays_generous():
    """A real subnegotiation is tens of bytes; the window is two orders more.

    Pinned so that "tighten it a bit" has to be a deliberate act — the cost of
    being wrong in that direction is a real client's negotiation being eaten.
    """
    assert MAX_SUBNEG_BYTES >= 256
    assert IAC_SE == bytes([IAC, SE])
