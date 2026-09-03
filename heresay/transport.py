# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Transports: the doors a Session can arrive through.

The chat core is transport-blind. A ``Session`` holds one of these and only ever
speaks in whole lines of text plus two out-of-band signals — a bare prompt and
an echo on/off toggle for password masking. Each transport decides how those map
to bytes on the wire:

- ``SocketTransport``  — TLS and plaintext socket doors. UTF-8 lines with CRLF,
  telnet IAC negotiation stripped on input, and minimal ``IAC WILL/WONT ECHO``
  emitted around password prompts (the one deliberate slice of telnet we speak).
- ``WebSocketTransport`` — the browser door. One text frame per line — and
  since v1.51.0 that is ENFORCED rather than assumed: a frame carrying newlines
  is split into the lines it contains, so the dispatcher gets the same
  one-line-at-a-time guarantee the socket doors get from their delimiter. Echo
  toggles ride as ``\\x01echo off`` / ``\\x01echo on`` control frames.

Ephemerality note: nothing here logs or buffers line content beyond what is
needed to deliver it, and no error path stringifies user input.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from websockets.exceptions import ConnectionClosed

# Telnet
IAC = 255
WILL = 251
WONT = 252
ECHO = 1
SE = 240                      # end of subnegotiation
SB = 250                      # begin subnegotiation
IAC_SE = bytes([IAC, SE])     # the two bytes that close a subnegotiation

# ⛔ How far past an ``IAC SB`` we will look for its ``IAC SE`` before deciding
# the peer is not speaking telnet (v1.48.0, audit A1).
#
# A real subnegotiation is TENS of bytes — NAWS is 9, TERMINAL-TYPE around 40 —
# and this engine does not implement any of them; it only refuses to let them
# reach the chat core. So the window is generous by two orders of magnitude and
# still a window.
#
# ⚠ The bound is not politeness, it is the whole fix. Without it an ``IAC SB``
# with no ``IAC SE`` is an OPEN sequence forever: every byte after it is
# returned as leftover, re-scanned on the next read, and re-scanned again — so
# absorbing N bytes cost O(N²), and one pre-auth connection could hold the event
# loop for the length of the login window. Bounding the look-ahead makes each
# byte cost O(1) whatever the peer sends. See `strip_telnet`.
MAX_SUBNEG_BYTES = 512

# WebSocket control convention: frames beginning with this byte are never shown.
CONTROL_PREFIX = "\x01"

# The longest a single input line may be, on EVERY door. The socket doors
# truncate here; the browser door truncates its decoded frame to match, so one
# door can't accept a megabyte line the other would clip to a kilobyte.
MAX_LINE_LENGTH = 1024

# ⛔ The most lines ONE WebSocket frame may yield (v1.51.0). A frame is supposed
# to be a line; splitting is a kindness to a client that pastes, not an invitation
# to submit a script.
#
# ⚠ Generous by construction: `WS_MAX_FRAME` is 8 KB and a line caps at 1024
# characters, so a legal frame can hold at most eight FULL lines — this is twice
# that, and far beyond the six visual rows our own compose box can even hold.
# Nothing a person types or pastes reaches it; a frame that does is a peer
# submitting a burst under cover of one send, and gets hung up on.
MAX_FRAME_LINES = 16

# ⛔ The most OUTBOUND frames that may sit queued for one browser (v1.51.0).
#
# `_out` was the one unbounded buffer in an otherwise rigorously-bounded engine:
# a peer that stops reading applies TCP backpressure, `_conn.send()` blocks, and
# anything the room says to that session piles up in memory. Our own audit
# called this "safe by accident, not by `maxsize`" — the session loop happens to
# `drain()` after every command, which stalls the loop before the queue can grow
# — and an independent reviewer found the same spot. Two people on one line is
# enough; the bound is structural now.
#
# ⚠ 256 is chosen against the biggest single thing the engine sends: `.help` is
# about 80 lines, the map and a full mailbox are of that order, and one output
# unit is emitted line by line. So the depth holds the largest legitimate screen
# several times over, and a session cannot hit it while anybody is reading.
#
# On overflow the connection is CLOSED rather than the put blocking: a peer that
# will not read is already gone, and waiting on it would hold up the room it is
# standing in. That is the difference between a bound and a deadlock.
MAX_OUT_QUEUE = 256

