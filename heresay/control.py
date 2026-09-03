# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The local control channel — say something to everyone, from outside (v1.46.0).

**The problem, and why it needs a channel at all.** Only the running engine holds
the connections, so a warning before a restart has to be broadcast *by* it. But
the thing that knows a restart is coming is `deploy.sh`, or an operator at a
prompt — outside the process. So there has to be a way in, and the whole design
of this module is what kind of way in it is.

⛔ **A UNIX-DOMAIN SOCKET IN THE STATE DIR, mode 0600, and NEVER a port.** The
trust posture is `--write-permissions`': something the operator does on the box,
by being on the box. A TCP listener — even on loopback — is reachable by every
process on the machine and is one config mistake away from being reachable by
the internet; a socket file is gated by the filesystem, which is a permission
system the operator already understands and systemd already confines. It also
fits the loop cleanly (`asyncio.start_unix_server` is the same shape as the
plaintext door) and costs no new dependency.

⚠ **The unit MUST list `AF_UNIX` in `RestrictAddressFamilies`, and it does** —
`RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` in
deploy/install/heresay.service, added in v1.46.0 with this feature. Without it
the `bind()` fails at startup and the daemon still runs, so the countdown would
silently not work; that is why the unit test pins the directive rather than
trusting it. (This note used to say the unit listed only `AF_INET AF_INET6` —
true when the note was written, stale from the moment the unit was updated.)

⚠ **What that widens, recorded rather than assumed** (v1.46 audit F9, no change):
`RestrictAddressFamilies` cannot tell a filesystem-backed unix socket from an
ABSTRACT-namespace one, which is not filesystem-gated. Allowing `AF_UNIX` is
unavoidable for this feature, and what a compromised daemon could reach through
it is already bounded by `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp` and
`NoNewPrivileges`. The trade is accepted deliberately, not overlooked.

**The wire is one JSON object per connection, and one back.** Not a protocol
worth designing: a line in, a line out, close. `{"cmd": "announce", "text": …}`.

**Three commands, one primitive.** `announce` is the primitive — broadcast one
line to every connected session, through the same delivery path as the presence
lines. `restart` and `shutdown` are nothing but SCHEDULES of announce calls with
a different thing at the end, which is why they share `run_countdown`. ⚡ Phase E's
"broadcast a message" button is `announce`, unchanged: the seam is this socket,
and an admin screen calls it exactly as `deploy.sh` does.

⚠ **The two front-ends differ ONLY at T=0**, and the difference is real:

* **restart** (the deploy) — the engine keeps running. `deploy.sh` blocks on the
  countdown and then runs `systemctl restart` itself, so the thing that brings
  the house back is the thing that has the new code.
* **shutdown** — the engine exits **0**, and the unit's `Restart=on-failure`
  (changed from `always` in v1.46.0) does not revive a clean exit. Staying down
  is the whole point of the command; under `Restart=always` it could not.

**Covenant.** These are system notices, not speech: fine to show, and **the body
is never written down**. What IS recorded is the FACT of a restart or a shutdown
— a `staff_action` with `actor="config"`, the same shape the live-reload uses,
carrying the countdown's LENGTH and not a syllable of its text. ⚠ A bare
`announce` records nothing: the only thing it has is its words, and a row saying
"something was announced" with the words removed is not accountability, it is
noise. If Phase E wants an audit trail for the button it can add one with a
policy behind it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from .commands._shared import clean_message

log = logging.getLogger("heresay")

# One line in, one line out. A body bigger than this is not a control command;
# it is somebody's mistake, and reading it into memory unbounded is how a local
# socket becomes a local denial of service.
MAX_REQUEST = 8192

# The countdown's last word, always. A schedule is minutes down to one minute
# and then this — close enough to the end to be a real warning, far enough that
# a sentence still lands.
FINAL_SECONDS = 10

