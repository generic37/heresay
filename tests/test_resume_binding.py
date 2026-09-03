# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""C4 (audit-3) — a resume token is single-use: consumed on every resume, so a
lifted copy replayed from anywhere is refused.

The store rotates on every use (`rotate` deletes the presented hash and issues a
fresh one in one write) and the server's `_try_resume` calls it on every resume,
so a token is a bearer credential for exactly ONE use. The good path — a genuine
reconnect, possibly from a new IP (a phone that changed networks) — keeps working,
because the binding is single-use rather than IP-scoped. The bad path — the same
token presented a second time, which is what a lifted/replayed copy looks like —
resolves to nothing.

⚠ Binding is deliberately single-use and NOT IP-scoped: remember-me exists for
the phone that backgrounds a tab and comes back on a different network, so an
IP-tight binding would log out the very users it is for. See resume.py.
"""
from heresay.resume import ResumeStore


def test_a_fresh_token_resolves_to_its_account(tmp_path):
    r = ResumeStore(tmp_path / "resume.json")
    tok = r.issue("Wren")
    assert r.resolve(tok) == "wren"


def test_the_good_path_a_reconnect_rotates_and_keeps_working(tmp_path):
    r = ResumeStore(tmp_path / "resume.json")
    tok = r.issue("Wren")
    fresh = r.rotate(tok)                 # the browser resumes; server rotates
    assert fresh and fresh != tok
    assert r.resolve(fresh) == "wren"     # the NEW token is what the browser now holds


def test_the_bad_path_a_consumed_token_is_refused_from_anywhere(tmp_path):
    r = ResumeStore(tmp_path / "resume.json")
    tok = r.issue("Wren")
    assert r.rotate(tok)                  # used once (the legitimate owner)
    # A lifted copy of the ORIGINAL token, replayed from anywhere, is now dead —
    # no IP needed to refuse it; single-use is the whole defence.
    assert r.resolve(tok) is None
    assert r.rotate(tok) is None          # and it cannot be rotated either


def test_a_second_resume_of_the_same_token_cannot_double_spend(tmp_path):
    r = ResumeStore(tmp_path / "resume.json")
    tok = r.issue("Wren")
    first = r.rotate(tok)
    second = r.rotate(tok)               # the thief presenting the same lifted token
    assert first is not None
    assert second is None               # exactly one of the two presentations wins


def test_rotation_survives_a_reload_from_disk(tmp_path):
    # The consumed token stays dead across a restart — the deletion is persisted,
    # not just in memory.
    p = tmp_path / "resume.json"
    r = ResumeStore(p)
    tok = r.issue("Wren")
    fresh = r.rotate(tok)
    r2 = ResumeStore(p)
    assert r2.resolve(tok) is None
    assert r2.resolve(fresh) == "wren"
