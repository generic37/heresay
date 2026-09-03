# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The .reboot boot-theater: parse a boot script, play it with timing + sound.

``reboot.txt`` (repo root, operator-editable) is loaded at startup like
``agreement.txt`` and parsed once into a flat list of *steps*. A missing file is
never fatal — ``.reboot`` simply says "Nothing happens." (the server never fails
over it). See ``server._reboot`` for the playback wiring and ``commands.cmd_reboot``
for the room-facing emote + rate limit.

Covenant note: sound rides only on the invoker's own command (see the CLAUDE.md
amendment). The playback writes to ONE transport — the person who typed
``.reboot`` — and nothing here is ever triggered by another person's activity.

Everything here is pure and clock-injected: the parser takes text in and returns
steps; the player takes an async ``sleep`` and a ``sink`` it writes raw frames to,
so both are exercised deterministically in tests with no real socket or wall clock.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Optional

from .transport import CONTROL_PREFIX

# ---- knobs (code, not config) -------------------------------------------

PAUSE_BUDGET_SECONDS = 240.0  # total @pause/@progress budget; can't stall forever
CLEAR_LINES = 24              # @clear: a screen-pushing gap (no ANSI)
BEL = "\x07"                  # ASCII BEL
PROGRESS_CELLS = 20           # @progress: '#' cells in the bar (mirror the web
                              #            client's PROGRESS.width in index.html)
PROGRESS_STAGES = 5           # @progress: number of discrete, newline-terminated
                              #            bar lines drawn on the socket doors (tt++
                              #            split mode mangles unterminated lines, so
                              #            we stage full lines, never same-line append)

# Socket-door fallback for @sound: each named cue's built-in BEL pattern, as
# (count, gap_seconds). ``fan`` is silent on the socket doors (a steady hum has
# no BEL analogue). An unknown name is silent on the socket doors; on the web
# door it still rides as a \x01sound frame (the client no-ops what it can't play).
SOUND_BEL = {
    "power": (1, 0.0),
    "fan": (0, 0.0),
    "hdd": (3, 0.3),
    "floppy": (2, 0.5),
    "beep": (1, 0.0),
}

DEFAULT_WIDTH = 78


# ---- loading -------------------------------------------------------------

def load_script(path) -> Optional[str]:
    """Read the boot-script text, or None if the file is absent.

    Absent is not an error (unlike agreement.txt / rooms.toml): the server keeps
    running and ``.reboot`` degrades to "Nothing happens."
    """
    p = Path(path)
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8")


# ---- parsing -------------------------------------------------------------

def _parse_directive(stripped: str):
    """Parse one ``@directive`` line into a step, or None if it isn't one we
    recognise (the caller then shows it verbatim)."""
    parts = stripped.split(None, 1)
    head = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if head == "@pause":
        try:
            return ("pause", float(arg))
        except ValueError:
            return None
    if head == "@clear" and not arg:
        return ("clear",)
    if head == "@beep" and not arg:
        return ("beep",)
    if head == "@sound" and arg:
        return ("sound", arg.split()[0])
    if head == "@progress" and arg:
        p = arg.split(None, 1)
        try:
            secs = float(p[0])
        except ValueError:
            return None
        label = p[1].strip() if len(p) > 1 else ""
        if not label:
            return None
        return ("progress", secs, label)
    return None


def parse(text: str, *, width: int = DEFAULT_WIDTH) -> list:
    """Turn boot-script text into an ordered list of steps.

    Steps: ``("pause", secs)``, ``("clear",)``, ``("beep",)``,
    ``("sound", name)``, ``("progress", secs, label)``, ``("line", text)``. Every
    non-directive line is shown verbatim; a line longer than ``width`` is wrapped,
    but a line that already fits keeps its exact characters (indentation and inner
    spacing intact — a 486 memory-map must line up)."""
    steps: list = []
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        stripped = line.strip()
        if stripped.startswith("@"):
            directive = _parse_directive(stripped)
            if directive is not None:
                steps.append(directive)
                continue
            # An unrecognised @line falls through to verbatim display.
        if len(line) <= width:
            steps.append(("line", line))
        else:
            for w in textwrap.wrap(line, width=width, break_long_words=False,
                                   break_on_hyphens=False):
                steps.append(("line", w))
    return steps


