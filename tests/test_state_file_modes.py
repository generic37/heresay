# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""F4 (audit-3) — every state file is 0600 by the CODE, not by the systemd umask.

The live box gets 0600 only because the unit sets `UMask=0077`; a house run any
other way (a hand launch, a test harness, a different unit) would otherwise write
these world-readable. Since v1.53.x every store sets the mode EXPLICITLY at its
write path (the atomic-write helper's `mode=0o600` default, `os.open(..., 0o600)`
for `mail.key`, an explicit `chmod` in the ban history). This test walks the
state dir after a write to each store and fails on anything group/world-readable
— so the guarantee travels with the code.

⚠ Under a normal pytest umask this test would FAIL if any store relied on the
inherited mode, which is exactly the point of pinning it.
"""
import os
import stat
from pathlib import Path

from heresay.accounts import AccountStore
from heresay.banhistory import BanHistoryStore
from heresay.bans import BanStore
from heresay.events import EventStore
from heresay.mail import MailStore
from heresay.resume import ResumeStore
from heresay.revtell import RevTellStore
from heresay.state import StateStore
from heresay.theme import DEFAULT_THEME


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_every_state_file_is_written_0600(tmp_path):
    # Exercise a write on each store, then stat the file it produced.
    written: list[Path] = []

    acc = AccountStore(tmp_path / "accounts.json", ladder=DEFAULT_THEME,
                       owner_name="Owner")
    acc.create("Owner", "pw-owner-123")
    written.append(tmp_path / "accounts.json")

    bans = BanStore(tmp_path / "bans.json")
    bans.ban("Mallory", "name", 1.0)
    written.append(tmp_path / "bans.json")

    hist = BanHistoryStore(tmp_path / "ban_history.json")
    hist.record("203.0.113.9", "Mallory", "Owner", 1.0)
    written.append(tmp_path / "ban_history.json")

    st = StateStore(tmp_path / "state.json")
    st.set_new_accounts(False)
    written.append(tmp_path / "state.json")

    res = ResumeStore(tmp_path / "resume.json")
    res.issue("Owner")
    written.append(tmp_path / "resume.json")

    ev = EventStore(tmp_path / "events.jsonl")
    ev.record("connection", dir="open", ip="203.0.113.9")
    written.append(tmp_path / "events.jsonl")

    mail = MailStore(tmp_path / "mailboxes.json", tmp_path / "mail.key")
    mail.deliver("Owner", "Owner", "a note to self")
    written.append(tmp_path / "mail.key")

    rev = RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key")
    rev.record("Owner", ">> Owner says: hi")

    for p in written:
        assert p.exists(), p
        assert _mode(p) == 0o600, f"{p} is {oct(_mode(p))}, expected 0600"

    # The per-box stores are directories of 0600 files under a 0700 dir.
    for d in (tmp_path / "mailboxes.json.d", tmp_path / "revtells.json.d"):
        assert d.is_dir(), d
        assert _mode(d) == 0o700, f"{d} is {oct(_mode(d))}, expected 0700"
        boxes = list(d.glob("*.json"))
        assert boxes, f"no box files under {d}"
        for b in boxes:
            assert _mode(b) == 0o600, f"{b} is {oct(_mode(b))}, expected 0600"
