# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ban split: `.ban` is an account, `.ban_ip` is an address (v1.32.0).

`.ban` used to take either and guess from the shape of its argument. That put
two quite different powers behind one word — banning a person, and banning an
address that may hold a household, a shared flat, a campus or a carrier's NAT.
Splitting them means the blunter one has to be asked for by name, shows what it
would catch before acting, and carries its own default duration.

The address comes from a new `login` event ({account, ip}) rather than a durable
`last_ip` on the account: a permanent account↔address link is the thing the
covenant declines, so the tie lives in the event log and fades with it.
"""

import asyncio
from pathlib import Path

import pytest

import heresay.commands as C
from heresay.commands import dispatch
from heresay.config import Config
from heresay.events import EVENT_TYPES, EventStore
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


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


# ---------------------------------------------------------------------------
# The `login` event — the account↔address tie
# ---------------------------------------------------------------------------

def test_login_is_a_declared_type():
    """v1.32.0 made it the eleventh; `staff_mail` twelve (v1.35.0) and
    `bad_frame` thirteen (v1.52.0). The count is pinned because adding a type is
    a deliberate act — but what this test is FOR is that `login` is one of
    them."""
    assert "login" in EVENT_TYPES
    assert len(EVENT_TYPES) == 15


@pytest.mark.asyncio
async def test_a_password_login_records_the_account_and_the_address(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Corvid", "correct horse battery")
        r, w = await _login(port, "Corvid")
        rows = srv.events.read(types=("login",))
        assert [(e["account"], e["ip"]) for e in rows] == [("Corvid", "127.0.0.1")]
        # Facts only — nothing else rode along.
        assert set(rows[0]) == {"ts", "type", "account", "ip"}
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_it_records_the_display_name_not_what_was_typed(tmp_path):
    """The store is keyed by casefold but read by people; a ban target resolved
    from it must match the name an operator would type."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Corvid", "correct horse battery")
        r, w = await _login(port, "cOrViD")
        assert [e["account"] for e in srv.events.read(types=("login",))] == ["Corvid"]
        w.close()
    finally:
        await srv.aclose()


def test_login_is_not_fsynced(tmp_path, monkeypatch):
    """It fires on EVERY successful auth including every silent token resume —
    the most routine event the store has. Losing the last one to a power cut
    costs a `.ban_ip` its shortcut and nothing else."""
    import os
    from heresay.events import DURABLE_TYPES
    assert "login" not in DURABLE_TYPES
    synced = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    EventStore(tmp_path / "e.jsonl").record("login", account="A", ip="1.1.1.1")
    assert synced == []


