# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Ban refinement (v1.33.0): names out, addresses in — and one security fix.

⛔ **THE RULE THIS FILE EXISTS TO PROTECT: no command output ever prints an IP.**
Not to a staff, not to the arch. The engine resolves, stores and matches
addresses internally; the raw value lives in `bans.json` and `ban_history.json`
and surfaces only when an operator deliberately opens one during an audit. A
grep test at the bottom drives every ban command and asserts the address appears
in none of it.

That rule is what let `.ban_ip` come down from arch to the ordinary moderation
tier: it was arch-only because it could expose an address, and it no longer can.
"""

import asyncio
import json
from pathlib import Path

import pytest

import heresay.commands as C
from heresay.banhistory import BanHistoryStore
from heresay.commands import dispatch
from heresay.config import Config
from heresay.events import EventStore
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
IP = "203.0.113.42"          # the canary: must never reach a screen


def _cfg(tmp_path, **over):
    base = dict(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        events_file=str(tmp_path / "events.jsonl"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        owner_name="Generic",
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
    )
    base.update(over)
    return Config(**base)


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def collect(r, timeout=0.3):
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


async def send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _login(port, name, pw="correct horse battery"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name)
    await collect(r); await send(w, pw)
    await collect(r, timeout=0.5)
    return r, w


def _staff(world, clock, srv, name="Wizzy", **kw):
    """A staff session wired to a real server's stores."""
    return FakeSession(world, name=name, clock=clock, level="wizard",
                       bans=srv.bans, events=srv.events,
                       ban_history=srv.ban_history, accounts=srv.accounts,
                       config=srv.config, wall_clock=lambda: 1_000_000.0, **kw)


# ---------------------------------------------------------------------------
# Part 1 — the security fix
# ---------------------------------------------------------------------------

def _arch(world, clock, srv, **kw):
    return FakeSession(world, name="Wizzy", clock=clock, level="arch",
                       bans=srv.bans, events=srv.events,
                       ban_history=srv.ban_history, accounts=srv.accounts,
                       config=srv.config, wall_clock=lambda: 1_000_000.0, **kw)


