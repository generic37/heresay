# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`forbidden_names.txt` — the operator's own blocklist (v1.44.0).

The engine already reserved the names it CANNOT let anyone take: every rank on
the ladder (a rank word is an identity here) and the arch account's name. This
adds the other kind — the operator's taste and the operator's trouble:
impersonation bait, `admin`/`root`, slurs, a brand they would rather not host.

Three things this file protects, and they are the three that could go wrong:

  * ⛔ ADDITIVE. The file is unioned with the rank names and can never UNRESERVE
    one — an operator editing a text file must not be able to make `staff`
    registrable and hand the next registrant a word the house uses to mean
    something.
  * ⚠ SOFT collision. A forbidden name already held by an account grandfathers
    the holder and logs a COUNT — never a name, and never a rename. That is a
    different rule from the HARD rank↔account exclusivity, which does refuse the
    boot, and confusing the two would either wreck somebody's account or fail to
    open the house over a word in a text file.
  * The refusal says the same sentence for every reason, so nobody can read the
    blocklist back one guess at a time.
"""

import asyncio
from pathlib import Path

import pytest

from heresay import forbidden
from heresay.config import Config
from heresay.server import TalkerServer, reserved_names
from heresay.theme import DEFAULT_THEME as T, Theme

from conftest import seed_account

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"

WARDENS = Theme(order=("citizen", "warden", "Steward"),
                resident="citizen", staff="warden")


def _cfg(tmp_path, **over):
    base = dict(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        owner_name="Generic", house_dir=str(REPO / "tests" / "fixtures" / "house"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        forbidden_names_file=str(tmp_path / "forbidden_names.txt"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000, reload_poll_seconds=0,   # the loop is off; we tick
    )
    base.update(over)
    return Config(**base)


def _path(tmp_path):
    return tmp_path / "forbidden_names.txt"


def _write(tmp_path, body):
    _path(tmp_path).write_text(body, encoding="utf-8")


def _server(tmp_path, **over):
    return TalkerServer(_cfg(tmp_path, **over))


# ---------------------------------------------------------------------------
# 1. The file
# ---------------------------------------------------------------------------

def test_one_name_per_line_lowercased():
    assert forbidden.parse("Admin\nRoot\n") == {"admin", "root"}


def test_comments_blanks_and_whitespace():
    """The dullest possible format, on purpose: a list of words wants to be a
    list of words, editable by somebody who has never seen a config format."""
    text = ("# my list\n"
            "\n"
            "   admin   \n"
            "root  # the obvious one\n"
            "   \n"
            "\tsupport\t\n"
            "#commented-out-name\n")
    assert forbidden.parse(text) == {"admin", "root", "support"}


def test_there_is_no_such_thing_as_a_syntax_error():
    """⚠ Why this source can never reject a reload: every non-comment line is a
    name, so there is nothing to get wrong."""
    assert forbidden.parse("a name with spaces\n[commands]\n= = =\n") == {
        "a name with spaces", "[commands]", "= = ="}


def test_an_absent_file_is_the_empty_list(tmp_path):
    """An operator who never creates one simply has no list of their own —
    exactly what the house did before the file existed."""
    assert not _path(tmp_path).exists()
    assert forbidden.load(_path(tmp_path)) == set()


def test_an_empty_file_is_the_empty_list(tmp_path):
    _write(tmp_path, "")
    assert forbidden.load(_path(tmp_path)) == set()
    _write(tmp_path, "# nothing but a comment\n\n")
    assert forbidden.load(_path(tmp_path)) == set()


def test_an_unreadable_file_degrades_rather_than_crashing(tmp_path, caplog):
    """A blocklist the daemon cannot open must not be the reason a house will
    not start — the same rule as permissions.toml's mode-000 case."""
    import os
    _write(tmp_path, "admin\n")
    os.chmod(_path(tmp_path), 0o000)
    try:
        with caplog.at_level("WARNING"):
            assert forbidden.load(_path(tmp_path)) == set()
        assert "could not read" in caplog.text
    finally:
        os.chmod(_path(tmp_path), 0o644)


def test_the_daemon_never_writes_it(tmp_path):
    """⛔ The permissions.toml rule, and the production incident behind it: /etc
    is mounted read-only for the service."""
    import inspect
    src = inspect.getsource(forbidden)
    assert "write_text" not in src and "open(" not in src
    # ...and a boot with no file leaves none.
    _server(tmp_path)
    assert not _path(tmp_path).exists()


