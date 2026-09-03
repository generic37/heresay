# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Common-password rejection.

A bundled blocklist of the most common breached passwords (see
``data/common_passwords.NOTICE.txt`` for source + license). Loaded once into a
lowercased set; a candidate is rejected if its lowercase form is a member —
case-insensitive, so ``Password`` and ``PASSWORD`` are caught along with
``password``. This blunts distributed/botnet guessing, which sprays exactly
these first, in a way per-IP limits (many IPs) cannot.

Engine data, not house content: it is the same in every themed instance and
lives in the package, never under ``house/``.
"""

from __future__ import annotations

from pathlib import Path

_DATA_FILE = Path(__file__).resolve().parent / "data" / "common_passwords.txt"


def _load() -> frozenset[str]:
    try:
        text = _DATA_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()   # never let a missing data file stop the daemon
    return frozenset(
        line.strip().lower() for line in text.splitlines() if line.strip()
    )


# Loaded once at import; ~10k entries.
COMMON_PASSWORDS: frozenset[str] = _load()


def is_common(password: str) -> bool:
    """True if ``password`` is on the common/breached blocklist (case-insensitive).

    Not whitespace-stripped: a password's surrounding spaces are part of it (see
    session.ask's secret-verbatim handling), so " password " is not "password".
    """
    return password.lower() in COMMON_PASSWORDS
