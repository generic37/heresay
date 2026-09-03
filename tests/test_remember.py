# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Remember-me and resume tokens — the silent web login.

The shape of the thing, and what these tests are defending:

  * **Opt-in, off by default.** Nobody is remembered who did not say so. That
    informed opt-in is what let `agreement.txt` stay byte-identical (its own test
    still asserts that), so "the setting defaults off" is a consent property, not
    a UX preference.
  * **Web-only by construction.** A socket door cannot present a token and is
    never issued one. Not an `if` at a call site — the transport simply has no
    control convention.
  * **Hashed at rest.** What lands in `resume.json`, and therefore in every
    backup tarball, is a hash. A stolen file is inert.
  * **Rotates on use, revoked on anything that smells like losing control** of
    the account: all three password-change paths, `.quit` (a full account
    logout), `.remember off`, deletion.
  * **A resume is a LOGIN**, not a grace reattach: bans and private mode apply.
"""

import asyncio
from pathlib import Path

import pytest

from conftest import FakeSession, seed_account
from heresay import resume as resume_mod
from heresay.accounts import AccountStore
from heresay.commands import REMEMBER_SETTING, dispatch, remembered
from heresay.config import Config
from heresay.resume import (MAX_TOKENS_PER_ACCOUNT, ResumeStore, new_token,
                            token_hash)
from heresay.transport import (CTRL_RESUME, SocketTransport, parse_resume_frame)
from heresay.world import load_world

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    return ResumeStore(tmp_path / "resume.json", wall_clock=Clock())


# ---------------------------------------------------------------------------
# The token itself
# ---------------------------------------------------------------------------

def test_a_token_is_high_entropy_and_never_repeats():
    seen = {new_token() for _ in range(200)}
    assert len(seen) == 200
    # token_urlsafe(32) -> 43 chars; well over the 256 bits the design asks for.
    assert all(len(t) >= 43 for t in seen)


def test_a_token_is_not_derived_from_anything():
    """Two tokens for the same account share nothing. (A token derived from the
    password would be a password equivalent, which is the whole thing to avoid.)"""
    a, b = new_token(), new_token()
    assert a != b


def test_the_raw_token_never_reaches_the_disk(tmp_path):
    """The property that makes a stolen backup inert rather than a set of live
    bearer credentials."""
    s = ResumeStore(tmp_path / "resume.json", wall_clock=Clock())
    token = s.issue("Corvid")
    raw = (tmp_path / "resume.json").read_text()
    assert token not in raw
    assert token_hash(token) in raw


def test_the_file_records_only_account_and_timestamps(tmp_path):
    """No content, ever — this is auth data. Assert the shape, not just the
    absence of a token."""
    import json
    s = ResumeStore(tmp_path / "resume.json", wall_clock=Clock())
    s.issue("Corvid")
    data = json.loads((tmp_path / "resume.json").read_text())
    rec = next(iter(data["tokens"].values()))
    assert set(rec) == {"account", "issued", "expires"}
    assert rec["account"] == "corvid"          # casefolded, like every store


# ---------------------------------------------------------------------------
# Issue / resolve / rotate / expire / revoke
# ---------------------------------------------------------------------------

def test_an_issued_token_resolves_to_its_account(store):
    token = store.issue("Corvid")
    assert store.resolve(token) == "corvid"


def test_an_unknown_token_resolves_to_nothing(store):
    store.issue("Corvid")
    assert store.resolve(new_token()) is None


@pytest.mark.parametrize("junk", ["", "   ", "x" * 5000, "\x00", "not-a-token"])
def test_junk_resolves_to_nothing_and_never_raises(store, junk):
    assert store.resolve(junk) is None


def test_rotation_kills_the_presented_token(store):
    old = store.issue("Corvid")
    fresh = store.rotate(old)
    assert fresh is not None and fresh != old
    assert store.resolve(old) is None, "a used token must not survive its use"
    assert store.resolve(fresh) == "corvid"


def test_rotation_leaves_exactly_one_token_behind(store):
    old = store.issue("Corvid")
    store.rotate(old)
    assert store.count_for("Corvid") == 1, "no window where both or neither work"


def test_rotating_an_invalid_token_changes_nothing(store):
    keep = store.issue("Corvid")
    assert store.rotate(new_token()) is None
    assert store.resolve(keep) == "corvid"


def test_a_token_expires_after_thirty_days(tmp_path):
    clock = Clock()
    s = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    token = s.issue("Corvid")
    clock.t += resume_mod.RESUME_TOKEN_DAYS * 86400 - 10
    assert s.resolve(token) == "corvid"          # still just inside
    clock.t += 20
    assert s.resolve(token) is None


def test_rotation_restarts_the_thirty_day_clock(tmp_path):
    clock = Clock()
    s = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    token = s.issue("Corvid")
    clock.t += 29 * 86400
    fresh = s.rotate(token)
    clock.t += 29 * 86400                        # 58 days after issue
    assert s.resolve(fresh) == "corvid", "use refreshes the lifetime"


def test_expired_tokens_are_purged_from_disk(tmp_path):
    clock = Clock()
    s = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    s.issue("Corvid")
    clock.t += 40 * 86400
    assert s.purge_expired() == 1
    assert s.count_for("Corvid") == 0


def test_expired_tokens_do_not_survive_a_restart(tmp_path):
    clock = Clock()
    s = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    s.issue("Corvid")
    clock.t += 40 * 86400
    again = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    assert again.count_for("Corvid") == 0        # purged on load


def test_tokens_do_survive_a_restart(tmp_path):
    """The point of storing them server-side at all."""
    clock = Clock()
    s = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    token = s.issue("Corvid")
    again = ResumeStore(tmp_path / "resume.json", wall_clock=clock)
    assert again.resolve(token) == "corvid"


def test_revoke_account_drops_every_device(store):
    tokens = [store.issue("Corvid") for _ in range(3)]
    store.issue("Wren")
    assert store.revoke_account("CORVID") == 3   # casefolded
    assert all(store.resolve(t) is None for t in tokens)
    assert store.count_for("Wren") == 1, "one person's revoke is not everyone's"


# ---------------------------------------------------------------------------
# ⛔ The per-account cap (v1.52.2, external review)
# ---------------------------------------------------------------------------

def test_an_account_holds_at_most_MAX_TOKENS_and_keeps_the_NEWEST(store):
    """⛔ `issue()` minted without ever counting, so `resume.json` grew one
    record per remembered login for thirty days.

    ⚠ Not an attack — minting one needs the password — just growth nothing
    bounded. Which is reason enough: a file that only ever gets longer is one
    nobody can reason about.

    Eviction is oldest-`issued` first, so the device you signed in on most
    recently is the last to go — the order a person would choose if asked.
    """
    n = MAX_TOKENS_PER_ACCOUNT + 5
    tokens = [store.issue("Corvid", now=1_000.0 + i) for i in range(n)]

    assert store.count_for("Corvid") == MAX_TOKENS_PER_ACCOUNT
    # The newest MAX_TOKENS survive...
    for t in tokens[-MAX_TOKENS_PER_ACCOUNT:]:
        assert store.resolve(t) == "corvid"
    # ...and every older one is gone.
    for t in tokens[:-MAX_TOKENS_PER_ACCOUNT]:
        assert store.resolve(t) is None


def test_the_cap_is_exact_at_the_boundary(store):
    """One over the cap evicts exactly one — not a batch, and not none."""
    tokens = [store.issue("Corvid", now=1_000.0 + i)
              for i in range(MAX_TOKENS_PER_ACCOUNT)]
    assert store.count_for("Corvid") == MAX_TOKENS_PER_ACCOUNT
    assert all(store.resolve(t) == "corvid" for t in tokens)

    extra = store.issue("Corvid", now=2_000.0)
    assert store.count_for("Corvid") == MAX_TOKENS_PER_ACCOUNT
    assert store.resolve(tokens[0]) is None        # the oldest, and only it
    assert store.resolve(tokens[1]) == "corvid"
    assert store.resolve(extra) == "corvid"


def test_the_cap_is_PER_ACCOUNT_and_never_touches_anybody_else(store):
    """⚠ The one that matters most: a busy account must not be able to sign
    somebody else out. The eviction is filtered on the account key, and this is
    what says so."""
    wren = [store.issue("Wren", now=1.0 + i) for i in range(3)]
    for i in range(MAX_TOKENS_PER_ACCOUNT + 10):
        store.issue("Corvid", now=1_000.0 + i)

    assert store.count_for("Corvid") == MAX_TOKENS_PER_ACCOUNT
    assert store.count_for("Wren") == 3
    for t in wren:
        assert store.resolve(t) == "wren", "another account's login evicted Wren"


def test_the_cap_survives_a_restart(store, tmp_path):
    """The eviction is written, not merely applied in memory — one `_save()`
    covers the insert and the eviction together, so a file that had dropped one
    and kept the other could not exist."""
    for i in range(MAX_TOKENS_PER_ACCOUNT + 5):
        store.issue("Corvid", now=1_000.0 + i)
    reopened = ResumeStore(tmp_path / "resume.json", wall_clock=Clock())
    assert reopened.count_for("Corvid") == MAX_TOKENS_PER_ACCOUNT


def test_rotation_cannot_grow_the_count_so_it_needs_no_cap(store):
    """⚠ Why `rotate()` was deliberately left alone: it deletes one and adds
    one, so an account that rotates for a decade holds exactly what it held
    before. Only a fresh `issue()` adds a device."""
    tokens = [store.issue("Corvid", now=1_000.0 + i) for i in range(5)]
    current = tokens[-1]
    for i in range(50):
        current = store.rotate(current, now=2_000.0 + i)
        assert current is not None
    assert store.count_for("Corvid") == 5


def test_revoke_account_is_safe_for_someone_who_never_had_one(store):
    assert store.revoke_account("Nobody") == 0


def test_a_corrupt_token_file_does_not_stop_the_house_opening(tmp_path):
    """Worst case must be "everyone types a password once", never "no server"."""
    p = tmp_path / "resume.json"
    p.write_text("{ this is not json")
    s = ResumeStore(p, wall_clock=Clock())
    assert s.resolve(new_token()) is None
    assert s.issue("Corvid")                     # and it recovers on the next write


# ---------------------------------------------------------------------------
# The wire format
# ---------------------------------------------------------------------------

def test_a_resume_frame_parses():
    assert parse_resume_frame(CTRL_RESUME + " abc123") == "abc123"


def test_a_bare_resume_frame_means_i_have_no_token():
    """Distinct from "not a resume frame": it is how a client says its opening
    hello is finished, which is what lets the server stop waiting."""
    assert parse_resume_frame(CTRL_RESUME) == ""


def test_a_non_resume_frame_is_not_one():
    assert parse_resume_frame("\x01width 40") is None
    assert parse_resume_frame("\x01resumexyz") is None
    assert parse_resume_frame("resume abc") is None


def test_an_over_long_resume_frame_is_refused_without_hashing():
    assert parse_resume_frame(CTRL_RESUME + " " + "x" * 5000) is None


# ---------------------------------------------------------------------------
# Web-only, by construction
# ---------------------------------------------------------------------------

def test_a_socket_transport_can_never_present_a_token():
    assert SocketTransport.pending_resume is None
    assert SocketTransport.supports_control_frames is False


@pytest.mark.asyncio
async def test_a_socket_door_drops_a_resume_frame_rather_than_speaking_it():
    reader = asyncio.StreamReader()
    reader.feed_data((CTRL_RESUME + " stolen-token\nhello\n").encode())
    reader.feed_eof()

    class _W:
        def is_closing(self): return False
        def write(self, b): pass
        def close(self): pass

    tr = SocketTransport(reader, _W(), "127.0.0.1")
    assert await tr.recv_line() == "hello"
    assert tr.pending_resume is None


def test_a_socket_session_is_never_pushed_a_token(world):
    """`send_control` no-ops without a control convention, so a terminal never
    sees the frame — but the token must not be MINTED either."""
    sess = FakeSession(world, supports_control_frames=False)
    sess.send_control_resume("a-token")
    assert sess.controls == []


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------

def test_remember_defaults_off(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "a-good-passphrase-here")
    assert remembered(store.get("Corvid")) is False


def test_remember_on_sets_the_account_setting(world, tmp_path, store):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Tester", "a-good-passphrase-here")
    sess = FakeSession(world, name="Tester", accounts=accts, resume=store)
    dispatch(sess, ".remember on")
    assert remembered(accts.get("Tester")) is True


def test_remember_off_revokes_every_token(world, tmp_path, store):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Tester", "a-good-passphrase-here")
    accts.set_setting("Tester", REMEMBER_SETTING, True)
    tokens = [store.issue("Tester") for _ in range(2)]
    sess = FakeSession(world, name="Tester", accounts=accts, resume=store)
    dispatch(sess, ".remember off")
    assert remembered(accts.get("Tester")) is False
    assert all(store.resolve(t) is None for t in tokens), \
        "a setting that says no while devices stay signed in would be a lie"


def test_remember_off_tells_this_browser_to_drop_its_copy(world, tmp_path, store):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Tester", "a-good-passphrase-here")
    sess = FakeSession(world, name="Tester", accounts=accts, resume=store)
    dispatch(sess, ".remember off")
    assert CTRL_RESUME in sess.controls, sess.controls


def test_bare_remember_reports_without_changing_anything(world, tmp_path, store):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Tester", "a-good-passphrase-here")
    sess = FakeSession(world, name="Tester", accounts=accts, resume=store)
    dispatch(sess, ".remember")
    assert "off" in sess.text
    assert remembered(accts.get("Tester")) is False


def test_a_typo_is_refused_with_the_usage_line(world, tmp_path, store):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Tester", "a-good-passphrase-here")
    accts.set_setting("Tester", REMEMBER_SETTING, True)
    sess = FakeSession(world, name="Tester", accounts=accts, resume=store)
    dispatch(sess, ".remember yes")           # not on|off
    assert "Usage" in sess.text
    assert remembered(accts.get("Tester")) is True, "a typo changes nothing"


# ---------------------------------------------------------------------------
# Revocation on password change — the hook, on all three paths
# ---------------------------------------------------------------------------

def test_changing_a_password_signs_out_every_device(tmp_path):
    """Hooked in AccountStore, not at the call sites, so no path can forget."""
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Corvid", "a-good-passphrase-here")
    store = ResumeStore(tmp_path / "resume.json", wall_clock=Clock())
    accts.on_password_change = store.revoke_account
    tokens = [store.issue("Corvid") for _ in range(2)]
    accts.change_password("Corvid", "another-good-passphrase")
    assert all(store.resolve(t) is None for t in tokens)


@pytest.mark.asyncio
async def test_the_async_password_change_revokes_too(tmp_path):
    """`.passwd`, `.resetpassword` and the recovery reset all use the async one."""
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Corvid", "a-good-passphrase-here")
    store = ResumeStore(tmp_path / "resume.json", wall_clock=Clock())
    accts.on_password_change = store.revoke_account
    token = store.issue("Corvid")
    await accts.achange_password("Corvid", "another-good-passphrase")
    assert store.resolve(token) is None


def test_a_password_change_for_one_account_spares_the_others(tmp_path):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Corvid", "a-good-passphrase-here")
    accts.create("Wren", "a-good-passphrase-here")
    store = ResumeStore(tmp_path / "resume.json", wall_clock=Clock())
    accts.on_password_change = store.revoke_account
    keep = store.issue("Wren")
    accts.change_password("Corvid", "another-good-passphrase")
    assert store.resolve(keep) == "wren"


def test_the_hook_is_optional(tmp_path):
    """A store with no hook (every unit test in the suite) must not blow up."""
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Corvid", "a-good-passphrase-here")
    accts.change_password("Corvid", "another-good-passphrase")   # no hook set


# ---------------------------------------------------------------------------
# The grace window (raised to 120s in this release)
# ---------------------------------------------------------------------------

def test_the_grace_default_is_two_minutes():
    assert Config().disconnect_grace_seconds == 120.0


# ---------------------------------------------------------------------------
# `.forget` is gone (v1.24.1)
#
# It existed for one release and never shipped. `.quit` being a full account
# logout made it redundant, and a second command that does most of what `.quit`
# does is a thing to explain rather than a thing to have.
# ---------------------------------------------------------------------------

def test_forget_is_not_a_command():
    from heresay import commands
    assert "forget" not in commands._COMMANDS
    assert not hasattr(commands, "cmd_forget")


def test_forget_has_no_help_page():
    assert not (Path(__file__).resolve().parent.parent
                / "house" / "help" / "forget.txt").exists()


def test_typing_forget_is_an_ordinary_unknown_command(world, tmp_path, store):
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Tester", "a-good-passphrase-here")
    sess = FakeSession(world, name="Tester", accounts=accts, resume=store)
    dispatch(sess, ".forget")
    assert "Unknown command" in sess.text


def test_the_remember_page_points_at_quit_not_forget():
    page = (Path(__file__).resolve().parent.parent
            / "house" / "help" / "remember.txt").read_text()
    assert ".quit" in page
    assert ".forget" not in page
