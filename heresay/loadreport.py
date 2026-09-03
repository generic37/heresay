# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The last load's report — what the engine saw when it read the house.

**The problem this solves.** Startup says a great deal and then forgets it. How
many rooms were read, how many help topics, which passages are declared from one
side only, and — the part that matters — every warning: a retired header, an
orphan narrow file, a malformed `[ranks]` block, an unreadable favicon, an
account holding a rank the ladder no longer has. All of it goes to stdout, which
in production means journald, which means a builder who mistyped a filename has
to SSH to the box to find out. The house knew; it just had nowhere to say so.

So the load is captured a SECOND time, into memory, and `.load_report` shows it.

**In memory, rebuilt every boot, and that is the whole of its lifecycle.** Not a
file, not the event log, not a line of persistence. A load report describes the
process that is running right now; a stale one from the last restart would be
worse than none. It holds authoring facts — filenames, room ids, counts — and
nothing about any person, so it has no covenant question to answer.

⚠ **Additive, never a replacement.** journald still receives every line exactly
as before; this is a second listener on the same messages. The split the engine
lives under (process output to the log, domain facts to the event store) is
untouched — a load warning is process output and stays there.

**How it is filled — two mechanisms, deliberately:**

- **Warnings arrive through a scoped log handler** (`capturing()`), so the buffer
  is complete BY CONSTRUCTION: every `log.warning` the load path emits is
  captured, including ones written after this file was, and including ones in
  modules that have never heard of it. A list of explicit `report.warn()` calls
  would be a list somebody has to remember to extend.
- **The counts and the one-way passages are recorded explicitly**, because they
  are structured — numbers and pairs — and the screen lays them out as such.
  Recovering them by re-parsing the log lines they were already formatted into
  would be the wrong direction of travel.

The handler is attached for the load phase only and removed in a `finally`, so
nothing said at runtime can reach a screen that claims to describe the boot.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager

LOGGER_NAME = "heresay"

# A bound on the buffer, because it lives for the life of the process and a
# pathological house (a thousand orphan files) should not be able to grow it
# without limit. Far above any real load; the render says when it bites.
WARNING_CAP = 200


class LoadReport:
    """What the engine saw while it read the house, held for `.load_report`.

    One per server, built before anything else so the timestamp is the moment
    the load began rather than the moment it finished.
    """

    def __init__(self, wall_clock=time.time):
        self._wall = wall_clock
        self.loaded_at = float(wall_clock())
        # label -> (n, singular, plural), in insertion order: the counts line.
        self._counts: list[tuple[int, str, str]] = []
        self.facts: list[str] = []            # short lines: entry/jail, favicon
        self.doors: list[str] = []            # which listeners came up
        self.one_way: list[tuple[str, str]] = []
        self.warnings: list[str] = []
        self.dropped = 0                      # warnings past the cap
        # The last live reload (v1.37.0), or None if nothing has been reloaded
        # since boot. A `reload.ReloadReport`, held here rather than in its own
        # screen because "what did the engine make of your files" is ONE
        # question whether the files were read at boot or five minutes ago.
        self.last_reload = None

    # ---- filling it in (explicit, from the loaders) -------------------

    def count(self, n: int, singular: str, plural: str = None) -> None:
        self._counts.append((int(n), singular, plural or singular + "s"))

    def fact(self, text: str) -> None:
        if text:
            self.facts.append(str(text))

    def door(self, name: str) -> None:
        self.doors.append(str(name))

    def note_one_way(self, pairs) -> None:
        self.one_way = [(str(a), str(b)) for a, b in pairs]

    def warn(self, text: str) -> None:
        """Record one warning. Never raises — a report that can break a load
        would be worse than no report."""
        if len(self.warnings) >= WARNING_CAP:
            self.dropped += 1
            return
        self.warnings.append(str(text))

    # ---- reading it back ---------------------------------------------

    def count_line(self) -> str:
        """``26 rooms, 100 echoes, 56 help topics`` — or "" if nothing counted."""
        return ", ".join(f"{n} {s if n == 1 else p}" for n, s, p in self._counts)

    def door_line(self) -> str:
        return ("doors: " + ", ".join(self.doors)) if self.doors else ""

    def summary_lines(self) -> list[str]:
        """The counts, then the facts, then the doors — each its own line."""
        out = [self.count_line()] if self._counts else []
        out.extend(self.facts)
        if self.doors:
            out.append(self.door_line())
        return [l for l in out if l]

    def stamp(self) -> str:
        """The load time, local to the server — the same clock and the same
        shape `.staff_actions` renders an event with."""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.loaded_at))

    # ---- the scoped handler ------------------------------------------

    @contextmanager
    def capturing(self, logger_name: str = LOGGER_NAME):
        """Capture every WARNING (and worse) the engine logs inside this block.

        Attached to the package logger, so a warning from any module the load
        touches — world, theme, accounts, state, events, the server itself — is
        caught without that module knowing this exists. Removed in a `finally`:
        a load that raises must not leave a listener behind, and a runtime
        warning must never appear on a screen headed "Last load".
        """
        logger = logging.getLogger(logger_name)
        handler = _CaptureHandler(self)
        logger.addHandler(handler)
        try:
            yield self
        finally:
            logger.removeHandler(handler)


class _CaptureHandler(logging.Handler):
    """A logging handler that files each record into a LoadReport.

    WARNING and above only: the INFO lines are the counts, and those are
    recorded structurally rather than scraped back out of their own formatting.
    """

    def __init__(self, report: LoadReport):
        super().__init__(level=logging.WARNING)
        self._report = report

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._report.warn(record.getMessage())
        except Exception:                      # pragma: no cover - never break a load
            pass
