# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bans — refuse login/connection by account name or IP.

This is **operational data, not conversation**: who is denied entry and until
when. It is covenant-clean for the same reason the account file is — it holds no
message content, ever. Persisted to ``bans.json`` (gitignored) with the same
atomic temp+rename write as the other stores, so a ban outlives a restart.

Names are compared casefolded (matching account identity); IPs verbatim.
Everything auto-expires; a wall clock is injected so tests are deterministic.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .atomicwrite import atomic_write_json

HOUR_SECONDS = 3600


def _subject_key(kind: str, subject: str) -> str:
    return f"{kind}:{subject.casefold() if kind == 'name' else subject}"


class BanStore:
    def __init__(self, path, wall_clock=time.time):
        self.path = Path(path)
        self._wall = wall_clock
        # key -> {"kind","subject","until","reason"}
        self._bans: dict[str, dict] = {}
        self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        self._bans.clear()
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for rec in data.get("bans", []):
            kind = rec.get("kind")
            subject = rec.get("subject")
            if kind not in ("name", "ip") or not subject:
                continue
            self._bans[_subject_key(kind, subject)] = {
                "kind": kind,
                "subject": subject,
                "until": float(rec["until"]),
                "reason": rec.get("reason", ""),
            }
        # Drop anything already expired at load; rewrite if we changed the file.
        if self.purge_expired():
            pass  # purge_expired already saved

    def _save(self) -> None:
        payload = {"version": 1, "bans": list(self._bans.values())}
        atomic_write_json(self.path, payload)

    # ---- queries -----------------------------------------------------

    def _active(self, kind: str, subject: str, now: float) -> bool:
        rec = self._bans.get(_subject_key(kind, subject))
        return rec is not None and rec["until"] > now

    def is_banned_name(self, name: str, now: float | None = None) -> bool:
        now = self._wall() if now is None else now
        return self._active("name", name, now)

    def is_banned_ip(self, ip: str, now: float | None = None) -> bool:
        now = self._wall() if now is None else now
        return self._active("ip", ip, now)

    def reason(self, kind: str, subject: str) -> str:
        rec = self._bans.get(_subject_key(kind, subject))
        return rec["reason"] if rec else ""

    def list_active(self, now: float | None = None) -> list[dict]:
        now = self._wall() if now is None else now
        return [r for r in self._bans.values() if r["until"] > now]

    # ---- mutations ---------------------------------------------------

    def ban(self, subject: str, kind: str, hours: float,
            now: float | None = None, reason: str = "") -> float:
        """Ban a name or IP for ``hours``. Returns the expiry epoch."""
        now = self._wall() if now is None else now
        until = now + hours * HOUR_SECONDS
        self._bans[_subject_key(kind, subject)] = {
            "kind": kind, "subject": subject, "until": until, "reason": reason,
        }
        self._save()
        return until

    def unban(self, subject: str, kind: str | None = None) -> bool:
        """Lift a ban. If kind is None, lift both a name and IP ban on ``subject``."""
        removed = False
        kinds = [kind] if kind else ["name", "ip"]
        for k in kinds:
            if self._bans.pop(_subject_key(k, subject), None) is not None:
                removed = True
        if removed:
            self._save()
        return removed

    def purge_expired(self, now: float | None = None) -> int:
        now = self._wall() if now is None else now
        stale = [k for k, r in self._bans.items() if r["until"] <= now]
        for k in stale:
            del self._bans[k]
        if stale:
            self._save()
        return len(stale)