CTRL_ECHO_OFF = "\x01echo off"
CTRL_ECHO_ON = "\x01echo on"
# .reboot @sound cue (web door only): the client synthesises the named hardware
# sound. Rides the same \x01 convention; never displayed. See reboot.py / web.
CTRL_SOUND = "\x01sound "  # + <name>
# The background-tab mark (web door only): tells the client whether THIS resident
# wants it. Sent at login and again whenever they change `.ignore tab_alert`.
# The mark itself lives entirely in the browser, so the setting has to be pushed
# to it — there is nothing server-side to switch off.
CTRL_TAB_ALERT = "\x01tab_alert "  # + on|off
# House-wide values the WEB client needs but cannot know. All pushed on connect
# (and on change, for the ones the owner can retune), all web-only, all ignored
# by an older client — an unrecognised \x01 frame falls through its dispatch and
# is dropped, never printed.
CTRL_RAISE_BOUNDS = "\x01raise_bounds "   # + "<min> <max>"
CTRL_TITLE = "\x01title "                 # + the instance name, for the tab
CTRL_FAVICON = "\x01favicon "             # + a data: URL (or empty = blank)
# Whether this connection is IN-WORLD: authenticated, attached to a resident,
# standing in a room. Sent `on` the moment a session enters (every route — fresh
# login, grace reattach, self-takeover, token resume — funnels through
# `_load_prefs`), and never sent at all before that.
#
# It exists for exactly one thing: the background-tab mark must not blink at a
# login prompt or a dead socket (v1.25.1). A greeting, a password prompt and a
# "Connection closed." notice are not the room talking to you, and the covenant
# amendment's whole claim is that the mark means "something happened HERE".
CTRL_INWORLD = "\x01inworld "             # + on|off

# ---------------------------------------------------------------------------
# The ONE frame that travels the other way: client -> server.
#
# `\x01width <cols>` — the browser reporting its usable monospace columns, sent
# as the first frame on connect and again on every resize/orientation change.
# It is the same `\x01<verb> <arg>` shape as everything above, deliberately, so
# there is one convention on the wire rather than two.
#
# It is the first thing a client has ever told the server out of band, so the
# handling is deliberately small and total: parse one integer, clamp it, store
# it, and drop anything else without comment. It arrives PRE-AUTH, so it may
# never allocate, never log, and never be able to become a resource sink.
#
# Covenant note: one integer about the SCREEN. It is never content, never
# logged, never persisted, and dies with the connection.
# ---------------------------------------------------------------------------
CTRL_WIDTH = "\x01width "                 # + <cols>

# `\x01resume <token>` — travels BOTH ways, and means something different each
# direction, which is the whole handshake:
#
#   client -> server   "here is the token you gave this device" (or empty: none)
#   server -> client   "store this token"  /  empty: "forget the one you have"
#
# The client always sends its half on connect, immediately after the width
# report, EVEN WHEN IT HAS NO TOKEN — an empty frame. That is not politeness: it
# tells the server the hello is finished, so the bounded pre-banner wait can stop
# at once instead of sitting out its whole timeout on every single login.
CTRL_RESUME = "\x01resume"                # + " <token>", or bare for "none"

# A token is 43 URL-safe characters; anything much longer is not one of ours.
# Bounded here so a stranger cannot make us carry a megabyte before auth.
MAX_RESUME_FRAME = 512

# A reported width outside this range is a broken or hostile client, not a
# phone; ignore it rather than trying to lay a screen out for it.
MIN_REPORTED_WIDTH = 1
MAX_REPORTED_WIDTH = 1000


def parse_width_frame(line: str) -> Optional[int]:
    """``\\x01width 42`` -> ``42``. Anything else -> ``None``.

    Strict on purpose: a bare integer, in range, nothing else. Junk is ignored
    silently rather than answered — an unparseable frame from a client we cannot
    identify is not worth a reply, and answering it would be a way to make the
    server talk before anyone has logged in.
    """
    if not line.startswith(CTRL_WIDTH):
        return None
    try:
        cols = int(line[len(CTRL_WIDTH):].strip())
    except ValueError:
        return None
    if not (MIN_REPORTED_WIDTH <= cols <= MAX_REPORTED_WIDTH):
        return None
    return cols