def test_resetpassword_refuses_the_arch(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    seed_account(srv.accounts, "Generic", "pw-generic")
    s = _arch(world, clock, srv)
    s.clear()
    assert dispatch(s, ".resetpassword Generic") is None
    assert "can't reset" in s.text.lower()
    assert s.pending_target is None                # nothing was staged


def test_a_keeper_holding_resetpassword_still_cannot_touch_the_arch(
        world, clock, tmp_path):
    """⛔ THE HOLE, exactly as it would have been exploited. The command is
    RANK-ASSIGNABLE and the gate is the CALLER's own password — so a house that
    hands `.resetpassword` to its staff was handing them the arch account,
    reachable with their own staff password. The guard is what closes it; the
    rank is not."""
    from heresay.state import StateStore
    from heresay.commands.registry import command_seed
    st = StateStore(tmp_path / "cmdstate.json", command_seed=command_seed())
    st.commands["resetpassword"] = "staff"        # the house reassigns it
    srv = TalkerServer(_cfg(tmp_path))
    seed_account(srv.accounts, "Generic", "pw-generic")

    staff = _staff(world, clock, srv, state=st)
    staff.clear()
    assert dispatch(staff, ".resetpassword Generic") is None
    assert "Unknown command" not in staff.text   # they really can run it...
    assert "can't reset" in staff.text.lower()   # ...and are still refused
    assert staff.pending_target is None


def test_deleteuser_still_refuses_the_arch(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    seed_account(srv.accounts, "Generic", "pw-generic")
    s = _arch(world, clock, srv)
    s.clear()
    assert dispatch(s, ".deleteuser Generic") is None
    assert "can't delete" in s.text.lower()


def test_resetpassword_still_works_on_anyone_else(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    seed_account(srv.accounts, "Bob", "pw-bob")
    s = _arch(world, clock, srv)
    assert dispatch(s, ".resetpassword Bob") == C.RESETPASSWORD
    assert s.pending_target == "Bob"


def test_the_docstrings_match_the_code():
    """They said "the OWNER's OWN password"; the code checks the CALLER's, which
    is the more robust behaviour and the one that survives reassignment."""
    import inspect
    from heresay.server import TalkerServer as T
    for fn in (T._deleteuser, T._resetpassword):
        doc = inspect.getdoc(fn)
        assert "CALLER's OWN password" in doc, fn.__name__
        assert "OWNER's OWN password" not in doc, fn.__name__
        # ...and the check really is the caller's.
        assert "session.resident.name" in inspect.getsource(fn)


# ---------------------------------------------------------------------------
# The history store
# ---------------------------------------------------------------------------

def test_history_records_every_ban(tmp_path):
    h = BanHistoryStore(tmp_path / "h.json", wall_clock=lambda: 1000.0)
    h.record(IP, "Bob", "Wizzy", 24, "flooding")
    h.record(IP, "Carol", "Wizzy", 12, "")
    assert h.count() == 2
    e = h.for_ip(IP)[0]
    assert (e["account"], e["actor"], e["reason"]) == ("Bob", "Wizzy", "flooding")
    assert e["expires_at"] == 1000.0 + 24 * 3600
    assert e["lifted_at"] is None


def test_history_survives_a_restart_and_is_owner_only(tmp_path):
    import os
    p = tmp_path / "h.json"
    BanHistoryStore(p, wall_clock=lambda: 1000.0).record(IP, "Bob", "Wizzy", 24)
    assert (os.stat(p).st_mode & 0o777) == 0o600
    assert BanHistoryStore(p, wall_clock=lambda: 1000.0).count() == 1


def test_history_outlives_the_ban_itself(tmp_path):
    """THE point of a second store. `bans.json` drops an expired ban — holding
    it would be holding a punishment that has ended — so it cannot answer "has
    this happened before". This can."""
    now = [1000.0]
    h = BanHistoryStore(tmp_path / "h.json", wall_clock=lambda: now[0])
    h.record(IP, "Bob", "Wizzy", 1)
    now[0] += 10 * 3600                       # long expired
    assert len(h.for_ip(IP)) == 1


def test_mark_lifted_stamps_only_the_open_ones(tmp_path):
    now = [1000.0]
    h = BanHistoryStore(tmp_path / "h.json", wall_clock=lambda: now[0])
    h.record(IP, "Bob", "Wizzy", 1)           # will expire
    now[0] += 10 * 3600
    h.record(IP, "Carol", "Wizzy", 24)        # still open
    assert h.mark_lifted(IP) == 1             # only the open one
    entries = h.for_ip(IP)
    assert entries[0]["lifted_at"] is None    # the expired one is untouched
    assert entries[1]["lifted_at"] == now[0]


def test_retention_defaults_to_forever(tmp_path):
    """0 = keep everything, and it is the shipped default — the OPPOSITE of the
    event log's, because this store's value is in the years."""
    assert Config().ban_history_days == 0.0
    now = [1000.0]
    h = BanHistoryStore(tmp_path / "h.json", retention_days=0,
                        wall_clock=lambda: now[0])
    h.record(IP, "Bob", "Wizzy", 1)
    now[0] += 3650 * 86400                    # ten years on
    assert h.purge() == 0 and h.count() == 1


def test_a_finite_retention_window_is_honoured(tmp_path):
    now = [1000.0]
    h = BanHistoryStore(tmp_path / "h.json", retention_days=30,
                        wall_clock=lambda: now[0])
    h.record(IP, "Old", "Wizzy", 1)
    now[0] += 40 * 86400
    h.record(IP, "New", "Wizzy", 1)           # purges on write
    assert [e["account"] for e in h.for_ip(IP)] == ["New"]


def test_a_corrupt_history_does_not_stop_the_house(tmp_path, caplog):
    p = tmp_path / "h.json"
    p.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        h = BanHistoryStore(p)
    assert h.count() == 0
    assert "ban history" in caplog.text


# ---------------------------------------------------------------------------
# Recidivism — matched on the address, never showing it
# ---------------------------------------------------------------------------

def test_no_warning_the_first_time(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    assert C.ban_ip_recidivism(_staff(world, clock, srv), IP) is None


def test_it_warns_with_the_count_and_the_last_details(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    srv.ban_history.record(IP, "Bob", "Wizzy", 24, "flooding")
    srv.ban_history.record(IP, "Carol", "Esoteric", 24, "abuse")
    line = C.ban_ip_recidivism(_staff(world, clock, srv), IP)
    assert "banned 2 time(s) before" in line
    assert "by Esoteric" in line and "abuse" in line
    assert IP not in line                     # ...and never the address


def test_it_catches_the_same_address_under_a_new_name(world, clock, tmp_path):
    """⚠ THE reason the history keeps an address at all. Matched name-to-name
    this would find nothing: the returning attacker registered something new."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.ban_history.record(IP, "TroubleMaker", "Wizzy", 24, "flooding")
    line = C.ban_ip_recidivism(_staff(world, clock, srv), IP)
    assert line is not None and "1 time(s)" in line
    # A different address is a different story.
    assert C.ban_ip_recidivism(_staff(world, clock, srv), "198.51.100.1") is None


@pytest.mark.asyncio
async def test_the_warning_appears_with_the_confirm(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        srv.events.record("login", account="Bob", ip=IP)
        srv.ban_history.record(IP, "Older", "Wizzy", 24, "flooding")

        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Bob")
        shown = await collect(ra, timeout=0.5)
        assert "banned 1 time(s) before" in shown
        assert IP not in shown
        await send(wa, "n")
        await collect(ra, timeout=0.5)
        wa.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 2g — every attempt is audited, confirmed or cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("answer,outcome,banned", [
    ("y", "confirmed", True),
    ("n", "cancelled", False),
    ("", "cancelled", False),
])
async def test_every_attempt_records_a_wizard_action(tmp_path, answer,
                                                     outcome, banned):
    """Running `.ban_ip` is itself the notable act — it is the moment a staff
    member is shown who shares an address — so the ATTEMPT is audited even when
    it ends in nothing."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        srv.events.record("login", account="Bob", ip=IP)

        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Bob")
        await collect(ra, timeout=0.5)
        await send(wa, answer)
        await collect(ra, timeout=0.5)
        wa.close()

        acts = srv.events.read(types=("staff_action",))
        acts = [a for a in acts if a["action"].startswith("ban_ip")]
        assert len(acts) == 1
        assert acts[0]["outcome"] == outcome
        assert acts[0]["target"] == "Bob"          # the NAME, never the address
        assert IP not in json.dumps(acts[0])

        # Confirmed writes the ban AND the history; cancelled writes neither.
        assert srv.bans.is_banned_ip(IP) is banned
        assert (srv.ban_history.count() == 1) is banned
    finally:
        await srv.aclose()


def test_only_ban_ip_carries_an_outcome(world, clock, tmp_path):
    """The field exists for actions that can end in nothing. `.ban` acts on
    execution, so it has no outcome to report and does not invent one."""
    srv = TalkerServer(_cfg(tmp_path))
    staff = _staff(world, clock, srv)
    dispatch(staff, ".ban Bob")
    acts = srv.events.read(types=("staff_action",))
    assert acts and all("outcome" not in a for a in acts)


# ---------------------------------------------------------------------------
# 2c / 2d — `.unban_ip` and `.bans`, both by name
# ---------------------------------------------------------------------------

def test_unban_ip_lifts_by_name(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    seed_account(srv.accounts, "Bob", "pw")
    srv.events.record("login", account="Bob", ip=IP)
    srv.bans.ban(IP, "ip", hours=24, now=1_000_000.0)
    srv.ban_history.record(IP, "Bob", "Wizzy", 24)

    staff = _staff(world, clock, srv)
    staff.clear()
    dispatch(staff, ".unban_ip Bob")
    assert not srv.bans.is_banned_ip(IP, now=1_000_000.0)
    assert "Lifted the IP ban on Bob's address" in staff.text
    assert IP not in staff.text
    # The history KEEPS the record, stamped — recidivism must survive a lift.
    assert srv.ban_history.count() == 1
    assert srv.ban_history.for_ip(IP)[0]["lifted_at"] is not None


def test_unban_ip_says_so_plainly_when_there_is_nothing_to_lift(world, clock,
                                                                tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    seed_account(srv.accounts, "Bob", "pw")
    srv.events.record("login", account="Bob", ip=IP)
    staff = _staff(world, clock, srv)

    staff.clear(); dispatch(staff, ".unban_ip Bob")
    assert "no active ip ban" in staff.text.lower()

    staff.clear(); dispatch(staff, ".unban_ip Ghost")   # no address on record
    assert "no address on record" in staff.text.lower()

    staff.clear(); dispatch(staff, ".unban_ip")
    assert "Usage: .unban_ip" in staff.text


def test_bans_lists_by_name_never_by_address(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    srv.bans.ban("Bob", "name", hours=24, now=1_000_000.0)
    srv.bans.ban(IP, "ip", hours=48, now=1_000_000.0)
    srv.ban_history.record(IP, "Carol", "Wizzy", 48, "flooding")

    staff = _staff(world, clock, srv)
    staff.clear(); dispatch(staff, ".bans")
    out = staff.text
    assert "Bob" in out and "account" in out
    assert "Carol" in out and "address" in out      # labelled by the ACCOUNT
    assert "Wizzy" in out and "flooding" in out
    assert IP not in out


def test_bans_is_empty_when_nothing_is_banned(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    staff = _staff(world, clock, srv)
    staff.clear(); dispatch(staff, ".bans")
    assert "No active bans." in staff.text


def test_bans_omits_expired_ones(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    srv.bans.ban("Old", "name", hours=1, now=1_000_000.0 - 10 * 3600)
    srv.bans.ban("Now", "name", hours=24, now=1_000_000.0)
    staff = _staff(world, clock, srv)
    staff.clear(); dispatch(staff, ".bans")
    assert "Now" in staff.text and "Old" not in staff.text


def test_bans_labels_an_unhistoried_ip_ban_without_showing_it(world, clock,
                                                              tmp_path):
    """A hand-edited bans.json, or one placed before v1.33.0. It is shown as an
    unnamed address rather than AS one — the rule holds even for a record the
    engine did not write."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.bans.ban(IP, "ip", hours=24, now=1_000_000.0)
    staff = _staff(world, clock, srv)
    staff.clear(); dispatch(staff, ".bans")
    assert "(unnamed address)" in staff.text
    assert IP not in staff.text


def test_bans_fits_at_forty(world, clock, tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    srv.bans.ban("Bob", "name", hours=24, now=1_000_000.0)
    srv.bans.ban(IP, "ip", hours=48, now=1_000_000.0)
    srv.ban_history.record(IP, "Carol", "Wizzy", 48, "persistent flooding")
    staff = _staff(world, clock, srv, width=40)
    staff.clear(); dispatch(staff, ".bans")
    for line in staff.text.split("\n"):
        assert len(line) <= 40, repr(line)


# ---------------------------------------------------------------------------
# ⛔ The rule: no command output ever prints an IP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_ban_command_ever_prints_an_address(tmp_path):
    """The grep. Drive every ban command through a real session — including the
    collateral screen, the recidivism warning, the listing and the staff log —
    and assert the address is in none of it.

    It IS on disk, in `bans.json` and `ban_history.json`, and that is the whole
    of its exposure: an operator opening a file on purpose during an audit.
    """
    srv, port = await _start(_cfg(tmp_path))
    try:
        for who in ("Generic", "Bob", "Carol"):
            seed_account(srv.accounts, who, "correct horse battery")
        for who in ("Bob", "Carol"):
            srv.events.record("login", account=who, ip=IP)
        srv.ban_history.record(IP, "Older", "Wizzy", 24, "flooding")

        ra, wa = await _login(port, "Generic")
        transcript = ""
        for line in (".ban_ip Bob", "y", ".bans", ".staff_actions",
                     ".unban_ip Bob", ".bans", ".ban_ip Carol", "n",
                     ".unban_ip Carol", ".ban Bob", ".unban Bob"):
            await send(wa, line)
            transcript += await collect(ra, timeout=0.5)
        wa.close()

        assert IP not in transcript
        for octet in IP.split("."):
            if len(octet) > 2:                 # skip trivially common numbers
                assert octet not in transcript.replace("203.0.113", "")
        # ...and it really did all happen.
        assert "Banned the address" in transcript
        assert "Lifted the IP ban" in transcript
        assert "Never mind" in transcript

        # The address IS on disk — that is where an audit would find it.
        assert IP in (srv.config.ban_history_path()).read_text(encoding="utf-8")
    finally:
        await srv.aclose()


def test_the_agreement_hash_is_unchanged(tmp_path):
    """Ban records are ENFORCEMENT, not the passive connection-notes clause 3
    governs. Nothing about the agreement moved."""
    from conftest import CURRENT_AGREEMENT_HASH
    assert TalkerServer(_cfg(tmp_path)).agreement_hash == CURRENT_AGREEMENT_HASH