# ⛔ THE CEILING ON `--shutdown` (v1.47.0, audit F2). One week — far more notice
# than anyone will ever give, and still a number the machine can hold.
#
# Two harms without it, and the second is the one that bites. The schedule is a
# list of every minute, built SYNCHRONOUSLY in the control handler, so a large N
# stalls the WHOLE house: measured at 2.4 s for 20,000,000, and ~5 GB with a dead
# request at 100,000,000. And the reply used to echo that list, overrunning the
# CLI's own read limit — so `--shutdown 200000`, one slipped keystroke from
# `--shutdown 20`, showed the operator a traceback while the house really was
# scheduled to go down.
MAX_SHUTDOWN_MINUTES = 7 * 24 * 60      # 10,080

# How long an `announce` may be. ⚠ NOT `_shared.MSG_MAX` (60), which is the cap
# on a resident's own one-line presence phrase: the shipped restart notice is
# already 62 characters, so borrowing that number would truncate the engine's
# own sentences and make the feature useless for the thing it is for. 200 is a
# notice — a sentence or two — and still a bound.
#
# ⛔ The CAP is the lesser half of this. What matters is `clean_message`'s
# COLLAPSE: it is what stops a multi-line announce shaping itself like a
# presence line or attributed speech (audit F3).
ANNOUNCE_MAX = 200


class ControlError(ValueError):
    """A request the engine will not act on. The reason goes back to the CLI."""


# ---------------------------------------------------------------------------
# Schedules — pure logic, so they are testable without sleeping through them
# ---------------------------------------------------------------------------

# ⛔ The longest a deploy countdown may be (v1.52.3, audit W3).
#
# `deploy.sh` BLOCKS on `--restart-countdown` synchronously — that is the whole
# point of it, since the restart must not land before the last warning — so an
# unbounded mark does not merely make a long countdown, it ties up a deploy for
# as long as it says. `checkpoints: [10**9]` armed a thirty-one-year one.
#
# An hour is a deploy notice with room to spare (the shipped marks are 120s) and
# is still a bound. Tunable, exactly like `MAX_SHUTDOWN_MINUTES`.
MAX_RESTART_SECONDS = 3600


def restart_schedule(checkpoints) -> list[int]:
    """The deploy countdown: the configured marks, largest first.

    Sorted, de-duplicated and stripped of anything ≤ 0 rather than trusted, so a
    mis-typed `[10, 60, 120]` (or a stray `0`) still counts DOWN. The first mark
    is the total wait, which is what makes a mis-ordered list otherwise silently
    wrong: it would announce "10 seconds" and then wait two minutes.

    ⛔ **A malformed list raises `ControlError`, not a bare ValueError/TypeError**
    (v1.52.3, audit W3). It used to coerce with a naked `int()`, so
    `{"cmd":"restart","checkpoints":["x"]}` fell straight past `_handle`'s
    ControlError branch into the blanket one — which `log.exception`s a full
    traceback for what is an ordinary bad request. That is the same shape audit
    F7 fixed for `readline`; `shutdown_schedule` already answered this way, and
    now both do.

    ⛔ **And it is CAPPED** — see `MAX_RESTART_SECONDS`. The over-cap message
    deliberately mirrors `shutdown_schedule`'s: from the operator's side "that
    is not a number I will act on" is one fact, and splitting it into two
    vocabularies would make a typo read as a new feature.
    """
    try:
        marks = sorted({int(c) for c in checkpoints if int(c) > 0}, reverse=True)
    except (TypeError, ValueError):
        raise ControlError(
            f"checkpoints must be whole seconds, not {checkpoints!r}") from None
    if marks and marks[0] > MAX_RESTART_SECONDS:
        raise ControlError(
            f"a restart checkpoint must be no greater than "
            f"{MAX_RESTART_SECONDS} seconds (1 hour), not {marks[0]!r}")
    return marks