# ---------------------------------------------------------------------------
# 2. Additive — the file ∪ the ranks
# ---------------------------------------------------------------------------

def test_the_rule_is_the_union(tmp_path):
    assert reserved_names(T, {"admin"}) == {"admin"} | {r.lower() for r in T.order}


def test_the_file_cannot_UNRESERVE_a_rank(tmp_path):
    """⛔ The one thing the file may never do. It is a set union; there is no
    syntax for removal and there must not be."""
    _write(tmp_path, "admin\n")
    srv = _server(tmp_path)
    # ⚠ Iterate the ladder the SERVER actually loaded (Commons's shipped
    # resident/staff/owner), not the engine's GENERIC DEFAULT_THEME (T) —
    # since v1.54.0 those differ, and reserved_names keys off srv.theme.
    for rank in srv.theme.order:
        assert rank.lower() in reserved_names(srv.theme, srv.forbidden_names)


def test_commons_still_reserves_keeper_and_caretaker_with_NO_file(tmp_path):
    """⚠ THE CONFIRMATION THE HARDCODE REMOVAL RESTS ON. `staff` and
    `owner` are RANK names on Commons, so the rank rule covers them and
    `RESERVED_NAMES` said nothing the ladder did not."""
    srv = _server(tmp_path)
    assert not _path(tmp_path).exists()
    assert srv.forbidden_names == set()
    r = reserved_names(srv.theme, srv.forbidden_names)
    assert {"staff", "owner", "resident"} <= r


def test_a_house_with_ANOTHER_vocabulary_does_not_inherit_them(tmp_path):
    """...and the other half of the argument: those two words are Commons's,
    not the engine's. A house of citizens and wardens gets them back by asking."""
    assert not ({"staff", "owner"} & reserved_names(WARDENS))
    assert "staff" in reserved_names(WARDENS, {"staff"})


# ---------------------------------------------------------------------------
# 3. Registration
# ---------------------------------------------------------------------------

async def _collect(r, timeout=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _try_name(port, name):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r)
    w.write((name + "\n").encode())
    await w.drain()
    out = await _collect(r, timeout=0.6)
    w.close()
    return out


async def _start(srv):
    await srv.start()
    return srv._servers[0].sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_a_name_in_the_file_is_refused(tmp_path):
    _write(tmp_path, "# no thanks\nadmin\nroot\n")
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        assert "isn't available" in await _try_name(port, "admin")
        assert "isn't available" in await _try_name(port, "root")
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_matching_is_case_insensitive(tmp_path):
    _write(tmp_path, "Admin\n")
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        for typed in ("admin", "ADMIN", "AdMiN"):
            assert "isn't available" in await _try_name(port, typed), typed
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_an_ordinary_name_is_still_offered_an_account(tmp_path):
    _write(tmp_path, "admin\n")
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        out = await _try_name(port, "Corvid")
        assert "isn't available" not in out
        assert "?" in out                       # the create-it offer
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_refusal_says_the_same_thing_for_every_reason(tmp_path):
    """⛔ Different reasons a name is unavailable, ONE sentence — and it never
    says the list exists. A message that distinguished "a rank" from "on the
    blocklist" would let anyone map the reservations one guess at a time.

    ⚠ The arch name is not exercised here: this door is the plaintext console,
    where registering it is deliberately ALLOWED so the operator can bootstrap a
    fresh box. Its refusal (over a network door) carries the same sentence and is
    asserted in test_arch_safety.py."""
    _write(tmp_path, "admin\n")
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        blocked = await _try_name(port, "admin")      # the operator's list
        rank = await _try_name(port, "staff")        # a rank name
        for out in (blocked, rank):
            assert "That name isn't available." in out
            # Nothing about WHY, and nothing that echoes the list back.
            for leak in ("forbidden", "blocklist", "rank", "reserved", "admin"):
                assert leak not in out.lower().replace("that name isn't "
                                                       "available.", "")
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_refusal_is_recorded_as_a_fact(tmp_path):
    """The distinction the SCREEN does not draw, the event log does — which is
    where it belongs."""
    _write(tmp_path, "admin\n")
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        await _try_name(port, "admin")
        rows = srv.events.read(types=("refused",))
        assert rows and rows[-1]["reason"] == "reserved_name"
        # ⛔ Facts, not identities: the refused NAME is not in the record.
        assert "admin" not in str(rows[-1])
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 4. The SOFT collision — grandfather, count only
# ---------------------------------------------------------------------------