def parse_resume_frame(line: str) -> Optional[str]:
    """``\\x01resume abc`` -> ``"abc"``; ``\\x01resume`` -> ``""``; else None.

    Returning ``""`` for a bare frame is meaningful and distinct from ``None``:
    ``""`` is a client saying "I have no token, and I am done talking", which is
    what lets the hello wait finish early. ``None`` means "not a resume frame at
    all". Junk and over-long frames are simply not resume frames.
    """
    if not line.startswith(CTRL_RESUME):
        return None
    rest = line[len(CTRL_RESUME):]
    if rest and not rest.startswith(" "):
        return None                       # `\x01resumexyz` is not this verb
    if len(line) > MAX_RESUME_FRAME:
        return None
    return rest.strip()


def strip_telnet(buf: bytearray) -> tuple[bytes, bytes]:
    """Remove complete telnet IAC sequences from ``buf``.

    Returns (clean_bytes, leftover) where leftover is a trailing, incomplete IAC
    sequence to be re-processed once more bytes arrive. We do not implement
    telnet — we just refuse to let its negotiation bytes reach the chat core.

    ⛔ **Absorbing N bytes costs O(N), and that is a property to protect**
    (v1.48.0, audit A1). Every branch below either consumes what it looked at or
    stops on a genuinely short tail; nothing re-reads the same bytes on the next
    call. The subnegotiation branch is the one that got this wrong — see
    ``MAX_SUBNEG_BYTES`` — and it is also the only branch that *can*, because it
    is the only one whose length is decided by the peer.
    """
    out = bytearray()
    i = 0
    n = len(buf)
    while i < n:
        b = buf[i]
        if b != IAC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            break  # lone IAC at tail; wait for more
        c = buf[i + 1]
        if c == IAC:  # escaped literal 0xFF
            out.append(IAC)
            i += 2
        elif c in (251, 252, 253, 254):  # WILL/WONT/DO/DONT + option byte
            if i + 2 >= n:
                break
            i += 3
        elif c == SB:  # SB ... IAC SE (subnegotiation)
            # Look for the close, but only within a bounded window, and with a
            # C-level search rather than a Python byte loop.
            window_end = min(n, i + 2 + MAX_SUBNEG_BYTES)
            j = buf.find(IAC_SE, i + 2, window_end)
            if j != -1:
                i = j + 2               # a complete subnegotiation: skip it
            elif window_end < n:
                # ⛔ No close within the window, and the peer has sent MORE
                # after it: this is not a subnegotiation. Discard the whole run
                # we looked at and resync past it — never carry it forward as
                # an open sequence. Consuming the window is what makes the scan
                # pay for itself: the cost of looking at those bytes is charged
                # against those same bytes, once, so a peer that sends `IAC SB`
                # over and over cannot make each marker re-scan a window.
                i = window_end
            else:
                break                   # still inside the window; wait for more
        else:  # other two-byte commands
            i += 2
    return bytes(out), bytes(buf[i:])