def shutdown_schedule(minutes) -> list[int]:
    """Every whole minute remaining, then ten seconds.

    `5` → `[300, 240, 180, 120, 60, 10]`; `1` → `[60, 10]`.

    ⚠ **Under a minute is the ten-second warning alone**, not an error. Someone
    typing `--shutdown 0` at a terminal means "now, but let them see it coming",
    and refusing would be pedantry that costs the connected residents their
    warning. A value that is not a number at all IS an error — that is a typo,
    and guessing at it would be worse than saying so.

    ⚠ **A non-integral value is refused, not truncated** (v1.47.0). `int(2.7)`
    was 2, so `--shutdown 2.7` quietly gave two minutes' notice rather than
    saying it had not understood. An integer-VALUED float (`2.0`) passes: it is
    the same number, and JSON has no integer type to tell the two apart.

    ⛔ **Both refusals happen BEFORE the list is built**, which is the whole
    point of the cap — validating afterwards would already have paid the
    allocation the cap exists to prevent.
    """
    try:
        n = int(minutes)
        if isinstance(minutes, float) and not float(minutes).is_integer():
            raise ValueError("not a whole number")
        if isinstance(minutes, str) and minutes.strip() != str(n):
            raise ValueError("not a whole number")
    except (TypeError, ValueError):
        raise ControlError(f"minutes must be a whole number, not {minutes!r}")
    if n > MAX_SHUTDOWN_MINUTES:
        # ⚠ The SAME shape of message a non-number gets: from the operator's
        # side both are "that is not a number I will act on", and splitting them
        # into two vocabularies would make the typo case read as a new feature.
        raise ControlError(
            f"minutes must be a whole number no greater than "
            f"{MAX_SHUTDOWN_MINUTES} (7 days), not {minutes!r}")
    if n < 1:
        return [FINAL_SECONDS]
    return [m * 60 for m in range(n, 0, -1)] + [FINAL_SECONDS]


def human_time(seconds: int) -> str:
    """`120` → "2 minutes", `60` → "1 minute", `10` → "10 seconds".

    Whole minutes only above a minute, because every schedule this renders is
    built from whole minutes; anything else would be a number nobody asked for.
    """
    s = int(seconds)
    if s >= 60 and s % 60 == 0:
        m = s // 60
        return f"{m} minute" if m == 1 else f"{m} minutes"
    return f"{s} second" if s == 1 else f"{s} seconds"


def schedule_reply(marks) -> dict:
    """What a countdown says back: **how many warnings, and the first one.**

    ⛔ NOT the schedule itself (v1.47.0, audit F2). The full list is what an
    operator never needs and the wire cannot always carry: at 100,000 minutes it
    is megabytes on one line, past the client's own read limit — so the shipped
    CLI died on its own SUCCESS reply while the shutdown was armed. Two numbers
    say everything a person wanted from it.
    """
    return {"ok": True, "marks": len(marks),
            "first": human_time(marks[0]) if marks else ""}


def render(template: str, instance: str, seconds: int = None) -> str:
    """Fill `{instance}` and `{t}`.

    ⚠ An unknown placeholder leaves the template as written rather than raising:
    this is operator config, and a stray brace must not be able to silence the
    one warning residents get. The same posture as the agreement's placeholders.
    """
    try:
        return template.format(instance=instance,
                               t=human_time(seconds) if seconds is not None else "")
    except (KeyError, IndexError, ValueError):
        log.warning("control: message template has an unknown placeholder; "
                    "sending it as written")
        return template


# ---------------------------------------------------------------------------
# The countdown
# ---------------------------------------------------------------------------

async def run_countdown(server, schedule, template, sleeper=None) -> None:
    """Announce at each mark, sleeping the gap between them.

    `[120, 60, 10]` says "2 minutes", waits 60, says "1 minute", waits 50, says
    "10 seconds", waits 10, and returns. The caller decides what happens then —
    which is the only difference between a deploy and a shutdown.

    `sleeper` is injected so the tests can run a thirty-minute schedule in no
    time at all and still assert the gaps.
    """
    sleep = sleeper or asyncio.sleep
    marks = list(schedule)
    for i, mark in enumerate(marks):
        server.announce(render(template, server.theme.instance_name, mark))
        nxt = marks[i + 1] if i + 1 < len(marks) else 0
        gap = mark - nxt
        if gap > 0:
            await sleep(gap)


