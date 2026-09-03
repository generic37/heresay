# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One file per box — the persistence layer under mail and `.revtell`.

⛔ **WHY THIS EXISTS: a delivery must cost one box, not the whole store**
(v1.52.5, audit C2). Both stores kept every box in a single JSON document and
rewrote it — serialise, write, **fsync** — on every single mutation. So the cost
of one `.tell` scaled with how many people had ever used the house: on a store
of a thousand accounts the whole thing was re-serialised and re-synced to say one
line to one person, **on the event loop**, with every other session waiting on it.
The measurement is in the release notes; the shape of it is that the cost of
speaking to one person was a function of everybody else.

**What changed is ONLY the persistence granularity.** The in-memory model is
exactly what it was — one dict of boxes, loaded at boot, every query untouched —
and the public API of both stores is unchanged to the character. What moved is
that `_save()` (the whole store) became `_save_box(key)` (one small file). Boot
still reads everything once, which is the right place to pay O(total): the bug
was never the boot, it was the per-message cost.

**The filename is the box key, hex-encoded** (`corvid` → `636f72766964.json`):

- **Path-safe by construction.** The output alphabet is `[0-9a-f]` and nothing
  else, so a key containing `..`, `/`, `\\`, a NUL or a newline cannot escape the
  directory, and no call site has to remember to sanitise anything. ⛔ A raw key
  is NEVER joined onto a path.
- **Reversible**, which is what makes enumeration at boot possible without a
  second index file that could drift from the files it describes.
- **No special case for the reserved boxes.** `#staff` and `#report` begin with
  a character no account name may start with, and hex does not care.

⚠ **The box owner is as visible as it ever was, and this is worth saying
plainly.** A directory listing shows one hex-encoded name per box, which anybody
can decode; the old `mailboxes.json` showed the same names as plaintext JSON
keys to anybody who opened it. Same disclosure, differently spelled — not a
regression, and not an improvement either. (Hiding box owners from a listing
would take a NON-reversible name, which then needs the real key stored inside
the file for enumeration — trading the "only ts, read and the box identity are
plaintext" property for nothing. If that is ever wanted it is a deliberate
change, not a tweak.)

**The message bodies are untouched by all of this.** They are Fernet tokens
before and after; this module moves ciphertext between files and never decrypts
anything.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .atomicwrite import atomic_write_json

log = logging.getLogger("heresay")

SUFFIX = ".json"

# What a retired single-file store is renamed to. ⚠ RENAMED, never deleted: a
# hand rollback stays possible for as long as the operator keeps the file, and
# the one thing a storage migration must never do is be the only copy.
LEGACY_SUFFIX = ".pre-split"

DIR_MODE = 0o700     # a listing of box files is a list of who has mail
FILE_MODE = 0o600


def box_dir(path) -> Path:
    """The per-box directory for a store configured as ``path``.

    ⚠ DERIVED, so no new `Config` field exists to be misconfigured — and derived
    from the WHOLE filename (`mailboxes.json` → `mailboxes.json.d`) rather than
    the stem, because two stores configured as `x.json` and `x.dat` in one
    directory would otherwise share a box directory and silently interleave two
    kinds of record. The ugliness buys an impossibility.
    """
    path = Path(path)
    return path.parent / (path.name + ".d")


def encode_key(key: str) -> str:
    return key.encode("utf-8").hex()


def decode_key(stem: str) -> str | None:
    """The key a filename stands for, or None if it is not one of ours.

    A stray file in the directory is skipped rather than fatal — it is somebody
    else's, or an editor's leftover, and a store that refuses to open because of
    a stray file is worse than one that ignores it.
    """
    try:
        return bytes.fromhex(stem).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def box_path(directory: Path, key: str) -> Path:
    return Path(directory) / (encode_key(key) + SUFFIX)


def ensure_dir(directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, DIR_MODE)
    except OSError:
        # A directory somebody else owns (a bind mount, an odd umask): the files
        # inside are still 0600, so this is a hardening step and not a
        # correctness one. Never a reason to refuse to run.
        pass
    return directory


def write_box(directory: Path, key: str, payload: dict) -> Path:
    """One box, atomically: temp file in the same directory, 0600, fsync, rename.

    The same write the whole store used to get — it is simply much smaller now,
    which is the entire point. Durability is unchanged: when this returns, the
    bytes are on the disk, so `deliver()` can still tell a sender "Mail sent."
    and mean it.
    """
    directory = ensure_dir(directory)
    target = box_path(directory, key)
    return atomic_write_json(target, payload, mode=FILE_MODE)


def remove_box(directory: Path, key: str) -> bool:
    """Delete one box's file. Missing is success — the box is gone either way."""
    try:
        box_path(directory, key).unlink()
        return True
    except FileNotFoundError:
        return False


def read_boxes(directory: Path):
    """Yield ``(key, data)`` for every box file. Boot reads the lot, once.

    ⚠ An unreadable or unparsable box file is SKIPPED with a warning naming the
    FILE — never the key, never a word of content — rather than taken as a
    reason the house cannot open. One corrupt box must not cost everybody else
    their mail, which is exactly what a single-file store risked.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*" + SUFFIX)):
        key = decode_key(path.name[: -len(SUFFIX)])
        if key is None:
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                yield key, json.load(fh)
        except (OSError, ValueError):
            log.warning("skipping unreadable box file %s", path)


def has_boxes(directory: Path) -> bool:
    directory = Path(directory)
    if not directory.is_dir():
        return False
    return any(decode_key(p.name[: -len(SUFFIX)]) is not None
               for p in directory.glob("*" + SUFFIX))


def retire_legacy(path: Path) -> Path:
    """Rename the single-file store out of the way once it has been split.

    ⛔ RENAME, not unlink. The migration reads ciphertext and writes it back
    unchanged, so nothing is lost in principle — but "in principle" is not a
    backup, and the retired file is what an operator would roll back to.

    ⚠ It overwrites an existing `.pre-split`: reaching here means a legacy file
    was present with no split boxes beside it, so the file being retired is the
    newer truth by construction.
    """
    path = Path(path)
    retired = path.with_name(path.name + LEGACY_SUFFIX)
    os.replace(path, retired)
    return retired