def test_an_existing_holder_is_grandfathered_and_the_boot_survives(tmp_path, caplog):
    """⚠ NOT the rank↔account rule. That one refuses the boot, because the house
    cannot mean two things by one word. This one is the operator changing their
    mind about what they want hosted — a decision about the FUTURE — and
    renaming somebody over a line in a text file is not the file's to do."""
    cfg = _cfg(tmp_path)
    srv = _server(tmp_path)                       # make the store, seed it
    seed_account(srv.accounts, "Admin", "correct horse battery")
    seed_account(srv.accounts, "Bob", "correct horse battery")
    _write(tmp_path, "admin\n")

    with caplog.at_level("WARNING"):
        srv2 = TalkerServer(cfg)                  # boots — does NOT raise
    assert srv2.accounts.get("Admin") is not None      # still there
    assert srv2.accounts.get("Admin").name == "Admin"  # and unrenamed
    assert "1 existing account(s) hold a now-forbidden name" in caplog.text
    # ⚠ v1.47.0 unified the wording: the log line now carries the reassuring
    # half too, because after the F6 dedup it IS the line `.load_report` shows.
    assert "grandfathered; nothing was changed" in caplog.text


def test_the_warning_is_a_COUNT_and_leaks_no_NAME(tmp_path, caplog):
    """⛔ Which somebody is a thing to go and look up deliberately, not something
    a log line hands over on the way past. Same posture as `.security`."""
    cfg = _cfg(tmp_path)
    srv = _server(tmp_path)
    for n in ("Admin", "Root", "Support"):
        seed_account(srv.accounts, n, "correct horse battery")
    _write(tmp_path, "admin\nroot\nsupport\nunheld\n")

    with caplog.at_level("WARNING"):
        srv2 = TalkerServer(cfg)
    assert "3 existing account(s)" in caplog.text
    for name in ("Admin", "Root", "Support", "admin", "root", "support"):
        assert name not in caplog.text
    # ...and the same discipline on the screen staff can read.
    said = " ".join(srv2.load_report.warnings)
    assert "3 existing account(s)" in said
    for name in ("Admin", "Root", "Support"):
        assert name not in said


@pytest.mark.asyncio
async def test_a_grandfathered_holder_can_still_log_in(tmp_path):
    """The point of grandfathering: nothing about their account changed."""
    cfg = _cfg(tmp_path)
    srv = _server(tmp_path)
    seed_account(srv.accounts, "Admin", "correct horse battery")
    _write(tmp_path, "admin\n")

    srv2 = TalkerServer(cfg)
    port = await _start(srv2)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await _collect(r)
        w.write(b"Admin\n"); await w.drain()
        await _collect(r)
        w.write(b"correct horse battery\n"); await w.drain()
        assert "atrium" in (await _collect(r, timeout=0.6)).lower()
        w.close()
    finally:
        await srv2.aclose()


