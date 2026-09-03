# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command handlers — see the package __init__ for dispatch and shared helpers."""
from __future__ import annotations


QUIT = "quit"
REBOOT = "reboot"               # ditto: the boot theater is async (pauses, sound)


REBOOT_REFUSED = "The machine refuses. It only just got here."
# The fake confirmation. Whatever the user types (y, n, empty, gibberish), the
# sequence runs — they only *believe* they've reached for the whole server. Asked
# in server._reboot, after the rate check, before the sequence. See CLAUDE.md.
REBOOT_PROMPT = "Reboot server? (y/n) "


def cmd_reboot(session, args: str):
    """Play a personal 486-era boot sequence on the invoker's screen (async, so it
    defers to the session loop via REBOOT). Only the invoker sees/hears it; the
    room gets one dry emote. Missing reboot.txt -> "Nothing happens." (no prompt,
    no token spent). Own rate bucket: 1 per config.reboot_rate_seconds — checked
    HERE, before the fake "Reboot server? (y/n)" prompt, so a refused user never
    even sees the question. The prompt (and the emote, which fires once the
    sequence actually begins) live in server._reboot."""
    if not session.has_reboot_script():
        session.send("Nothing happens.")
        return None
    if not session.reboot_bucket.take(session.now()):
        session.send(REBOOT_REFUSED)
        return None
    return REBOOT


def cmd_quit(session, args: str) -> str:
    return QUIT