class SocketTransport:
    """A raw asyncio socket (plaintext or TLS)."""

    wants_prompt = True
    supports_ansi = True   # `.set color` emits minimal ANSI on the socket doors
    # .reboot @sound: the socket doors have no audio channel, so a cue falls back
    # to its built-in BEL pattern (reboot.SOUND_BEL) rather than a control frame.
    supports_sound_frames = False
    # No \x01 control convention on a socket: a terminal would PRINT the frame.
    # Anything that rides one is web-only by construction, not by an if.
    supports_control_frames = False
    # A socket door cannot report its width and never goes narrow (see
    # session.resolve_width). None means "never told us", which is exactly true.
    reported_width = None
    # Nor can it present a resume token: remember-me is web-only by construction,
    # not by an `if` at the call site. A telnet/TLS/console session simply has
    # nothing here for the login layer to find.
    pending_resume = None

    # ⛔ HOW A DOOR REPORTS THAT IT HUNG UP ON A HOSTILE PEER (v1.52.0).
    #
    # `(reason, sample)` once set, else None. The transport SIGNALS; the server
    # RECORDS. That split is deliberate and worth keeping: this module holds raw
    # user bytes before anything has sanitised them, and "transport.py contains
    # zero log calls" is a grep-provable guarantee. Writing the record here would
    # spend it — so the fact travels up to the events layer, whose job it is.
    rejected = None

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 peer_ip: str, is_local_console: bool = False):
        self._reader = reader
        self._writer = writer
        self.peer_ip = peer_ip
        # True ONLY for a connection accepted on the local plaintext console
        # listener (127.0.0.1:2324). The arch name may be registered only when
        # this is set — never inferred from a peer IP or a forwarded header.
        self.is_local_console = is_local_console
        self._raw = bytearray()    # bytes awaiting telnet processing
        self._clean = bytearray()  # cleaned bytes awaiting a newline

    async def recv_line(self, max_len: int = MAX_LINE_LENGTH) -> Optional[str]:
        """Read one logical line as text, or None at EOF.

        Tolerates CRLF and strips telnet negotiation. Over-long lines are
        truncated rather than buffered without bound.

        A line beginning with the control prefix is DROPPED, exactly as on the
        web door — this door has no control convention to interpret, but a
        Ctrl-A-prefixed line should not become speech either. One rule on both
        doors is easier to reason about than two.

        ⛔ **TWO buffers, and BOTH are bounded** (the second since v1.48.0,
        audit A1). ``_clean`` has always been capped — but no byte reaches
        ``_clean`` while a telnet sequence is open, so that cap could not see
        the case that mattered, and ``_raw`` had none at all. Now a peer whose
        raw buffer has grown past the cap while producing NOTHING readable is
        answered by closing the connection: nothing legitimate needs kilobytes
        of unterminated negotiation, and a door with an unbounded buffer in
        front of the login prompt is a door anyone can lean on.

        ⚠ It is a BELT. With the look-ahead bounded, ``strip_telnet`` now
        always makes progress, so the leftover cannot reach the cap through any
        path known today — which is exactly why the check is worth keeping,
        because that is a statement about today's parser rather than about
        every parser this file will ever have.
        """
        while True:
            nl = self._clean.find(b"\n")
            if nl != -1:
                raw_line = bytes(self._clean[:nl])
                del self._clean[: nl + 1]
                line = raw_line.rstrip(b"\r")
                if len(line) > max_len:
                    line = line[:max_len]
                text = line.decode("utf-8", errors="replace")
                if text.startswith(CONTROL_PREFIX):
                    continue   # never interpreted here, never spoken
                return text

            if len(self._clean) > max_len * 4:
                chunk = bytes(self._clean[:max_len])
                self._clean.clear()
                return chunk.decode("utf-8", errors="replace")

            data = await self._reader.read(4096)
            if not data:
                if self._clean:
                    line = bytes(self._clean).rstrip(b"\r")
                    self._clean.clear()
                    return line.decode("utf-8", errors="replace")
                return None
            self._raw.extend(data)
            clean, leftover = strip_telnet(self._raw)
            self._clean.extend(clean)
            self._raw = bytearray(leftover)
            if not clean and len(self._raw) > max_len * 4:
                # Kilobytes in, nothing readable out: not a client. Close, and
                # let the ordinary disconnect path clean up — the same thing an
                # EOF does, because to everything above here it IS one.
                #
                # ⚠ The sample is taken from the RAW buffer and escaped by the
                # events layer, never here: this file does not log.
                self.rejected = ("too_large",
                                 bytes(self._raw[:200]).decode("utf-8", "replace"))
                self.close()
                return None

    def send_text(self, text: str) -> None:
        if self._writer.is_closing():
            return
        data = text.replace("\r\n", "\n").replace("\n", "\r\n")
        try:
            self._writer.write(data.encode("utf-8", errors="replace"))
        except (ConnectionError, OSError):
            pass

    def prompt(self) -> None:
        self.send_text("> ")

    def set_echo(self, on: bool) -> None:
        # Server WILL ECHO tells a telnet client to stop echoing locally, which
        # masks the password; WONT ECHO hands echo back. Clients that ignore the
        # negotiation are no worse off than before.
        if self._writer.is_closing():
            return
        seq = bytes([IAC, WONT if on else WILL, ECHO])
        try:
            self._writer.write(seq)
        except (ConnectionError, OSError):
            pass

    async def drain(self) -> None:
        try:
            await self._writer.drain()
        except (ConnectionError, OSError):
            pass

    def close(self) -> None:
        try:
            self._writer.close()
        except (ConnectionError, OSError):
            pass

    def is_closing(self) -> bool:
        return self._writer.is_closing()


