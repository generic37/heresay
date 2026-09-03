# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mail — the covenant's single sanctioned exception to no-persistence.

Room conversation, emotes, shouts and tells are still never written down. Mail is
the one thing the server keeps, because it is **addressed and consented**: one
sender deliberately asked the server to hold specific words for one named
recipient. The promise is "we store nothing you haven't deliberately asked us to
hold" — and no wider.

Bounded precisely:
- Encrypted at rest with Fernet (AES-128-CBC + HMAC). The key lives in
  ``mail.key`` (mode 0600), created on first run.
- Only three plaintext fields per message: the recipient (the mailbox key), a
  timestamp — enough to purge without decrypting anything — and, since v1.34.0,
  a ``read`` flag. The flag is the owner's own metadata about their own box, not
  content: it says *whether* a message has been shown, never a word of it.
- Delivered on request only (``.mail``); deleted means deleted (remove + rewrite,
  no tombstone); purged after 180 days; mailboxes capped.
- The login notice fires on UNREAD mail only — see ``unread_count``.

What this protects: backups, casual reads of the JSON file, a stolen disk image.
What it does NOT protect against: an attacker who has both root on the running
host and the key file — they can decrypt, exactly as the server does. This is
honest at-rest encryption, not end-to-end secrecy.

Ephemerality/audit discipline: message bodies never appear in logs or error
paths, same as room text.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import boxfiles

log = logging.getLogger("heresay")

DAY_SECONDS = 86400

SCHEMA_VERSION = 2   # v2 adds the plaintext per-message `read` flag


@dataclass
class Message:
    index: int      # 1-based position in the mailbox
    sender: str
    timestamp: float  # epoch seconds
    body: str
    read: bool = True   # plaintext metadata, never content

    def date_str(self) -> str:
        # Local date, no time — mail is dated, not clocked.
        return time.strftime("%Y-%m-%d", time.localtime(self.timestamp))


