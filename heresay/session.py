# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A single connection: line-in / line-out, attached to a resident.

The core never knows which door someone came through (TLS socket, plaintext
socket, or WebSocket) — a Session holds a transport and only ever speaks in whole
lines of text plus two out-of-band signals (a prompt, and an echo toggle used to
mask passwords). See ``transport.py``.

Ephemerality note: exception handlers here never include the content of a line
or the read buffer. If something goes wrong we log the fact, not the words.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .accounts import AccountStore
from .ratelimit import SpeechGovernor, ShoutLimiter, TokenBucket
from .state import HELP_STYLES, HOUSE_DEFAULTS, house_switch
from .theme import DEFAULT_THEME
from .world import World

# Re-exported for anything that still imports it from here.
from .transport import CTRL_RESUME, strip_telnet  # noqa: F401

# Minimal ANSI: dim for system/announcement lines, reset after. Nothing more.
ANSI_DIM = "\x1b[2m"
ANSI_RESET = "\x1b[0m"


# ---------------------------------------------------------------------------
# Session width — the portrait-phone switch
#
# ONE switch, two values: wide (the operator's `[world] wrap_columns`, 78 by
# default) and narrow (40). Not a continuum — a layout that has to be right at
# every width between 30 and 200 is a layout that is good at none of them, and
# the screens here are hand-set tables, not fluid boxes.
#
# NARROW IS WEB-ONLY, by construction rather than by an `if`: it needs the
# client to report its columns, and only a door with a control convention can.
# A telnet/plaintext session is therefore always wide — which is also right on
# its own terms, since that door is loopback and operator-facing.
#
# The width lives on the SESSION, never on the config: one Config object is
# shared by every connection, so narrowing `config.wrap_columns` would narrow
# the whole server.
# ---------------------------------------------------------------------------

NARROW_COLUMNS = 40

# A reported width BELOW this is narrow; at or above it is wide. A portrait
# phone reports roughly 40-46 usable monospace columns and a landscape one 70+,
# so 60 splits them with a wide margin on either side — deliberately nowhere
# near either cluster, so a slightly different font or zoom level cannot flip a
# screen back and forth across the boundary.
NARROW_THRESHOLD = 60

# How long the server will wait for the opening width report before painting the
# login banner. Long enough for a frame the client sends immediately on open,
# short enough that nobody perceives it; on timeout we simply go wide.
WIDTH_REPORT_TIMEOUT = 0.25


def resolve_width(reported, wide: int) -> int:
    """The width to render this session at: ``wide``, or ``NARROW_COLUMNS``.

    ``reported`` is whatever the client last said (or None). Re-evaluated on
    every report, so rotating a phone flips the layout live — the next wrapped
    line, rule, map or help screen simply comes out at the new width.
    """
    if reported is None:
        return wide
    return NARROW_COLUMNS if reported < NARROW_THRESHOLD else wide


def strip_controls(text: str) -> str:
    """Remove terminal control bytes (ESC/BEL/BS/CR/NUL and other C0/C1) from
    text bound for a screen, keeping printable characters, spaces, tabs, and
    newlines.

    Applied at the output sink so no user-authored line — spoken, emoted,
    thought, shouted, told, stored in a description/profile/mail, or replayed via
    .rev — can smuggle ANSI/cursor/bell sequences into another user's terminal.
    The server's own dim/colour ANSI is added by ``_decorate`` AFTER this runs,
    and the reboot player writes straight to the transport (``_SessionSink``), so
    both legitimate control-byte paths are unaffected. Multibyte UTF-8 text is
    printable and passes through intact.
    """
    return "".join(ch for ch in text if ch.isprintable() or ch in "\n\t")

EDITOR_HEADER = (
    'Composing. Type your text; ".end" on its own line saves, ".abort" discards.'
)
# A generous safety ceiling so a runaway paste can't grow the buffer without
# bound; the real per-kind cap is enforced on save.
_EDITOR_HARD_LIMIT = 500