# ---------------------------------------------------------------------------
# The socket
# ---------------------------------------------------------------------------

class ControlServer:
    """The listener, and the dispatch of the three commands.

    Held by the `TalkerServer`, started beside the doors, closed with them. It
    owns the pending-shutdown task so `cancel` has something to cancel.
    """

    def __init__(self, server, path, sleeper=None):
        self.server = server
        self.path = Path(path)
        self._listener = None
        self._pending = None          # the fire-and-forget shutdown task
        # ⚠ Injected, not patched. A test that reached in and replaced
        # `asyncio.sleep` would be replacing it for the whole PROCESS — the
        # event loop included — which is how a thirty-minute schedule becomes a
        # hung suite. One attribute costs nothing and stays local.
        self.sleeper = sleeper

    # ---- lifecycle -------------------------------------------------

    async def start(self) -> None:
        """Bind the socket. A stale file from an unclean exit is removed first —
        a socket path is not state worth keeping, and refusing to start over one
        would turn a crash into an outage."""
        try:
            if self.path.exists():
                self.path.unlink()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._listener = await asyncio.start_unix_server(
                self._handle, path=str(self.path))
            # ⛔ 0600 and not one bit more: the filesystem IS the access control
            # here, and the socket is the one way to make the house speak.
            os.chmod(self.path, 0o600)
            log.info("control socket on %s", self.path)
        except OSError as exc:
            # Never fatal. A house that will not open because a control socket
            # could not be bound has traded a working server for a convenience.
            log.warning("control socket NOT started (%s): %s — the graceful "
                        "countdown and --shutdown are unavailable",
                        self.path, exc)
            self._listener = None

    async def aclose(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
        if self._listener is not None:
            self._listener.close()
            try:
                await asyncio.wait_for(self._listener.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError:
            pass

    # ---- the wire --------------------------------------------------

    async def _handle(self, reader, writer) -> None:
        try:
            # ⛔ NO TRACEBACK FOR A MALFORMED REQUEST (v1.47.0, audit F7).
            # `readline()` used to hit its OWN 64 KB limit and raise ValueError,
            # which fell through to the blanket handler and put a full traceback
            # in the process log for what is an ordinary bad request.
            # `readuntil` raises LimitOverrunError instead, caught below with
            # the other bad-request cases, so an oversized line is simply
            # answered.
            #
            # ⚠ TWO bounds, not one, and the comment used to claim otherwise.
            # This call passes NO explicit limit, so the StreamReader's default
            # (64 KiB) is the outer guard that raises; the `len(raw) >
            # MAX_REQUEST` check just below is the tighter one actually enforced
            # (8192). Left as it is deliberately: this is a 0600 socket only a
            # local operator can open, so 64 KiB versus 8 KiB is immaterial —
            # what mattered was that neither path is a traceback.
            raw = await asyncio.wait_for(
                reader.readuntil(b"\n"), timeout=5.0)
            if len(raw) > MAX_REQUEST:
                raise ControlError("request too large")
            req = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(req, dict):
                raise ControlError("expected a JSON object")
            reply = await self.dispatch(req)
        except ControlError as exc:
            reply = {"ok": False, "error": str(exc)}
        except (asyncio.LimitOverrunError, asyncio.IncompleteReadError):
            # Over MAX_REQUEST with no newline in sight, or a peer that hung up
            # mid-line. Both are the same fact from here: not a request.
            reply = {"ok": False, "error": "request too large"}
        except (asyncio.TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            reply = {"ok": False, "error": f"bad request: {exc}"}
        except Exception as exc:                       # never take the house down
            log.exception("control: request failed")
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            writer.write((json.dumps(reply) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        try:
            writer.close()
        except Exception:
            pass

    async def dispatch(self, req: dict) -> dict:
        """One request → one reply. Split out so tests drive it directly."""
        cmd = str(req.get("cmd", ""))
        if cmd == "announce":
            raw = req.get("text", "")
            # ⚠ A STRING, not whatever stringifies (v1.47.0, audit F4). `str()`
            # happily turned `{"a": 1}` into a broadcast reading `{'a': 1}`;
            # `shutdown` already refuses a non-number, and this is the same rule.
            if not isinstance(raw, str):
                raise ControlError(
                    f"announce text must be a string, not "
                    f"{type(raw).__name__}")
            # ⛔ ONE LINE (v1.47.0, audit F3). `strip_controls` at the sink kills
            # ANSI, BEL and the rest — but it keeps `\n` on purpose, so a
            # multi-line announce could put lines on every screen shaped exactly
            # like a presence line or attributed speech. `clean_message` is what
            # every other operator-supplied one-liner already goes through
            # (`.topic`, the five `.set *_msg` keys, `.ownerset`): it
            # collapses every run of whitespace to a single space and caps the
            # length, which closes the amplification path in the same call.
            # ⚠ At ANNOUNCE_MAX rather than the message keys' 60 — see there.
            text = clean_message(raw, ANNOUNCE_MAX)
            if not text:
                raise ControlError("announce needs text")
            n = self.server.announce(text)
            return {"ok": True, "sent": n}

        if cmd == "restart":
            # ⚠ SYNCHRONOUS on purpose: `deploy.sh` blocks here, and the whole
            # reason it does is that the restart must not land before the last
            # warning. The engine does NOT exit — deploy.sh runs the restart.
            marks = restart_schedule(
                req.get("checkpoints") or self.server.config.restart_checkpoints)
            if not marks:
                return schedule_reply(marks)
            self._record("restart", marks)
            await run_countdown(self.server, marks,
                                self.server.config.restart_notice,
                                sleeper=self.sleeper)
            return schedule_reply(marks)

        if cmd == "shutdown":
            if str(req.get("minutes", "")).strip().lower() == "cancel":
                return self._cancel()
            marks = shutdown_schedule(req.get("minutes"))
            if self._pending is not None and not self._pending.done():
                raise ControlError("a shutdown is already scheduled "
                                   "(--shutdown cancel to call it off)")
            self._record("shutdown", marks)
            # ⚡ FIRE AND FORGET: the ENGINE owns the clock, so the operator can
            # run `--shutdown 30` and close the terminal. A CLI that blocked for
            # half an hour would tie the countdown to an SSH session, which is
            # exactly the thing that gets dropped.
            self._pending = asyncio.create_task(self._shutdown(marks))
            return schedule_reply(marks)

        raise ControlError(f"unknown command {cmd!r}")

    # ---- the terminal actions --------------------------------------

    async def _shutdown(self, marks) -> None:
        try:
            await run_countdown(self.server, marks,
                                self.server.config.shutdown_notice,
                                sleeper=self.sleeper)
        except asyncio.CancelledError:
            return
        # ⛔ Ask the main loop to stop. It closes the doors and returns 0, and
        # `Restart=on-failure` leaves a clean exit alone — which is what "stays
        # down" means against systemd.
        self.server.request_stop()

    def _cancel(self) -> dict:
        if self._pending is None or self._pending.done():
            return {"ok": True, "cancelled": False}
        self._pending.cancel()
        self._pending = None
        self.server.announce(render(self.server.config.shutdown_cancelled_notice,
                                    self.server.theme.instance_name))
        self._record("shutdown_cancelled", [])
        return {"ok": True, "cancelled": True}

    # ---- the fact, never the words ---------------------------------

    def _record(self, action: str, marks) -> None:
        """⛔ The FACT of it, and nothing else. `actor="config"` is the live
        reload's precedent: the operator acted through the box rather than
        through a command, and naming the mechanism is more honest than naming
        nobody. The target is the countdown's LENGTH — a number — because the
        only other thing here is the message, and the message is not ours to
        keep."""
        events = getattr(self.server, "events", None)
        if events is None:
            return
        events.record("staff_action", actor="config", action=action,
                      target=f"{marks[0]}s" if marks else "-")