class MailStore:
    """Encrypted, atomically-written mailboxes — **one file per mailbox**.

    On-disk shape since v1.52.5 (``mailboxes.json.d/<hex key>.json``)::

        {"version": 2,
         "messages": [{"ts": 1720000000, "read": false,
                       "blob": "<fernet token>"}]}

    ``ts``, ``read`` and the mailbox key are plaintext; ``blob`` encrypts
    {from, body}. The mailbox key is the FILENAME now rather than a JSON key —
    the same disclosure, differently spelled; see `boxfiles`.

    ⛔ **Why one file per box** (audit C2): every mutation used to rewrite and
    fsync the WHOLE store, so the cost of delivering one message scaled with the
    number of accounts that had ever received one — on the event loop, with the
    rest of the house waiting. A box is small and is the only thing a delivery
    touches. Nothing else changed: `_boxes` is still built at boot and every
    query still runs against memory.

    ⚠ **Durability is deliberately unchanged.** `deliver()` still writes and
    fsyncs before it returns True, so "Mail sent." still means the letter is on
    the disk. That promise used to be expensive; it is now cheap, which is the
    tension this dissolves rather than a trade it makes.

    The legacy single-file store is read once, split, and RENAMED aside; a
    pre-v2 message still migrates as **unread**, because whether it was read is
    genuinely not known and the flag may not claim otherwise.
    """

    def __init__(self, path, key_path, cap: int = 50, purge_days: int = 180,
                 wall_clock=time.time, box_purge_days: dict[str, int] | None = None,
                 box_caps: dict[str, int] | None = None):
        self.path = Path(path)
        # ⚠ DERIVED from the configured path, so per-box storage added no
        # `Config` field to be misconfigured or left unclassified — see
        # `boxfiles.box_dir`. `self.path` is still the legacy file: it is what
        # the migration reads and what `retire_legacy` renames.
        self._box_dir = boxfiles.box_dir(self.path)
        self.key_path = Path(key_path)
        self.cap = cap
        self.purge_days = purge_days
        # ⚠ A per-BOX retention override (v1.50.0). One store, one file, one key,
        # one purge loop — but the report box ages out on its own number, not
        # mail's 180. Reports are the only box that needs this today, and giving
        # the mechanism a dict rather than the report box a special case is what
        # keeps `purge()` a single loop.
        self._box_purge_days = {k.casefold(): int(v)
                                for k, v in (box_purge_days or {}).items()}
        # ⛔ A per-BOX CAP override (v1.58.0), the same mechanism one field
        # along. The report box inheriting `mailbox_cap`'s 50 was an accident of
        # sharing the store rather than a decision: a mailbox cap bounds how
        # much one person can be sent, while the report box is the whole house's
        # single channel for saying something is wrong. Fifty junk reports could
        # wedge it shut — the 51st is refused, and the resident with something
        # real to say is told the box is full.
        self._box_caps = {k.casefold(): int(v)
                          for k, v in (box_caps or {}).items()}
        self._wall = wall_clock
        self._fernet = Fernet(self._load_or_create_key())
        # mailbox name (lowercased) -> list of {"ts": float, "blob": str}
        self._boxes: dict[str, list[dict]] = {}
        self._load()

    # ---- key management ---------------------------------------------

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0600 from the start (O_EXCL so we never clobber a key).
        fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
            fh.flush()
            os.fsync(fh.fileno())
        return key

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        """Rebuild `_boxes` from disk, migrating a legacy single file if there
        is one.

        ⚠ **The precedence, when both shapes exist: the SPLIT boxes win.** A
        legacy file beside a populated box directory means the migration has
        already run and something put the old file back — a restore, a rsync, a
        hand copy. Re-importing it would resurrect deleted mail and double-count
        what is still there, so it is left strictly alone and named in one
        warning, every boot, because an ambiguous store is a thing an operator
        should be told about until they resolve it.
        """
        self._boxes.clear()
        legacy = self.path.exists()
        split = boxfiles.has_boxes(self._box_dir)
        if legacy and not split:
            self._migrate_legacy()
            return
        if legacy and split:
            log.warning("mail: ignoring %s — the per-box store in %s is "
                        "authoritative (rename or remove the old file)",
                        self.path, self._box_dir)
        for key, data in boxfiles.read_boxes(self._box_dir):
            msgs = [{"ts": float(m["ts"]),
                     "read": bool(m.get("read", False)),
                     "blob": m["blob"]}
                    for m in data.get("messages", ())]
            if msgs:
                self._boxes[key] = msgs

    def _migrate_legacy(self) -> None:
        """Split the single file into one file per box, then retire it.

        ⛔ Ciphertext is MOVED, never decrypted: every `blob` is carried across
        byte for byte, with its `ts` and its `read` flag. The two old in-place
        migrations still run here, because this is the last time the legacy
        shape is ever read — a non-casefolded mailbox key is re-keyed, and a
        pre-v2 message with no `read` field becomes UNREAD.
        """
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        for name, msgs in data.get("mailboxes", {}).items():
            box = self._boxes.setdefault(name.casefold(), [])
            for m in msgs:
                box.append({"ts": float(m["ts"]),
                            "read": bool(m.get("read", False)),
                            "blob": m["blob"]})
        for key in list(self._boxes):
            if self._boxes[key]:
                self._save_box(key)
            else:
                del self._boxes[key]
        retired = boxfiles.retire_legacy(self.path)
        log.info("mail: split %d mailbox(es) into %s; %s kept as %s",
                 len(self._boxes), self._box_dir, self.path.name, retired.name)

    def _save_box(self, key: str) -> None:
        """Write ONE mailbox. The whole point of the layout."""
        boxfiles.write_box(self._box_dir, key, {
            "version": SCHEMA_VERSION,
            "messages": self._boxes.get(key, []),
        })

    def _write(self, key: str) -> None:
        """Persist one box after a change: write it, or remove it if it emptied.

        An empty box has no file, exactly as an empty box had no JSON key —
        `has_mail` reads memory either way, and a file full of `[]` would be a
        record that somebody once had mail, which is not a thing worth keeping.
        """
        if self._boxes.get(key):
            self._save_box(key)
        else:
            self._boxes.pop(key, None)
            boxfiles.remove_box(self._box_dir, key)

    # ---- queries -----------------------------------------------------

    def count(self, recipient: str) -> int:
        return len(self._boxes.get(recipient.casefold(), ()))

    def has_mail(self, recipient: str) -> bool:
        return self.count(recipient) > 0

    def unread_count(self, recipient: str) -> int:
        return sum(1 for m in self._boxes.get(recipient.casefold(), ())
                   if not m.get("read", True))

    def has_unread(self, recipient: str) -> bool:
        """What the login notice asks. A non-empty box is not news; unread is."""
        return self.unread_count(recipient) > 0

    def read(self, recipient: str) -> list[Message]:
        """Decrypt and return the whole box. Does NOT mark anything read — the
        caller that actually SHOWS the messages calls ``mark_read``, so a read
        that is never displayed cannot silence the notice."""
        out = []
        for i, m in enumerate(self._boxes.get(recipient.casefold(), ()), start=1):
            try:
                payload = json.loads(self._fernet.decrypt(m["blob"].encode()).decode())
                sender = payload.get("from", "?")
                body = payload.get("body", "")
            except (InvalidToken, ValueError, KeyError):
                # A corrupt/unreadable message is shown as such — never crash a
                # mailbox read, and never leak raw bytes.
                sender, body = "?", "(unreadable message)"
            out.append(Message(index=i, sender=sender, timestamp=m["ts"],
                               body=body, read=bool(m.get("read", True))))
        return out

    # ---- mutations ---------------------------------------------------

    def deliver(self, recipient: str, sender: str, body: str,
                timestamp: float | None = None) -> bool:
        """Store a message. Returns False (nothing stored) if the box is full."""
        key = recipient.casefold()
        if self.count(key) >= self.cap_for(key):
            return False  # nothing stored; the sender is told the box is full
        box = self._boxes.setdefault(key, [])
        ts = self._wall() if timestamp is None else timestamp
        blob = self._fernet.encrypt(
            json.dumps({"from": sender, "body": body}).encode()
        ).decode()
        box.append({"ts": ts, "read": False, "blob": blob})
        # ⚠ Written and fsynced BEFORE this returns True, so the sender is only
        # told "Mail sent." once the letter is on the disk. Unchanged from the
        # single-file store — it is simply one small file now instead of all of
        # them, which is what made keeping the promise cheap.
        self._save_box(key)
        return True

    def mark_read(self, recipient: str, indices=None) -> int:
        """Mark messages read. ``indices`` is 1-based (None = the whole box).

        Returns how many actually changed; the file is rewritten only then, so
        re-reading an already-read mailbox costs no disk write.
        """
        box = self._boxes.get(recipient.casefold())
        if not box:
            return 0
        if indices is None:
            targets = range(1, len(box) + 1)
        else:
            targets = indices
        changed = 0
        for i in targets:
            if 1 <= i <= len(box) and not box[i - 1].get("read", True):
                box[i - 1]["read"] = True
                changed += 1
        if changed:
            self._save_box(recipient.casefold())
        return changed

    def delete(self, recipient: str, index: int) -> bool:
        """Delete message ``index`` (1-based). Really gone: removed and rewritten."""
        box = self._boxes.get(recipient.casefold())
        if not box or index < 1 or index > len(box):
            return False
        del box[index - 1]
        self._write(recipient.casefold())
        return True

    def purge_mailbox(self, recipient: str) -> int:
        """Delete an ENTIRE mailbox without reading it (arch spam cleanup).

        Never decrypts anything. Returns the number of messages removed.
        """
        key = recipient.casefold()
        n = len(self._boxes.get(key, ()))
        if key in self._boxes:
            del self._boxes[key]
            boxfiles.remove_box(self._box_dir, key)
        return n

    def cap_for(self, recipient: str) -> int:
        """How many messages THIS box holds. ``cap`` unless overridden.

        ⚠ The report box is the one caller of the override today, exactly as it
        is for `purge_days_for`. Ordinary mailboxes are untouched by it, which
        is what keeps `mailbox_cap` meaning what it has always meant.
        """
        return self._box_caps.get(recipient.casefold(), self.cap)

    def purge_days_for(self, recipient: str) -> int:
        """How long THIS box keeps a message. ``purge_days`` unless overridden.

        The report box (v1.50.0) is the one caller of the override: it ages on
        `report_retention_days`, not on mail's 180, because expiry is the ONLY
        way a report ever leaves — there is no delete verb for it.
        """
        return self._box_purge_days.get(recipient.casefold(), self.purge_days)

    def purge(self, now: float | None = None) -> int:
        """Delete messages older than this box's retention. Returns how many went."""
        now = self._wall() if now is None else now
        removed = 0
        # ⚠ The one operation that legitimately touches many boxes — and it
        # still writes only the ones that CHANGED, so a nightly purge over a
        # house where nothing expired costs no disk at all.
        for key in list(self._boxes.keys()):
            box = self._boxes[key]
            cutoff = now - self.purge_days_for(key) * DAY_SECONDS
            kept = [m for m in box if m["ts"] >= cutoff]
            if len(kept) == len(box):
                continue
            removed += len(box) - len(kept)
            self._boxes[key] = kept
            self._write(key)
        return removed