def test_nothing_is_grandfathered_when_nothing_collides(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    srv = _server(tmp_path)
    seed_account(srv.accounts, "Corvid", "correct horse battery")
    _write(tmp_path, "admin\n")
    with caplog.at_level("WARNING"):
        srv2 = TalkerServer(cfg)
    assert "grandfathered" not in caplog.text
    assert not any("grandfather" in w for w in srv2.load_report.warnings)


# ---------------------------------------------------------------------------
# 5. Live reload, on the existing spine
# ---------------------------------------------------------------------------

def _tick(srv):
    return srv.reloader.check_once()


def _result(results, source="forbidden_names"):
    return next((r for r in results if r.source == source), None)


def test_an_edit_changes_what_is_blocked_without_a_restart(tmp_path):
    srv = _server(tmp_path)
    assert "admin" not in reserved_names(srv.theme, srv.forbidden_names)

    _write(tmp_path, "admin\n")
    r = _result(_tick(srv))
    assert r is not None and r.ok
    assert "admin" in reserved_names(srv.theme, srv.forbidden_names)


def test_removing_a_name_unblocks_it(tmp_path):
    _write(tmp_path, "admin\nroot\n")
    srv = _server(tmp_path)
    _write(tmp_path, "root\n")
    r = _result(_tick(srv))
    assert r.ok
    assert "admin" not in srv.forbidden_names
    assert "root" in srv.forbidden_names


def test_the_diff_is_COUNTS_and_never_the_names(tmp_path):
    """⛔ This file is a list of words an operator would rather not see. A
    report that recited them back — into the log, or into `.load_report`, which
    staff can read — would be the one place in the house that says them out
    loud."""
    _write(tmp_path, "oldname\n")
    srv = _server(tmp_path)
    _write(tmp_path, "newname\nanother\n")
    r = _result(_tick(srv))
    said = " ".join(r.changes + r.notes + [r.summary()])
    assert "2 name(s) added" in said and "1 name(s) removed" in said
    for leak in ("oldname", "newname", "another"):
        assert leak not in said


def test_the_grandfather_count_is_reported_on_a_LIVE_edit_too(tmp_path):
    """The edit lands on somebody whether it arrives at boot or at 3pm."""
    srv = _server(tmp_path)
    seed_account(srv.accounts, "Admin", "correct horse battery")
    _write(tmp_path, "admin\n")
    r = _result(_tick(srv))
    assert any("1 existing account(s)" in n for n in r.notes)
    assert "Admin" not in " ".join(r.notes)
    assert srv.accounts.get("Admin") is not None          # untouched


def test_deleting_the_file_empties_the_list(tmp_path):
    _write(tmp_path, "admin\n")
    srv = _server(tmp_path)
    assert srv.forbidden_names == {"admin"}
    _path(tmp_path).unlink()
    r = _result(_tick(srv))
    assert r.ok and srv.forbidden_names == set()
    assert any("empty" in n for n in r.notes)


def test_an_unchanged_file_is_not_reloaded(tmp_path):
    _write(tmp_path, "admin\n")
    srv = _server(tmp_path)
    assert _result(_tick(srv)) is None


def test_it_is_its_own_source_on_the_spine(tmp_path):
    """Not folded into "permissions": different file, different decision, and a
    diff that reads quite differently. They only share a directory."""
    from heresay import reload as reload_mod
    names = [s.name for s in reload_mod.SOURCES]
    assert "forbidden_names" in names and "permissions" in names
    srv = _server(tmp_path)
    src = next(s for s in srv.reloader.sources if s.name == "forbidden_names")
    assert src.paths(srv) == [srv.config.forbidden_names_path()]


def test_the_reload_is_reported_through_all_three_channels(tmp_path):
    """The spine's contract: the process log, `.load_report`, and a fact."""
    import heresay.commands as C
    from conftest import FakeSession

    srv = _server(tmp_path)
    _write(tmp_path, "admin\n")
    _tick(srv)

    rows = [e for e in srv.events.read(types=("staff_action",))
            if e["target"] == "forbidden_names"]
    assert rows and rows[-1]["actor"] == "config" and rows[-1]["action"] == "reload"

    s = FakeSession(srv.world, name="Wizzy", level="wizard", config=srv.config,
                    load_report=srv.load_report)
    s.clear()
    C.dispatch(s, ".load_report")
    assert "forbidden_names" in s.text
    assert "admin" not in s.text                  # ...and still no names


@pytest.mark.asyncio
async def test_a_live_edit_blocks_the_very_next_registration(tmp_path):
    """⚡ The whole feature: `_create_account` reads the set at use time, so
    there is nothing to refresh."""
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        assert "isn't available" not in await _try_name(port, "Admin")
        _write(tmp_path, "admin\n")
        _tick(srv)
        assert "isn't available" in await _try_name(port, "Admin")
    finally:
        await srv.aclose()


def test_no_new_admin_command_was_added():
    """⛔ The spine's rule: the WATCHER is the trigger and `.load_report` is the
    readout. An operator who edited a file has already said what they want."""
    from heresay.commands.registry import COMMANDS
    for c in COMMANDS:
        assert "forbid" not in c.name and "reload" not in c.name


# ---------------------------------------------------------------------------
# 6. Casefold hygiene — ONE vocabulary for name-matching (v1.47.0)
# ---------------------------------------------------------------------------
#
# ⚠ Pure hygiene, and it fixes NO bug today. The forbidden and rank sets were
# built with `.lower()` while the registration check has always been
# `.casefold()`. On the strict-ASCII alphabet `valid_name` allows those two are
# identical, so nothing could slip through — but they diverge outside it
# ("Straße".lower() is not "Straße".casefold()), and a later widening of
# `valid_name` would then have opened a bypass in silence. One vocabulary means
# that widening cannot.

def test_lower_and_casefold_agree_across_the_ENTIRE_valid_name_alphabet():
    """⛔ The proof that this change is behaviour-preserving TODAY. Not a sample:
    every character `valid_name` accepts, checked."""
    from heresay.accounts import _NAME_CHARS
    assert all(c.lower() == c.casefold() for c in _NAME_CHARS)


def test_every_registrable_name_folds_identically_either_way():
    """The alphabet result lifted to whole names — the thing the comparison
    actually operates on."""
    import itertools
    from heresay.accounts import _NAME_CHARS, valid_name
    alphabet = "".join(sorted(_NAME_CHARS))
    # Exhaustive over every 2-character name, plus a wide sweep of longer ones.
    for a, b in itertools.product(alphabet, repeat=2):
        n = a + b
        if valid_name(n):
            assert n.lower() == n.casefold(), n
    for n in ("Admin", "ROOT", "sUpPoRt", "a-b_9", "Z" * 16, "staff",
              "owner", "OWNER", "x1", "Q-_9zZ"):
        assert n.lower() == n.casefold(), n


def test_the_sets_are_built_with_CASEFOLD(tmp_path):
    """The change itself, at all three comparison sites."""
    import inspect
    src = inspect.getsource(forbidden)
    assert ".lower()" not in src.replace("lower() is not", "")
    _write(tmp_path, "Admin\nROOT\n")
    assert forbidden.load(_path(tmp_path)) == {"admin", "root"}
    assert forbidden.rank_names(T) == {r.casefold() for r in T.order}


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["admin", "ADMIN", "AdMiN", "aDMIn"])
async def test_ASCII_matching_is_byte_for_byte_what_it_was(tmp_path, typed):
    """The behaviour a resident meets, unchanged."""
    _write(tmp_path, "Admin\n")
    srv = _server(tmp_path)
    port = await _start(srv)
    try:
        assert "isn't available" in await _try_name(port, typed)
    finally:
        await srv.aclose()