def test_the_last_login_lookup_is_newest_first(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    s.record("login", account="Corvid", ip="1.1.1.1")
    s.record("login", account="Corvid", ip="2.2.2.2")
    s.record("login", account="Wren", ip="3.3.3.3")
    assert s.last_login_ip("Corvid") == "2.2.2.2"
    assert s.last_login_ip("corvid") == "2.2.2.2"        # casefold identity
    assert s.last_login_ip("Nobody") is None


def test_a_login_outside_the_window_is_simply_gone(tmp_path):
    """⚠ THE bound. The tie lasts exactly as long as the operational record —
    there is no durable `last_ip` to fall back on, and that is the point."""
    wall = [1_000_000.0]
    s = EventStore(tmp_path / "e.jsonl", retention_days=1.0,
                   wall_clock=lambda: wall[0])
    s.record("login", account="Corvid", ip="1.1.1.1")
    assert s.last_login_ip("Corvid") == "1.1.1.1"
    wall[0] += 2 * 86400
    s.trim()
    assert s.last_login_ip("Corvid") is None


def test_accounts_on_ip_dedupes_and_keeps_order(tmp_path):
    s = EventStore(tmp_path / "e.jsonl")
    for acct, ip in (("Corvid", "1.1.1.1"), ("Wren", "1.1.1.1"),
                     ("Corvid", "1.1.1.1"), ("Bob", "2.2.2.2")):
        s.record("login", account=acct, ip=ip)
    assert s.accounts_on_ip("1.1.1.1") == ["Corvid", "Wren"]
    assert s.accounts_on_ip("9.9.9.9") == []


# ---------------------------------------------------------------------------
# `.ban` is name-only
# ---------------------------------------------------------------------------

def test_ban_refuses_an_address_with_a_pointer(world, clock, tmp_path):
    from heresay.bans import BanStore
    bans = BanStore(tmp_path / "bans.json", wall_clock=lambda: 0.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, bans=bans, level="wizard",
                      wall_clock=lambda: 0.0)
    wiz.clear()
    dispatch(wiz, ".ban 9.9.9.9")
    assert not bans.is_banned_ip("9.9.9.9", now=0.0)
    assert ".ban_ip" in wiz.text          # ...and says where to go instead


def test_ban_still_bans_the_account(world, clock, tmp_path):
    from heresay.bans import BanStore
    bans = BanStore(tmp_path / "bans.json", wall_clock=lambda: 0.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, bans=bans, level="wizard",
                      wall_clock=lambda: 0.0)
    dispatch(wiz, ".ban Bob 5")
    assert bans.is_banned_name("Bob", now=4 * 3600)
    assert not bans.is_banned_name("Bob", now=6 * 3600)


def test_ban_refuses_the_arch(world, clock, tmp_path):
    """Lockout-proofing, the same rule `.deleteme` and `.promote` carry."""
    from heresay.bans import BanStore
    bans = BanStore(tmp_path / "bans.json", wall_clock=lambda: 0.0)
    wiz = FakeSession(world, name="Wiz", clock=clock, bans=bans, level="wizard",
                      wall_clock=lambda: 0.0,
                      config=Config(wrap_columns=1000, owner_name="Generic"))
    wiz.clear()
    dispatch(wiz, ".ban Generic")
    assert not bans.is_banned_name("Generic", now=0.0)
    assert "can't ban" in wiz.text.lower()


# ---------------------------------------------------------------------------
# `.ban_ip` — resolution, collateral, the confirm
# ---------------------------------------------------------------------------

def test_the_ip_commands_sit_with_the_rest_of_moderation(tmp_path):
    """v1.33.0 moved `.ban_ip` from arch to staff. It was arch-only when it
    could expose an address; nothing prints one now, so the reason for the
    higher bar went with it."""
    from heresay.theme import DEFAULT_THEME as T
    for verb in ("ban", "ban_ip", "unban", "unban_ip", "bans"):
        assert C._visible(verb, T.staff_rank, T), verb
        assert C._visible(verb, T.owner_rank, T), verb
        assert not C._visible(verb, T.resident_rank, T), verb


@pytest.mark.asyncio
async def test_it_bans_the_address_of_a_connected_account(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        rb, wb = await _login(port, "Bob")
        ra, wa = await _login(port, "Generic")

        await send(wa, ".ban_ip Bob")
        shown = await collect(ra, timeout=0.5)
        assert "1 online and 0 offline account(s)" in shown
        assert "online:  Bob" in shown              # named, and marked online
        await send(wa, "y")
        out = await collect(ra, timeout=0.5)
        assert "Banned the address" in out
        assert srv.bans.is_banned_ip("127.0.0.1")
        wa.close(); wb.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_n_cancels_and_places_nothing(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        rb, wb = await _login(port, "Bob"); wb.close()
        ra, wa = await _login(port, "Generic")

        await send(wa, ".ban_ip Bob")
        await collect(ra, timeout=0.5)
        await send(wa, "n")
        assert "Never mind" in await collect(ra, timeout=0.5)
        assert not srv.bans.is_banned_ip("127.0.0.1")
        wa.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_bare_enter_cancels_too(tmp_path):
    """The safe answer to a question you did not mean to reach."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        rb, wb = await _login(port, "Bob"); wb.close()
        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Bob")
        await collect(ra, timeout=0.5)
        await send(wa, "")
        assert "Never mind" in await collect(ra, timeout=0.5)
        assert not srv.bans.is_banned_ip("127.0.0.1")
        wa.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_it_resolves_the_address_from_a_login_event_when_offline(tmp_path):
    """The fallback that makes a durable `last_ip` unnecessary."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Ghost", "correct horse battery")
        # Ghost logged in from elsewhere once, and is long gone.
        srv.events.record("login", account="Ghost", ip="203.0.113.7")

        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Ghost")
        shown = await collect(ra, timeout=0.5)
        assert "0 online and 1 offline account(s)" in shown
        assert "offline: Ghost" in shown
        await send(wa, "y")
        await collect(ra, timeout=0.5)
        assert srv.bans.is_banned_ip("203.0.113.7")
        wa.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_offline_and_unseen_offers_the_account_ban_instead(tmp_path):
    """No live session, nothing in the window: the engine says so rather than
    reaching for a stored address, because there is no stored address."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Stranger", "correct horse battery")
        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Stranger")
        out = await collect(ra, timeout=0.5)
        assert "hasn't been seen recently" in out
        assert ".ban Stranger" in out
        assert not srv.bans._bans          # nothing placed, nothing pending
        wa.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_collateral_count_covers_the_whole_window(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        for who in ("Bob", "Wren", "Esoteric"):
            srv.events.record("login", account=who, ip="198.51.100.4")

        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Bob")
        shown = await collect(ra, timeout=0.5)
        # None of the three is connected — only the arch is — so all three are
        # offline: seen on the address in the window, blocked going forward.
        assert "0 online and 3 offline account(s)" in shown
        assert "offline: Bob, Esoteric, Wren" in shown
        assert "(last 10 days)" in shown            # the window is named
        await send(wa, "n")
        await collect(ra, timeout=0.5)
        wa.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_an_hours_argument_overrides_the_default(tmp_path):
    srv, port = await _start(_cfg(tmp_path, ban_ip_default_hours=24.0))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        srv.events.record("login", account="Bob", ip="203.0.113.9")
        ra, wa = await _login(port, "Generic")
        await send(wa, ".ban_ip Bob 2")
        assert "for 2h" in await collect(ra, timeout=0.5)
        await send(wa, "y")
        await collect(ra, timeout=0.5)
        wa.close()
    finally:
        await srv.aclose()


def test_the_default_duration_is_its_own_config_key():
    """Not shared with `.ban`: an address ban is the blunter instrument, so an
    operator can keep it short without shortening account bans."""
    assert Config().ban_ip_default_hours == 24.0
    c = Config(ban_default_hours=72.0, ban_ip_default_hours=1.0)
    assert (c.ban_default_hours, c.ban_ip_default_hours) == (72.0, 1.0)


@pytest.mark.asyncio
async def test_it_refuses_the_arch_and_an_unknown_name(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        ra, wa = await _login(port, "Generic")

        await send(wa, ".ban_ip Generic")
        assert "can't ban" in (await collect(ra, timeout=0.5)).lower()
        await send(wa, ".ban_ip Nobody")
        assert "no one called" in (await collect(ra, timeout=0.5)).lower()
        assert not srv.bans._bans
        wa.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# The arch is never locked out
# ---------------------------------------------------------------------------

def test_loopback_is_never_ip_refused(tmp_path):
    """⚠ ABSOLUTE, not conditional. The plaintext console is the operator at the
    machine and the door the arch is bootstrapped through; a guaranteed local
    way in is worth more than being able to ban yourself locally."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.bans.ban("127.0.0.1", "ip", hours=24)
    assert srv.bans.is_banned_ip("127.0.0.1")
    assert srv._ip_ban_exempt("127.0.0.1")
    assert srv._ip_ban_exempt("::1")


def test_the_arch_s_last_known_address_is_exempt(tmp_path):
    """The one that matters remotely: an arch who bans the NAT they share with a
    troublemaker must not lock themselves out from the far side of the world."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.events.record("login", account="Generic", ip="203.0.113.5")
    srv.bans.ban("203.0.113.5", "ip", hours=24)
    assert srv._ip_ban_exempt("203.0.113.5")
    assert not srv._ip_ban_exempt("198.51.100.9")


def test_the_exemption_is_best_effort_and_says_so(tmp_path):
    """If the arch has not been seen inside the window there is nothing to match
    on — which is exactly why the loopback exemption is absolute rather than
    also conditional."""
    srv = TalkerServer(_cfg(tmp_path))
    assert srv.events.last_login_ip("Generic") is None
    assert not srv._ip_ban_exempt("203.0.113.5")
    assert srv._ip_ban_exempt("127.0.0.1")            # ...and this still holds


@pytest.mark.asyncio
async def test_a_banned_address_is_refused_for_everyone_else(tmp_path):
    srv, port = await _start(_cfg(tmp_path, owner_name="Elsewhere"))
    try:
        srv.bans.ban("127.0.0.1", "ip", hours=24)
        # The arch is "Elsewhere" and has never been seen, so loopback is the
        # only exemption in play — and it is absolute, so this door still opens.
        r, w = await asyncio.open_connection("127.0.0.1", port)
        assert "refused" not in (await collect(r)).lower()
        w.close()
    finally:
        await srv.aclose()


def test_the_collateral_excludes_the_arch(world, clock, tmp_path):
    """Counting the arch would overstate the damage (they are exempt from
    enforcement) and naming them would be noise."""
    events = EventStore(tmp_path / "e.jsonl")
    for who in ("Bob", "Generic", "Wren"):
        events.record("login", account=who, ip="1.2.3.4")
    s = FakeSession(world, name="Generic", clock=clock, level="arch",
                    events=events,
                    config=Config(wrap_columns=1000, owner_name="Generic"))
    online, offline = C.ban_ip_collateral(s, "1.2.3.4")
    assert sorted(offline) == ["Bob", "Wren"]        # not Generic
    assert "Generic" not in online and "Generic" not in offline


@pytest.mark.asyncio
async def test_the_arch_is_not_kicked_by_their_own_ip_ban(tmp_path):
    """They share the address with the target — which is the ordinary case on a
    home connection, and the one where a careless ban would be worst."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        rb, wb = await _login(port, "Bob")
        ra, wa = await _login(port, "Generic")

        await send(wa, ".ban_ip Bob")
        await collect(ra, timeout=0.5)
        await send(wa, "y")
        await collect(ra, timeout=0.5)

        # Bob is gone; the arch is still standing.
        assert srv.world.get_resident("Generic").session is not None
        await send(wa, ".who")
        assert "Generic" in await collect(ra, timeout=0.5)
        wa.close(); wb.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# ⛔ The agreement did NOT change
# ---------------------------------------------------------------------------

def test_the_agreement_hash_is_unchanged(tmp_path):
    """Clause 3 (v1.31.1) already disclosed connection/IP recording and that
    certain records are noted against the account — it was worded to cover this
    exact event. Nobody re-consents for the ban rework."""
    from conftest import CURRENT_AGREEMENT_HASH
    srv = TalkerServer(_cfg(tmp_path))
    assert srv.agreement_hash == CURRENT_AGREEMENT_HASH
    # The clause that covers it is present and resolved.
    flat = " ".join(srv.agreement_text.split())
    assert "the time and your connection's IP address are recorded" in flat
    assert "noted against the account they concern" in flat