class WebSocketTransport:
    """A browser WebSocket connection: one text frame per line."""

    wants_prompt = False   # the web client draws its own input line
    supports_ansi = False  # the browser has its own themes; never send it ANSI
    # .reboot @sound: the browser synthesises hardware sound from a \x01sound
    # <name> control frame (WebAudio, in web/index.html).
    supports_sound_frames = True
    # The \x01 convention: the client reads these and never displays them.
    supports_control_frames = True
    is_local_console = False   # the browser door can never be the local console

    def __init__(self, connection, peer_ip: str):
        self._conn = connection
        self.peer_ip = peer_ip
        self._out: asyncio.Queue = asyncio.Queue(maxsize=MAX_OUT_QUEUE)
        self._closed = False
        # The browser's last reported usable columns, or None if it has never
        # said. One integer, replaced in place on every report — nothing
        # accumulates, nothing is stored, nothing is logged.
        self.reported_width: Optional[int] = None
        # The token this browser presented on connect, or None if it never sent
        # a resume frame at all. "" means it sent one and has no token. Consumed
        # ONCE by the login layer, which clears it — the transport never reads it.
        self.pending_resume: Optional[str] = None
        # Why this door hung up, if it did. See SocketTransport.rejected.
        self.rejected = None
        # One-slot pushback for the pre-banner width wait: if the very first
        # frame turns out to be a real typed line rather than the expected width
        # report, it is put back here instead of eaten. A fast typer must never
        # lose their first line to a timing optimisation.
        self._pushback: list[str] = []
        # Output frames are queued and flushed by one task so that synchronous
        # command handlers can "send" without awaiting.
        self._sender = asyncio.create_task(self._run_sender())

    def _mark_closed(self) -> None:
        # Once closed, no more frames will ever be sent; discard anything queued
        # so a pending drain()/queue.join() cannot block forever.
        self._closed = True
        while not self._out.empty():
            try:
                self._out.get_nowait()
                self._out.task_done()
            except asyncio.QueueEmpty:
                break

    async def _run_sender(self) -> None:
        try:
            while True:
                frame = await self._out.get()
                try:
                    await self._conn.send(frame)
                except ConnectionClosed:
                    self._out.task_done()
                    self._mark_closed()
                    return
                self._out.task_done()
        except asyncio.CancelledError:
            self._mark_closed()
            return

    def _note_control(self, line: str) -> bool:
        """Apply one inbound control frame. Unknown verbs are dropped in silence.

        Each verb is a single assignment, so a client that spams them costs
        nothing and can grow nothing. Returns True if this frame ENDS the opening
        hello (see ``wait_for_hello``).
        """
        cols = parse_width_frame(line)
        if cols is not None:
            self.reported_width = cols
            return False
        token = parse_resume_frame(line)
        if token is not None:
            # Held for the login layer to consume, exactly as reported_width is
            # held for the render layer. The transport does not interpret it: it
            # has no idea what a token means and must not learn.
            self.pending_resume = token
            return True                   # the client's hello is complete
        return False

    async def _recv_frame(self) -> Optional[str]:
        """One raw frame off the wire, decoded and stripped, or None at EOF."""
        try:
            frame = await self._conn.recv()
        except ConnectionClosed:
            self._closed = True
            return None
        if frame is None:
            self._closed = True
            return None
        if isinstance(frame, (bytes, bytearray)):
            frame = frame.decode("utf-8", errors="replace")
        return frame.rstrip("\r\n")

    def _split_input(self, frame: str) -> Optional[list[str]]:
        """⛔ ONE FRAME = ONE LINE. Split a frame into the lines it contains.

        The socket doors get this invariant for free — the newline IS their
        delimiter, so a line cannot contain one. The web door's delimiter is the
        FRAME, and until v1.51.0 it trusted our own JS client to send single
        lines: `_recv_frame` stripped only a TRAILING "\\r\\n", so a hostile
        non-browser client could put a newline in the middle.

        That mattered because `strip_controls` deliberately KEEPS "\\n" and
        `send()` splits output on it — so one frame could paint several physical
        lines on every screen in the room. Confirmed live before the fix:

            sent one frame: 'hello\\n[*** Wren enters the world ***]'
            the room saw:   Mallory says: hello
                            [*** Wren enters the world ***]

        A forged system line and a forged speaker. Line-spoofing rather than
        XSS, but forging who-said-what is an integrity hole in a talker, and
        `cmd_say` only `.strip()`s its body — so this was live, not theoretical.

        ⚠ **SPLIT, not reject**, and the reasoning is worth keeping:

        * It is what the SOCKET doors already do. Two lines arrive as two lines
          there, so splitting gives one rule over both doors instead of two —
          the same argument that made a `\\x01`-prefixed line dropped on both.
        * It never silently eats someone's text. A paste containing newlines is
          a plausible thing for a person to do, and truncation would lose the
          rest of it — the failure mode this codebase keeps choosing against.
        * It cannot amplify: every split line goes through the ordinary dispatch
          and spends a command-governor token exactly as if it had arrived in
          its own frame. Five lines cost five tokens either way.
        * ⚠ It costs OUR OWN CLIENT nothing either way, which is what makes the
          choice free: `collapse()` in web/index.html already flattens `\\s+` to
          a single space before sending, so the browser cannot produce a newline
          at all. Anything that does is, by construction, not our client.

        `str.splitlines()` rather than a `\\n` split: it takes `\\r`, `\\r\\n`
        and the exotic separators (`\\v`, `\\f`, `\\x85`, `\\u2028`) in one go,
        which is stricter than the output side needs and free.

        Returns None when the frame holds absurdly many lines — see
        MAX_FRAME_LINES; the caller closes the connection.
        """
        lines = frame.splitlines()
        if len(lines) > MAX_FRAME_LINES:
            self.rejected = ("too_many_lines", frame[:200])
            return None
        # ⛔ AN EMBEDDED \x01 IS HOSTILE (v1.52.0). The control convention is a
        # FRAME-level thing: a frame that begins with \x01 is out-of-band and is
        # handled as such by `recv_line` before we ever get here. A \x01 that
        # appears on the SECOND or later line of a frame is something else
        # entirely — an attempt to smuggle a control verb in behind ordinary
        # text, which nothing legitimate does.
        #
        # ⚠ The shipped client cannot produce one: `collapse()` flattens `\s+`
        # to a space before sending, so it cannot emit a multi-line frame at all,
        # let alone one with a control prefix inside it. Same reasoning as the
        # 16-line hang-up and the socket door's `_raw` cap — anything that does
        # this is by construction not our client.
        if any(ln.startswith(CONTROL_PREFIX) for ln in lines):
            self.rejected = ("control_byte", frame[:200])
            return None
        return [ln[:MAX_LINE_LENGTH] for ln in lines]

    async def recv_line(self) -> Optional[str]:
        """One line of USER INPUT, or None at EOF.

        Inbound control frames are consumed here and never surface. They used to
        come back as an empty string, which the session loop discarded — fine
        while nothing sent them, but a width report arriving during login would
        read as a blank line and re-print the name prompt. Consuming them inside
        the loop means an out-of-band frame is genuinely out of band: it can
        arrive at any moment, including mid-login, and change nothing but the
        value it carries.

        ⚠ A frame's lines land in `_pushback` and are handed out one at a time,
        so the queue that already existed for the hello race does the delivery
        here too — there is one place a line can come from, not two.
        """
        while True:
            if self._pushback:
                return self._pushback.pop(0)
            line = await self._recv_frame()
            if line is None:
                return None
            if line.startswith(CONTROL_PREFIX):
                self._note_control(line)
                continue
            parts = self._split_input(line)
            if parts is None:
                # Not a paste and not our client. The socket door answers the
                # same way when a peer produces nothing usable (the `_raw` cap,
                # v1.48.0): hang up rather than serve it.
                self.close()
                return None
            if not parts:
                return ""            # an empty frame is an empty line, as before
            self._pushback.extend(parts)

    async def wait_for_hello(self, timeout: float, max_frames: int = 4) -> None:
        """Briefly wait for the client's opening control frames.

        Called ONCE, before the login banner, so the very first thing a phone
        sees is already laid out for it — and so a remembered device is logged
        back in without a banner and a prompt flashing past first.

        The client sends ``\\x01width`` then ``\\x01resume`` (empty if it has
        none), and the resume frame is what ends the wait — so a current client
        costs one round trip, not the whole timeout. An older client that sends
        only a width report pays the timeout once, at connect, and is otherwise
        unaffected. ``max_frames`` is a backstop against a client that streams
        control frames to hold the login open.

        Bounded and best-effort throughout: on timeout we simply proceed with
        whatever arrived, which for a telnet user (who never gets here) and an
        older browser is exactly the old behaviour.

        A non-control frame read in the attempt is PUSHED BACK, not consumed —
        someone who types before the hello lands must not lose that line. The
        timeout path is safe from the other side for the same reason: the
        websockets library documents cancelling ``recv()`` as lossless (the next
        call gets the frame), so a timeout mid-read cannot swallow one either.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        for _ in range(max_frames):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            try:
                line = await asyncio.wait_for(self._recv_frame(), remaining)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                return
            if line is None:
                return
            if line.startswith(CONTROL_PREFIX):
                if self._note_control(line):
                    return                # hello complete; do not sit out the rest
                continue
            # ⚠ Through the SAME splitter as `recv_line` (v1.51.0). A frame
            # arriving during the hello is pushed back rather than consumed, and
            # if it were pushed back WHOLE the one-frame-one-line rule would have
            # a hole in it exactly the width of the login window.
            parts = self._split_input(line)
            if parts is None:
                self.close()
                return
            self._pushback.extend(parts)
            return

    # Kept under its old name for anything still calling it; the wait now covers
    # the whole opening hello, of which the width report is one frame.
    wait_for_width = wait_for_hello

    def _enqueue(self, frame: str) -> bool:
        """Queue one outbound frame. False if the peer has stopped reading.

        ⛔ The bound (`MAX_OUT_QUEUE`) is the point, and so is what happens when
        it is reached: the connection is CLOSED, not blocked on. Every caller
        here is a synchronous command handler pushing text at a room, so a
        blocking put would stall the whole house behind one browser that had
        stopped reading — trading a memory leak for a liveness bug.
        """
        if self._closed:
            return False
        try:
            self._out.put_nowait(frame)
            return True
        except asyncio.QueueFull:
            # A peer this far behind is gone. Drop what is queued and hang up;
            # the ordinary disconnect path does the rest.
            self.close()
            return False

    def send_text(self, text: str) -> None:
        if self._closed or text == "":
            return
        body = text[:-1] if text.endswith("\n") else text
        # One frame per line; blank lines are preserved as empty frames so the
        # terminal spacing matches the socket doors.
        for line in body.split("\n"):
            if not self._enqueue(line):
                return          # closed mid-unit; the rest has nowhere to go

    def prompt(self) -> None:
        pass  # no server-side prompt on the web door

    def set_echo(self, on: bool) -> None:
        if self._closed:
            return
        self._enqueue(CTRL_ECHO_ON if on else CTRL_ECHO_OFF)

    async def drain(self) -> None:
        # Let queued frames flush before the caller continues. If the connection
        # is gone, there is nothing to wait for.
        if self._closed:
            return
        try:
            await asyncio.wait_for(self._out.join(), timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass

    def close(self) -> None:
        # Stop the sender and unblock any waiter. We also proactively close the
        # underlying connection so a *pending* recv_line() (a blocked read on a
        # ghost being booted or displaced by a takeover) returns at once, instead
        # of waiting for the browser to send or the socket to die on its own.
        if self._sender and not self._sender.done():
            self._sender.cancel()
        self._mark_closed()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self._conn.close())

    def is_closing(self) -> bool:
        return self._closed