def test_the_grandfather_count_still_matches_case_insensitively(tmp_path):
    """`grandfathered` was the third site using `.lower()`; it compares stored
    display names, which carry their original capitalisation."""
    cfg = _cfg(tmp_path)
    srv = _server(tmp_path)
    for n in ("AdMiN", "ROOT"):
        seed_account(srv.accounts, n, "correct horse battery")
    _write(tmp_path, "admin\nroot\n")
    assert forbidden.grandfathered(
        forbidden.load(_path(tmp_path)), srv.accounts) == 2


def test_the_grandfather_warning_appears_ONCE_at_boot(tmp_path, caplog):
    """⛔ F6 of the v1.46 audit. `LoadReport.capturing()` files every
    `log.warning` into the report, so calling `load_report.warn()` as well
    printed the same fact twice on `.load_report`. The capture is enough at
    boot; the explicit call is kept for the LIVE reload, which runs outside it."""
    cfg = _cfg(tmp_path)
    srv = _server(tmp_path)
    seed_account(srv.accounts, "Admin", "correct horse battery")
    _write(tmp_path, "admin\n")
    with caplog.at_level("WARNING"):
        srv2 = TalkerServer(cfg)
    hits = [w for w in srv2.load_report.warnings if "forbidden_names" in w]
    assert len(hits) == 1, hits
    assert "1 existing account(s)" in hits[0]
    assert "Admin" not in hits[0]                 # still a count, never a name
    # ...and the log itself is unchanged: exactly one line.
    assert caplog.text.count("now-forbidden name") == 1


def test_a_LIVE_reload_still_reports_the_grandfather_count(tmp_path):
    """The other half of the dedup: a reload runs outside `capturing()`, so
    there the explicit call is the only thing that lands it."""
    srv = _server(tmp_path)
    seed_account(srv.accounts, "Admin", "correct horse battery")
    before = len([w for w in srv.load_report.warnings if "forbidden_names" in w])
    _write(tmp_path, "admin\n")
    r = next(x for x in srv.reloader.check_once() if x.source == "forbidden_names")
    after = [w for w in srv.load_report.warnings if "forbidden_names" in w]
    assert len(after) == before + 1, after
    assert "1 existing account(s)" in after[-1]
    assert "Admin" not in " ".join(after)
    assert any("1 existing account(s)" in n for n in r.notes)
