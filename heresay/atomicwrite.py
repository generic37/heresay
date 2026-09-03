# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One safe atomic write, shared by every JSON store (CODE_AUDIT.md shortlist #1).

The identical `mkstemp → fdopen → dump → flush → fsync → os.replace`,
`unlink-on-failure` block had been copy-pasted across `bans.py`, `accounts.py`,
`resume.py`, `state.py` and `boxfiles.py`, with a **divergent, un-fsynced**
variant in `events.py`'s trim. This module is that one sequence, in one place.

`atomic_write` is the core (any text); `atomic_write_json` is the wrapper the
stores use. Both guarantee:

- **A reader never sees a half-written file, and a crash leaves the OLD one
  intact.** The bytes go to a temp file IN THE TARGET'S OWN DIRECTORY, then
  `os.replace` swaps it in — atomic only within one filesystem, which is why the
  temp is a sibling of the target and not in `/tmp`.
- **The bytes are never briefly world-readable.** `os.fchmod` sets `mode`
  (default `0o600`) on the fd BEFORE anything is written. `mkstemp` already
  creates at `0600`; this makes it explicit and lets a caller ask for another.
- **"It returned" means "it is on the disk"** — `fsync` before the rename, by
  default. That is the property `MailStore.deliver()` leans on to tell a sender
  "Mail sent." and mean it. A caller on a hot, loss-tolerant path can pass
  `fsync=False`; nothing in the tree does today.
- **A failed write is a no-op.** Any exception removes the temp and re-raises, so
  the target is left exactly as it was.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def atomic_write(path, data: str, *, mode: int = 0o600, fsync: bool = True) -> Path:
    """Write ``data`` to ``path`` atomically. See the module note for the four
    guarantees. Returns the target path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def atomic_write_json(path, payload, *, mode: int = 0o600, fsync: bool = True,
                      indent: int = 2) -> Path:
    """``atomic_write`` of ``payload`` as pretty JSON with a trailing newline —
    the exact serialization every store used (``indent=2``, ``ensure_ascii=False``,
    a final ``\\n``)."""
    text = json.dumps(payload, indent=indent, ensure_ascii=False) + "\n"
    return atomic_write(path, text, mode=mode, fsync=fsync)
