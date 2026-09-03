# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Review buffers for private lines — the covenant's SECOND sanctioned exception.

The first exception was mail: addressed and consented, one sender deliberately
asking the server to hold specific words for one named recipient. This is the
second, and it is narrower in reach but wider in trigger: each account keeps the
last ``CAP`` **tell/remote lines it sent or received**, so a conversation
interrupted by a closed lid or a boss walking in can be picked back up.

Bounded exactly as mail is, and no wider:
- Encrypted at rest with Fernet, using the SAME ``mail.key`` — one key, one
  trust boundary, nothing new to protect.
- Plaintext on disk is only the casefolded owner key and a per-entry timestamp:
  enough to purge without decrypting a single line.
- **Exactly one read path: the owner reading their own** (``.revtell``). There is
  no command at any rank to read another's, and ``.mailpurge`` does not touch
  these — the same structural rule the mailboxes live under.
- Rolling: the buffer self-prunes to ``CAP``. The 180-day purge is a backstop
  for a buffer nobody has touched since, run beside mail's.
- Deleted with the account (``.deleteme``).

Room speech, emotes, thinks and shouts remain never-persisted. This holds private
lines only, for their own two participants, each in the wording they saw.

Audit discipline, as with mail: a held line never appears in a log or an error
path. The only facts logged anywhere about this file are counts.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import boxfiles

log = logging.getLogger("heresay")

DAY_SECONDS = 86400
CAP = 20            # lines kept per account — the fallback; the server passes
                    # config.revtell_count, which the agreement also quotes
PURGE_DAYS = 180    # backstop, matching mail


