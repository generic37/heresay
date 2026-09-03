# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ban history — every ban ever placed, kept longer than the ban itself.

`bans.json` is ENFORCEMENT: who is refused entry, and until when. It is
deliberately short-lived — a ban expires and the record goes with it, because
holding an expired ban would be holding a punishment that has ended.

This is a different question: **has this happened before?** An address banned
three times in a month is a different situation from one banned once, and
`bans.json` cannot tell you because by then the earlier bans are gone. So the
history outlives them.

⛔ **THIS FILE HOLDS THE IP, AND IT IS THE ONLY REASON IT DOES.**

Nothing prints an IP — at no level, not even the owner's (see the "IPs are never
shown" rule). The address is kept here so the engine can match it INTERNALLY:
recidivism is checked address-to-address, which is what catches the same person
returning under a fresh account name. A name-keyed history could not do that,
and doing it is the entire justification for keeping the address at all.

The raw value is reachable exactly one way: an operator deliberately opening the
file during a security audit. That is a considered act with a reason attached,
not something a command can be talked into.

**Retention is the operator's, and defaults to forever** (`ban_history_days`,
0 = keep everything). That is the opposite default from the event log, and
deliberately so: the event log answers "what is happening", which is a question
about now, while this answers "has this happened before", which is a question
whose whole value is in the years. An operator who wants a finite window sets
one and the store honours it.

Facts only, like everything else the engine writes: an address, account names,
a reason line an operator typed about an account, timestamps. Never a word
anyone said.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("heresay")

DAY_SECONDS = 86400


class BanHistoryStore:
    """Append-only-in-spirit JSON: every ban placed, oldest first.

    Written whole rather than appended a line at a time (unlike the event log)
    because entries are MUTATED — a ban that is lifted early gets its
    `lifted_at` stamped — and because the volume is a handful of records a year
    rather than several per connection.
    """

    def __init__(self, path, retention_days: float = 0.0, wall_clock=time.time):
        self.path = Path(path)
        # 0 (or less) means keep forever — the shipped default. See the module
        # note: this store's value is in the years.
        self.retention_days = float(retention_days)
        self._wall = wall_clock
        self._entries: list[dict] = []
        self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        self._entries = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A corrupt history must not stop the house opening: it is a
            # reference, not a gate. Say so once and carry on empty.
            log.warning("ban history: could not read %s — starting empty",
                        self.path)
            return
        for rec in data.get("bans", []):
            if isinstance(rec, dict) and rec.get("ip"):
                self._entries.append(rec)
        self.purge()

    def _save(self) -> None:
        payload = {"version": 1, "bans": self._entries}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent),
                                   prefix=".banhistory-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)       # it holds addresses; owner only
            os.replace(tmp, self.path)
        except OSError:
            log.warning("ban history: could not write %s", self.path)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # ---- writing -----------------------------------------------------

    def record(self, ip: str, account: str, actor: str, hours: float,
               reason: str = "") -> dict:
        """Note a ban that has just been placed. Returns the stored entry.

        ``account`` is the name `.ban_ip` was RUN ON — the label the listing and
        the staff log use, so nothing downstream has to hold an address to say
        what happened.
        """
        now = float(self._wall())
        entry = {
            "ip": ip,
            "account": account,
            "actor": actor,
            "reason": reason,
            "set_at": now,
            "hours": float(hours),
            "expires_at": now + float(hours) * 3600.0,
            "lifted_at": None,
        }
        self._entries.append(entry)
        self.purge()
        self._save()
        return entry

    def mark_lifted(self, ip: str) -> int:
        """Stamp the still-open entries for ``ip`` as lifted, now.

        "Still open" means not already lifted and not yet expired — an `.unban_ip`
        after natural expiry changes nothing, which is correct: the ban had
        already ended on its own.
        """
        now = float(self._wall())
        n = 0
        for e in self._entries:
            if e["ip"] == ip and e.get("lifted_at") is None \
                    and float(e.get("expires_at", 0)) > now:
                e["lifted_at"] = now
                n += 1
        if n:
            self._save()
        return n

    # ---- reading -----------------------------------------------------

    def for_ip(self, ip: str) -> list[dict]:
        """Every recorded ban on this address, oldest first.

        ⚠ Matched on the ADDRESS, which is the point: it finds the same person
        coming back under a name nobody has seen before. A name-keyed history
        would miss exactly the case worth catching.
        """
        return [e for e in self._entries if e["ip"] == ip]

    def all(self) -> list[dict]:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    # ---- retention ---------------------------------------------------

    def purge(self) -> int:
        """Drop entries older than the window. 0 days keeps everything."""
        if self.retention_days <= 0:
            return 0
        cutoff = float(self._wall()) - self.retention_days * DAY_SECONDS
        keep = [e for e in self._entries if float(e.get("set_at", 0)) >= cutoff]
        dropped = len(self._entries) - len(keep)
        if dropped:
            self._entries = keep
        return dropped