@dataclass
class EditorState:
    """An in-session multi-line compose buffer. In memory only until save."""

    kind: str                 # "profile" | "mail" (for messaging only)
    max_lines: int
    on_save: Callable         # on_save(session, text) -> None
    buffer: list = field(default_factory=list)


class Session:
    def __init__(
        self,
        transport,
        world: World,
        accounts: AccountStore,
        config,
        clock=time.monotonic,
        mail=None,
        bans=None,
        state=None,
        wall_clock=time.time,
        reboot_steps=None,
        reboot_steps_narrow=None,
        theme=None,
        help_topics=None,
        revtells=None,
        events=None,
        ban_history=None,
        resume=None,
        load_report=None,
        agreement_text="",
        favicon_url="",
    ):
        self.transport = transport
        self.world = world
        self.accounts = accounts
        self.config = config
        self.mail = mail
        self.revtells = revtells
        # The operational event log, or None in unit sessions. Facts only —
        # who did what to whom — and never a word anyone said.
        self.events = events
        # Every ban ever placed (or None in unit sessions). The one store that
        # holds an address — matched internally, never printed.
        self.ban_history = ban_history
        # The resume-token store, or None in unit sessions. Auth data only.
        self.resume = resume
        # What the engine saw when it read the house, held in memory since the
        # last boot (or None in unit sessions). Authoring facts — filenames,
        # room ids, counts — and nothing about any person.
        self.load_report = load_report
        # The RESOLVED agreement text (placeholders substituted, unwrapped) —
        # the exact string the version hash was taken of. `.agreement` lays it
        # out through the same renderer the signup screen uses; see
        # commands.render_agreement.
        self.agreement_text = agreement_text
        self.bans = bans
        self.state = state
        self.theme = theme or DEFAULT_THEME
        # {topic: page text} from help/, loaded once by the server.
        self.help_topics = help_topics or {}
        # The house favicon as a data: URL (or ""), read once by the server and
        # pushed to a web client on connect. Held here so the push has one source.
        self.favicon_url = favicon_url or ""
        self.peer_ip = transport.peer_ip
        # True only for the local plaintext console listener; gates arch-name
        # registration. Never derived from an IP or a forwarded header.
        self.is_local_console = getattr(transport, "is_local_console", False)
        self._clock = clock
        self._wall = wall_clock

        self.resident = None
        # Recomputed from the transport's latest report on every read (see the
        # `width` property). Wide unless a client has told us it is small.
        self._wide = config.wrap_columns
        self._closing = False
        self._disconnect_reason: Optional[str] = None
        self.editor: Optional[EditorState] = None
        # The account a pending arch command is about (.deleteuser /
        # .resetpassword): the handler validates and names it, the session loop
        # does the prompting. Cleared as soon as the loop picks it up.
        self.pending_target: Optional[str] = None
        # The pending `.ban_ip` — (account, ip, hours) — set by the handler and
        # picked up by the session loop, which owns the collateral confirm.
        # Same shape as `pending_target`, and cleared the moment it is read.
        self.pending_ban_ip = None
        # The pre-auth phase's deadline, as an `asyncio.Timeout` the server hangs
        # here for the duration of `_login` (and clears after). It lives on the
        # session because the one step that needs to suspend it — reading the
        # agreement — is reached four calls deep, down paths that already thread
        # quite enough. None outside the login phase, and in unit sessions.
        self.auth_timeout = None

        # .reboot boot theater: parsed steps (or None if no reboot.txt), a private
        # rate bucket (1 per config.reboot_rate_seconds), and a flag that, while a
        # sequence plays, suppresses every OTHER line bound for this session — you
        # were rebooting, you had to be there. The player writes to the transport
        # directly, so its own frames sail past this gate.
        self.reboot_steps = reboot_steps
        # The same script parsed at 40 columns, or None. The parse pre-wraps
        # every line, so there is no way to re-flow one list into the other.
        self.reboot_steps_narrow = reboot_steps_narrow
        self._reboot_active = False
        self.reboot_bucket = TokenBucket(1, 1.0 / max(config.reboot_rate_seconds, 1e-6))

        # .call_staff (call for staff): own stern bucket, 1 per staff_call_seconds.
        self.staff_bucket = TokenBucket(
            1, 1.0 / max(config.staff_call_seconds, 1e-6)
        )

        # Resident preferences are NOT snapshotted here — see the three
        # properties below. Before login there is no resident, so they resolve to
        # the house value, which is what an unstyled name prompt wants anyway.

        self.speech = SpeechGovernor(
            burst=config.speech_burst,
            refill_per_sec=config.speech_refill_per_sec,
            mute_strikes=config.speech_mute_strikes,
            disconnect_strikes=config.speech_disconnect_strikes,
            mute_seconds=config.speech_mute_seconds,
        )
        self.shout_limiter = ShoutLimiter(config.shout_per_seconds)
        self.mail_bucket = TokenBucket(
            config.mail_send_burst, config.mail_send_refill_per_sec
        )
        self.invite_bucket = TokenBucket(
            config.invite_burst, config.invite_refill_per_sec
        )
        # A generous per-session governor over ALL dotted commands, so no
        # logged-in client can spam handler/disk work (.set/.desc/.profile/.mute/
        # .who/…). Speech has its own SpeechGovernor; this covers the rest.
        self.command_bucket = TokenBucket(
            config.command_burst, config.command_refill_per_sec
        )

    # ---- multi-line editor -------------------------------------------

    def has_reboot_script(self) -> bool:
        """True when a non-empty reboot.txt was loaded (else .reboot no-ops)."""
        return bool(self.reboot_steps)

    def reboot_script(self):
        """The boot steps at this session's width, falling back to the wide parse."""
        if self.is_narrow and self.reboot_steps_narrow:
            return self.reboot_steps_narrow
        return self.reboot_steps

    def in_editor(self) -> bool:
        return self.editor is not None

    def start_editor(self, kind: str, max_lines: int, on_save: Callable) -> None:
        self.editor = EditorState(kind=kind, max_lines=max_lines, on_save=on_save)
        self.send(EDITOR_HEADER)

    def feed_editor(self, line: str) -> None:
        """Route one input line to the active editor. Never interprets commands
        as speech while composing."""
        ed = self.editor
        if ed is None:
            return
        stripped = line.strip()
        if stripped == ".end":
            n = len(ed.buffer)
            if n > ed.max_lines:
                # Over cap: tell the writer, stay in the editor (nothing saved).
                self.send(
                    f"That's {n} lines; the limit is {ed.max_lines}. "
                    f"Trim it, or .abort to discard — still composing."
                )
                return
            text = "\n".join(ed.buffer)
            self.editor = None
            ed.on_save(self, text)
        elif stripped == ".abort":
            self.editor = None
            self.send("Discarded.")
        else:
            if len(ed.buffer) >= _EDITOR_HARD_LIMIT:
                self.send("That's very long — type .end or .abort.")
                return
            ed.buffer.append(line)

    # ---- clock hooks (kept simple; injected for tests) ---------------

    def now(self) -> float:
        return self._clock()

    def now_wall(self) -> float:
        return self._wall()

    def speech_check(self) -> str:
        return self.speech.check(self.now())

    def request_disconnect(self, reason: str) -> None:
        self._disconnect_reason = reason
        self._closing = True

    def kick(self, reason: str) -> None:
        """Forcibly disconnect this session (staff .boot / .ban).

        Marks an intentional departure and closes the transport so a blocked
        read returns at once. Best-effort delivery of the reason line first.
        """
        self._disconnect_reason = "kicked"
        self._closing = True
        self._close_with_line(f"You have been disconnected. {reason}".rstrip())

    def displace(self, message: str) -> None:
        """Disconnect this session because the same account logged in elsewhere
        (self-takeover). Like kick(), but the peer gets a plain one-line notice,
        not a moderation-boot line. Closing the transport unblocks the ghost's
        pending read so its loop dies cleanly — no leaked task.
        """
        self._disconnect_reason = "displaced"
        self._closing = True
        self._close_with_line(message)

    def _close_with_line(self, line: str) -> None:
        """Best-effort: deliver one final line, then close the transport. When no
        loop is running (unit tests) there's nothing to close — just record intent."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            loop.create_task(self._flush_and_close(line))
        else:  # no running loop (unit tests): best effort, no transport close
            self.send(line)

    async def _flush_and_close(self, line: str) -> None:
        try:
            self.send(line)
            await self.drain()
        finally:
            self.transport.close()

    # ---- the display switches (dim / time / helpstyle) ----------------
    #
    # Resolved at USE-TIME, never snapshotted — which is what makes a
    # `.ownerset` change reach a connected resident with no relogin
    # (v1.25.0). Precedence, unchanged: an explicit `.set` value on the resident
    # wins; otherwise the live house value; otherwise the built-in.
    #
    # This is deliberately the SAME model as the message defaults
    # (`resident_message` / `res.messages`), not a second mechanism: hold only
    # what the person chose, and read the house at the moment of rendering. The
    # three switches were the last place in the program doing the opposite.
    #
    # Each has a SETTER, and assigning one records an explicit override — which
    # is the honest reading of `session.dim_system = True` and exactly what
    # `.set dim on` means. `.set <key> default` deletes the key instead, putting
    # the resident back on the house.

    def _switch(self, key: str):
        """The resolved value of one display switch, own-then-house."""
        res = self.resident
        if res is not None:
            own = (getattr(res, "switches", None) or {}).get(key)
            if own is not None:
                return own
        return house_switch(self.state, key)

    def _set_switch(self, key: str, value) -> None:
        res = self.resident
        if res is not None:
            if getattr(res, "switches", None) is None:
                res.switches = {}
            res.switches[key] = value

    @property
    def dim_system(self) -> bool:
        return bool(self._switch("dim"))

    @dim_system.setter
    def dim_system(self, value) -> None:
        self._set_switch("dim", bool(value))

    @property
    def show_time(self) -> bool:
        return bool(self._switch("time"))

    @show_time.setter
    def show_time(self, value) -> None:
        self._set_switch("time", bool(value))

    @property
    def help_style(self) -> str:
        value = self._switch("helpstyle")
        return value if value in HELP_STYLES else HOUSE_DEFAULTS["helpstyle"]

    @help_style.setter
    def help_style(self, value) -> None:
        self._set_switch("helpstyle", value)

    def follows_house(self, key: str) -> bool:
        """True when this resident has NOT chosen their own value for ``key`` —
        i.e. they move when the house's own value moves. What bare `.set` marks."""
        res = self.resident
        return not (res is not None and (getattr(res, "switches", None) or {}).get(key) is not None)

    # ---- width -------------------------------------------------------

    @property
    def width(self) -> int:
        """The column count this session renders at — 78 (or the operator's
        `wrap_columns`) normally, 40 on a portrait phone.

        A property, not a stored value, so it is *always* current: the transport
        replaces its reported width the moment a resize frame lands, and the
        very next thing rendered picks it up. Nothing has to be told to refresh,
        and there is no stale copy anywhere to forget.
        """
        return resolve_width(getattr(self.transport, "reported_width", None),
                             self._wide)

    @property
    def is_narrow(self) -> bool:
        return self.width <= NARROW_COLUMNS

    async def await_client_hello(self) -> None:
        """Give a web client a moment to send its opening control frames.

        Two things ride on this. The greeting is the first thing written to a new
        connection, so without it a phone would see one 78-column banner and only
        then switch; and a remembered device must be logged in BEFORE the banner
        and the name prompt, or the silent part of a silent login is a screenful
        of prompts flashing past.

        A no-op on any door that cannot send control frames — it has nothing to
        wait for, which is every door but the browser.
        """
        waiter = getattr(self.transport, "wait_for_hello", None)
        if waiter is None:
            waiter = getattr(self.transport, "wait_for_width", None)
        if waiter is not None:
            await waiter(WIDTH_REPORT_TIMEOUT)

    # The old name, kept so nothing that calls it breaks; the wait now covers the
    # whole opening hello rather than the width report alone.
    await_width_report = await_client_hello

    def send_control_resume(self, token: str) -> None:
        """Push a resume token to the browser — or "" to tell it to forget one.

        Web-only like every control frame, so this is a silent no-op on a socket
        door; a resident who `.quit`s from telnet still revokes their tokens
        server-side, there is simply no browser here to tell.
        """
        self.send_control(CTRL_RESUME + (f" {token}" if token else ""))

    def take_pending_resume(self):
        """The resume token this browser presented, consumed exactly ONCE.

        Consumed rather than read: a token is a single-use credential here (it
        rotates on every successful use), so leaving it lying on the transport
        for a second reader to find would be a way to spend it twice.
        """
        token = getattr(self.transport, "pending_resume", None)
        if token is not None:
            try:
                self.transport.pending_resume = None
            except AttributeError:
                pass                     # a class-level None on the socket doors
        return token

    # ---- output ------------------------------------------------------
    #
    # ONE CALL = ONE OUTPUT UNIT. A unit is one logical event — a say (however
    # many physical lines it wraps to), an emote, a tell, an arrival, the whole
    # `.who` block, the whole `.help` screen, one error line. Every unit is
    # preceded by exactly one blank line, so a screen reads as separated events
    # instead of a wall of text. Handlers don't space anything: they hand over
    # their text, and the session does the spacing (see `_emit_unit`).
    #
    # The `.reboot` player is exempt by construction, not by a flag check here:
    # it writes already-formed frames through `_SessionSink` straight to the
    # transport, so its `@clear` blanks and back-to-back `@progress` stage lines
    # play exactly as authored.

    def _emit_unit(self, out_lines: list[str]) -> None:
        """Write one output unit: a single blank separator line, then its lines.

        The unit's own leading and trailing blank lines are dropped first, so
        the separator can never produce two blank lines in a row. Blank lines
        INSIDE a unit (the gap under a room's title, the wholist's rows) are
        left exactly as the handler wrote them. An all-blank unit is no event at
        all, and is dropped entirely rather than emitted as a stray blank.
        """
        while out_lines and not out_lines[0].strip():
            out_lines.pop(0)
        while out_lines and not out_lines[-1].strip():
            out_lines.pop()
        if not out_lines:
            return
        self.transport.send_text("\n".join([""] + out_lines) + "\n")

    def send(self, text: str, *, dim: bool = True) -> None:
        """Send one unit of text, wrapping each logical line at the configured width.

        ``dim`` marks system/announcement text (vs. speech). With ``.set dim``
        on, system lines are dimmed on socket doors (the web ignores ANSI). With
        ``.set time`` on, each line is prefixed with ``HH:MM``.
        """
        if self._reboot_active:
            return  # a reboot is playing: drop everything else bound for this screen
        text = strip_controls(text)   # no injected ANSI/BEL reaches another screen
        width = self.width
        out_lines = []
        for logical in text.split("\n"):
            if not logical:
                out_lines.append("")
                continue
            wrapped = textwrap.wrap(
                logical, width=width, break_long_words=False, break_on_hyphens=False
            )
            out_lines.extend(wrapped or [""])
        # Decorate first, then unit-emit: the separator is a true blank line,
        # never carrying a time prefix of its own.
        #
        # The two switches are resolved ONCE here, per unit, and handed down —
        # not read inside _decorate, which runs per PHYSICAL LINE. Live to the
        # granularity of a message, which is the only granularity anyone can
        # observe, without doing a dict lookup and a string normalisation for
        # every wrapped line of every message to every recipient.
        dim_on = self.dim_system
        time_on = self.show_time
        self._emit_unit([self._decorate(ln, dim, dim_on, time_on)
                         for ln in out_lines])

    def _decorate(self, line: str, dim: bool, dim_on: bool = None,
                  time_on: bool = None) -> str:
        """Apply per-line time prefix and (socket-only) ANSI dimming.

        ``dim_on``/``time_on`` are the switch values resolved once by the caller;
        left out, they are resolved here (the path a direct caller takes).
        """
        if dim_on is None:
            dim_on = self.dim_system
        if time_on is None:
            time_on = self.show_time
        if dim_on and dim and line and getattr(self.transport, "supports_ansi", False):
            line = f"{ANSI_DIM}{line}{ANSI_RESET}"
        if time_on:
            stamp = time.strftime("%H:%M", time.localtime(self._wall()))
            line = f"{stamp} {line}"
        return line

    def send_raw(self, text: str) -> None:
        """Send one unit verbatim — no wrapping (used for the map, help, .who)."""
        if self._reboot_active:
            return  # suppressed during a reboot, like send()
        self._emit_unit(strip_controls(text).split("\n"))

    def prompt(self) -> None:
        # A bare, quiet prompt on the socket doors; a no-op on the web door,
        # which draws its own input line.
        self.transport.prompt()

    async def drain(self) -> None:
        await self.transport.drain()

    # ---- input -------------------------------------------------------

    async def read_line(self, max_len: int = 1024) -> Optional[str]:
        return await self.transport.recv_line()

    def send_control(self, payload: str) -> None:
        """Send one ``\\x01`` control frame — WEB DOOR ONLY, silently.

        A socket terminal would PRINT the frame, so this is gated on the
        transport's own capability rather than on an isinstance check at the call
        site: a door that has no control convention simply never receives one, and
        a handler can call this unconditionally. Bypasses ``send()`` entirely —
        a control frame is not output, gets no blank-line separation, no wrapping
        and no ANSI.

        ⛔ **EXACTLY ONE FRAME** (v1.52.3, audit W5). `WebSocketTransport.send_text`
        splits its argument on ``\\n`` and enqueues one frame per line, so a
        payload carrying a newline became a control frame **plus a line the
        browser printed into the terminal**. Verified before the fix:
        ``send_control("\\x01title Wonder\\nland")`` produced
        ``['\\x01title Wonder', 'land']``. This is the output-side counterpart
        of the one-frame-one-line rule v1.51.0 put on input, and it belongs at
        this choke point so the guarantee holds for every caller and every door
        rather than for the payloads somebody remembered to check.

        ⚠ STRIPPED, not refused. Every payload here is server- or
        config-generated — the instance name, a favicon ``data:`` URL, the raise
        bounds, an echo toggle — never user speech, so there is nothing to
        preserve, and a silently-dropped frame (the browser never learning its
        title) would be worse than a tidied one. The only source that could
        carry a break is ``theme.instance_name``, which is a bare ``str()`` of
        an operator's TOML.

        ⚠ ``splitlines()`` rather than a ``\\n``/``\\r`` replace, matching
        ``_split_input`` on the way in: it takes ``\\r``, ``\\r\\n`` and the
        exotic separators (``\\v``, ``\\f``, ``\\x85``, ``\\u2028``) in one go,
        which is stricter than the split needs and free.
        """
        if not getattr(self.transport, "supports_control_frames", False):
            return
        self.transport.send_text("".join(payload.splitlines()) + "\n")

    # ---- prompts for the login flow ----------------------------------

    async def ask(self, prompt: str, *, secret: bool = False) -> Optional[str]:
        """Write a prompt and read one line.

        When ``secret`` is set, the input is masked: echo off around the read on
        every door (telnet ECHO negotiation on sockets, an ``\\x01echo`` control
        frame on the web).
        """
        if secret:
            self.transport.set_echo(False)
        self.transport.send_text(prompt)
        await self.drain()
        line = await self.read_line()
        if secret:
            self.transport.set_echo(True)
            await self.drain()
        if line is None:
            return None
        # Secret input (passwords, recovery codes) is returned verbatim — never
        # trimmed — so a password may legitimately contain leading/trailing
        # spaces. Ordinary prompts (names, y/n) are still stripped.
        return line if secret else line.strip()