class RevTellStore:
    """Encrypted, atomically-written per-account rolling line buffers —
    **one file per account**.

    On-disk shape since v1.52.5 (``revtells.json.d/<hex key>.json``)::

        {"version": 1,
         "entries": [{"ts": 1720000000, "blob": "<fernet token>"}]}

    ``ts`` and the owner key are plaintext; ``blob`` encrypts {"line": "..."}.
    The owner key is the FILENAME now rather than a JSON key — see `boxfiles`.

    ⛔ **This is where audit C2 bit hardest.** `record()` runs on EVERY tell and
    remote, for BOTH participants, and it used to rewrite and fsync the whole
    store each time — so two people chatting paid, per line, for every account
    that had ever sent one. A buffer is twenty lines; that is all a `.tell`
    should ever cost, and now it is.

    Write-through is unchanged and needs no coalescing: a buffer is only useful
    if it survives the disconnect that made you want it, and one small file is
    cheap enough to write on the spot.
    """

    def __init__(self, path, key_path, cap: int = CAP, purge_days: int = PURGE_DAYS,
                 wall_clock=time.time):
        self.path = Path(path)
        # Derived, so no new Config field exists — see `boxfiles.box_dir`.
        # `self.path` remains the legacy file the migration reads and retires.
        self._box_dir = boxfiles.box_dir(self.path)
        self.key_path = Path(key_path)
        self.cap = cap
        self.purge_days = purge_days
        self._wall = wall_clock
        self._fernet = Fernet(self._load_or_create_key())
        # casefolded owner -> list of {"ts": float, "blob": str}, oldest first
        self._buffers: dict[str, list[dict]] = {}
        self._load()

    # ---- key management ---------------------------------------------

    def _load_or_create_key(self) -> bytes:
        """The same key mail uses. Whichever store starts first creates it."""
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        return key

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        """Rebuild the buffers from disk, splitting a legacy single file if one
        is there.

        ⚠ Same precedence as mail: if a legacy file sits beside populated
        per-account files, the SPLIT files win and the old one is left alone
        with a warning. Re-importing it would resurrect lines an account has
        since aged out.
        """
        self._buffers.clear()
        legacy = self.path.exists()
        split = boxfiles.has_boxes(self._box_dir)
        if legacy and not split:
            self._migrate_legacy()
            return
        if legacy and split:
            log.warning("revtell: ignoring %s — the per-account store in %s is "
                        "authoritative (rename or remove the old file)",
                        self.path, self._box_dir)
        for key, data in boxfiles.read_boxes(self._box_dir):
            entries = [{"ts": float(e["ts"]), "blob": e["blob"]}
                       for e in data.get("entries", ())]
            if entries:
                self._buffers[key] = entries

    def _migrate_legacy(self) -> None:
        """Split the single file per account, then retire it by RENAME.

        ⛔ Ciphertext moved verbatim; nothing here decrypts a line. ⚠ The cap is
        deliberately NOT re-applied on the way through: a buffer written under a
        larger `revtell_count` keeps what it holds until its owner's next line
        rolls it, exactly as a restart used to leave it. A migration is not the
        place to quietly delete somebody's lines.
        """
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for name, entries in data.get("buffers", {}).items():
            buf = self._buffers.setdefault(name.casefold(), [])
            buf.extend({"ts": float(e["ts"]), "blob": e["blob"]} for e in entries)
        for key in list(self._buffers):
            if self._buffers[key]:
                self._save_buffer(key)
            else:
                del self._buffers[key]
        retired = boxfiles.retire_legacy(self.path)
        log.info("revtell: split %d buffer(s) into %s; %s kept as %s",
                 len(self._buffers), self._box_dir, self.path.name, retired.name)

    def _save_buffer(self, key: str) -> None:
        """Write ONE account's buffer — the whole point of the layout."""
        boxfiles.write_box(self._box_dir, key, {
            "version": 1,
            "entries": self._buffers.get(key, []),
        })

    def _write(self, key: str) -> None:
        """Persist one buffer after a change; an emptied buffer loses its file."""
        if self._buffers.get(key):
            self._save_buffer(key)
        else:
            self._buffers.pop(key, None)
            boxfiles.remove_box(self._box_dir, key)

    # ---- the one read path -------------------------------------------

    def read(self, owner: str) -> list[str]:
        """The owner's own buffer, oldest first, exactly as they saw each line.

        This is the ONLY way anything leaves this store. It takes the owner's
        name and nothing else — there is no 'read someone's' shape to misuse.
        """
        out = []
        for e in self._buffers.get(owner.casefold(), ()):
            try:
                out.append(json.loads(
                    self._fernet.decrypt(e["blob"].encode()).decode())["line"])
            except (InvalidToken, ValueError, KeyError):
                # Never crash a read, never leak raw bytes.
                out.append("(unreadable line)")
        return out

    def count(self, owner: str) -> int:
        return len(self._buffers.get(owner.casefold(), ()))

    # ---- mutations ---------------------------------------------------

    def record(self, owner: str, line: str, timestamp: float | None = None) -> None:
        """Append one rendered line to ``owner``'s buffer, rolling at the cap.

        Write-through: a buffer is only useful if it survives the disconnect
        that made you want it.
        """
        key = owner.casefold()
        buf = self._buffers.setdefault(key, [])
        ts = self._wall() if timestamp is None else timestamp
        buf.append({
            "ts": ts,
            "blob": self._fernet.encrypt(json.dumps({"line": line}).encode()).decode(),
        })
        if len(buf) > self.cap:
            del buf[: len(buf) - self.cap]
        self._save_buffer(key)

    def delete_all(self, owner: str) -> int:
        """Drop an owner's whole buffer without reading it (``.deleteme``)."""
        key = owner.casefold()
        n = len(self._buffers.get(key, ()))
        if key in self._buffers:
            del self._buffers[key]
            boxfiles.remove_box(self._box_dir, key)
        return n

    def purge(self, now: float | None = None) -> int:
        """Backstop for buffers nobody has touched: drop entries older than
        ``purge_days``. Never decrypts anything. Returns how many went."""
        now = self._wall() if now is None else now
        cutoff = now - self.purge_days * DAY_SECONDS
        removed = 0
        # Only the buffers that actually lost something are written.
        for key in list(self._buffers.keys()):
            buf = self._buffers[key]
            kept = [e for e in buf if e["ts"] >= cutoff]
            if len(kept) == len(buf):
                continue
            removed += len(buf) - len(kept)
            self._buffers[key] = kept
            self._write(key)
        return removed