# ---- playback ------------------------------------------------------------

def _progress_line(label: str, stage: int, stages: int) -> str:
    """One socket-door progress bar line for ``stage`` of ``stages`` (0-based).

    The fill grows evenly from 0 to ``PROGRESS_CELLS`` across the stages; the
    unfilled remainder is spaces. The final (full) stage carries a `` done``
    suffix. Every line is a complete line — the caller newline-terminates it."""
    if stages > 1:
        filled = min(round(stage * PROGRESS_CELLS / (stages - 1)), PROGRESS_CELLS)
    else:
        filled = PROGRESS_CELLS
    if filled >= PROGRESS_CELLS:
        return f"{label} [{'#' * PROGRESS_CELLS}] done"
    return f"{label} [{'#' * filled}{' ' * (PROGRESS_CELLS - filled)}]"


async def play(steps, sink, *, sleep, sound_frames: bool,
               pause_budget: float = PAUSE_BUDGET_SECONDS) -> None:
    """Play ``steps`` to ``sink``, sleeping via the injected ``sleep``.

    ``sink.write(text)`` emits one raw frame (already newline-terminated for
    visible lines; a bare control frame or BEL for the rest); ``sink.flush()``
    awaits delivery so the timing the operator authored is what the eye sees.

    ``sound_frames`` picks the door's channel: True → the web door, where
    ``@sound`` rides as a ``\\x01sound <name>`` control frame and ``@progress``
    as ``\\x01progress <secs> <label>`` (the client animates the bar); False →
    the socket doors, where ``@sound`` becomes the cue's built-in BEL pattern
    (spec §3) and ``@progress`` draws ``PROGRESS_STAGES`` discrete, fully
    newline-terminated bar lines spread across the duration (no same-line append
    — tt++ split mode would mangle an unterminated line), ending in ``] done``.

    The total ``@pause`` + ``@progress`` time is clamped to ``pause_budget`` —
    once spent, later waits are zero, so no script can wedge the session open.
    """
    budget = float(pause_budget)
    for step in steps:
        kind = step[0]
        if kind == "line":
            sink.write(step[1] + "\n")
        elif kind == "clear":
            sink.write("\n" * CLEAR_LINES)
        elif kind == "beep":
            sink.write(BEL)              # @beep: BEL on every door
        elif kind == "sound":
            name = step[1]
            if sound_frames:
                sink.write(CONTROL_PREFIX + "sound " + name)
            else:
                count, gap = SOUND_BEL.get(name, (0, 0.0))
                for i in range(count):
                    if i > 0 and gap > 0:
                        await sink.flush()
                        await sleep(gap)
                    sink.write(BEL)
        elif kind == "progress":
            secs, label = step[1], step[2]
            dur = max(0.0, min(float(secs), budget))   # counts against the budget
            budget -= dur
            if sound_frames:
                # Web: one control frame; the client animates over `dur`. We wait
                # the same span so the sequence stays in step with the animation.
                sink.write(f"{CONTROL_PREFIX}progress {dur:g} {label}")
                await sink.flush()
                await sleep(dur)
            else:
                # Socket: N discrete, fully newline-terminated bar lines evenly
                # spread across `dur`. No same-line append — tt++ (split mode)
                # re-renders unterminated lines and would mangle it.
                stages = PROGRESS_STAGES
                gap = dur / (stages - 1) if stages > 1 else 0.0
                for i in range(stages):
                    if i > 0:
                        await sink.flush()
                        await sleep(gap)
                    sink.write(_progress_line(label, i, stages) + "\n")
            await sink.flush()
            continue
        elif kind == "pause":
            secs = max(0.0, min(float(step[1]), budget))
            budget -= secs
            await sink.flush()
            await sleep(secs)
            continue
        await sink.flush()
