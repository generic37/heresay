# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the v1.10.0 distributed-attack hardening:

  1. common/breached-password rejection at every password-set point
  2. server-wide auth-failure ceiling (cooldown; recovery counts too)
"""

import asyncio
import json
from pathlib import Path

import pytest

from heresay import passwords, recovery
from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.ratelimit import GlobalAuthCeiling
from heresay.server import (
    COMMON_PASSWORD_MSG, TalkerServer, _password_problem,
)

from conftest import seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
STRONG = "brass-otter-hum-9"          # not on the blocklist, 8..128 chars


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
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return (srv, srv._servers[0].sockets[0].getsockname()[1],
            srv._ws_server.sockets[0].getsockname()[1])


async def ws_first_reply(ws_port, name):
    """Connect to the WS door (NON-console), send a name, return the reply text.
    The WS door is where the creation-pause is observable (the plaintext console
    is deliberately exempt so the operator can always bootstrap)."""
    from websockets.asyncio.client import connect as ws_connect
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        async def wc(t=0.5):
            out = []
            while True:
                try:
                    f = await asyncio.wait_for(ws.recv(), t)
                except Exception:
                    break
                out.append(f if isinstance(f, str) else f.decode())
            return "".join(out)
        await wc()
        await ws.send(name)
        return await wc()


async def collect(reader, timeout=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def send(writer, text):
    writer.write((text + "\n").encode())
    await writer.drain()


# ======================= 1. breach blocklist =======================

def test_is_common_case_insensitive():
    assert passwords.is_common("password1")          # on the list
    assert passwords.is_common("PASSWORD")           # case-folded
    assert passwords.is_common("Qwerty")
    assert not passwords.is_common(STRONG)
    assert not passwords.is_common("swordfish-x9")


def test_password_problem_order_length_then_common():
    # A short AND common password reports its LENGTH first (order: length, then
    # ceiling, then blocklist).
    assert "at least 8" in _password_problem("abc")            # short (and common-ish)
    assert _password_problem("password1") == COMMON_PASSWORD_MSG   # long enough but common
    assert "maximum 128" in _password_problem("x" * 200)       # over the ceiling
    assert _password_problem(STRONG) is None                    # accepted
    # The message never echoes the attempt or reveals list membership beyond this.
    assert "password1" not in COMMON_PASSWORD_MSG


@pytest.mark.asyncio
async def test_common_password_rejected_at_create(tmp_path):
    srv, port, ws_port = await _start(_cfg(tmp_path))
    try:
        # common -> rejected with the message
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "Newbie")
        await collect(r); await send(w, "yes")
        await collect(r); await send(w, "password1")
        out = await collect(r)
        assert "too common" in out
        assert "random words" in out
        w.close()
        # strong -> proceeds to the agreement/arrival
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        await collect(r2); await send(w2, "Newbie")
        await collect(r2); await send(w2, "yes")
        await collect(r2); await send(w2, STRONG)
        await collect(r2); await send(w2, STRONG)
        await collect(r2); await send(w2, "y")
        assert "atrium" in (await collect(r2, timeout=2.0)).lower()
        w2.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_common_password_rejected_at_passwd(tmp_path):
    path = tmp_path / "accounts.json"
    seed_account(AccountStore(path), "Changer", STRONG)
    srv, port, ws_port = await _start(_cfg(tmp_path))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "Changer")
        await collect(r); await send(w, STRONG)        # log in
        await collect(r, timeout=1.5)
        await send(w, ".passwd")
        await collect(r); await send(w, STRONG)        # old password
        await collect(r); await send(w, "password1")   # new password: common
        assert "too common" in await collect(r)
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_common_password_rejected_at_recovery_reset(tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    seed_account(store, "Ada", STRONG)
    codes = recovery.generate_set()
    store.set_recovery("Ada", codes)                 # sync setup
    srv, port, ws_port = await _start(_cfg(tmp_path))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "Ada")
        await collect(r); await send(w, "recover")
        await collect(r, timeout=1.0); await send(w, codes[0])
        await collect(r, timeout=1.5); await send(w, "password1")   # new pw: common
        assert "too common" in await collect(r, timeout=1.0)
        w.close()
    finally:
        await srv.aclose()


# ======================= 2. global auth ceiling =======================

def test_global_ceiling_trip_clear_disabled():
    g = GlobalAuthCeiling(window=10.0, threshold=3, cooldown=5.0)
    assert g.note_failure(0.0) is False
    assert g.note_failure(0.0) is False
    assert g.note_failure(0.0) is True          # 3rd trips
    assert g.in_cooldown(1.0) is True
    assert g.note_cleared(1.0) is False         # still cooling
    assert g.note_cleared(6.0) is True          # lapses once
    assert g.note_cleared(6.0) is False         # only once
    assert g.in_cooldown(6.0) is False
    # window pruning: old failures age out, so they don't accumulate forever
    g2 = GlobalAuthCeiling(window=10.0, threshold=3, cooldown=5.0)
    g2.note_failure(0.0); g2.note_failure(0.0)
    assert g2.note_failure(100.0) is False       # the two old ones pruned; only 1 in window


def test_global_ceiling_disabled_is_inert():
    g = GlobalAuthCeiling(threshold=1, enabled=False)
    assert g.note_failure(0.0) is False
    assert g.in_cooldown(0.0) is False


def test_recovery_failure_feeds_backoff_and_global_counter(tmp_path):
    # Recovery is a first-class auth path: a failed recovery attempt must feed the
    # SAME per-IP backoff and the SAME global ceiling as a failed login.
    srv = TalkerServer(_cfg(tmp_path, auth_backoff_base=1.0))
    assert srv.auth_backoff.delay("5.5.5.5") == 0.0
    assert len(srv.global_ceiling._fails) == 0
    srv._record_auth_failure("5.5.5.5", "recovery")
    assert srv.auth_backoff.delay("5.5.5.5") > 0.0        # per-IP backoff advanced
    assert len(srv.global_ceiling._fails) == 1            # global counter advanced


@pytest.mark.asyncio
async def test_ceiling_pauses_creation_after_failures_then_clears(tmp_path):
    # Trip on 3 failures; a generous cooldown so the WS round-trip below doesn't
    # race the timer (the clear TRANSITION is covered deterministically by the
    # unit test). Per-IP backoff off so the failing logins don't each self-delay.
    path = tmp_path / "accounts.json"
    seed_account(AccountStore(path), "Target", STRONG)
    cfg = _cfg(tmp_path, auth_backoff_base=0.0, global_auth_fail_threshold=3,
               global_auth_cooldown_seconds=30.0)
    srv, port, ws_port = await _start(cfg)
    try:
        # Three failed logins for Target -> ceiling trips.
        for _ in range(3):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            await collect(r); await send(w, "Target")
            await collect(r); await send(w, "wrong-password-x")
            await collect(r); w.close()
        assert srv.global_ceiling.in_cooldown(srv.clock())

        # New-account creation is paused during cooldown — checked on the WS door
        # (the plaintext console is exempt so the operator can always bootstrap).
        assert "can't be created just now" in await ws_first_reply(ws_port, "Fresh")

        # Manually lapse the cooldown: creation is allowed again.
        srv.global_ceiling._cooldown_until = 0.0
        assert not srv.global_ceiling.in_cooldown(srv.clock())
        assert "Create it?" in await ws_first_reply(ws_port, "Fresh")
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_ceiling_disabled_never_pauses_creation(tmp_path):
    path = tmp_path / "accounts.json"
    seed_account(AccountStore(path), "Target", STRONG)
    cfg = _cfg(tmp_path, auth_backoff_base=0.0, global_auth_enabled=False,
               global_auth_fail_threshold=2)
    srv, port, ws_port = await _start(cfg)
    try:
        for _ in range(4):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            await collect(r); await send(w, "Target")
            await collect(r); await send(w, "wrong-password-x")
            await collect(r); w.close()
        assert not srv.global_ceiling.in_cooldown(srv.clock())   # disabled: inert
        assert "Create it?" in await ws_first_reply(ws_port, "Fresh")   # not paused
    finally:
        await srv.aclose()


# ---- the ceiling as a RECORDED FACT (v1.53.0) -----------------------------
#
# ⛔ The trip and the clear were the one notable security transition the engine
# only ever said out loud in the journal. Everything else a door does — a
# refusal, a failed credential, a takeover, a staff action — is a fact in
# `events.jsonl`, readable back in-world and by an ops watcher keying on a
# stable TYPE rather than on log prose that a reword would break.
#
# ⚠ EDGE-TRIGGERED. `note_failure()` and `note_cleared()` each return True
# exactly once per transition, and the records live INSIDE those guards — so a
# thousand failed passwords during one cooldown write a thousand `auth_fail`
# facts and exactly ONE `auth_ceiling`.

def _ceiling_events(srv, phase=None):
    rows = srv.events.read(types=("auth_ceiling",))
    return [e for e in rows if phase is None or e.get("phase") == phase]


def test_the_ceiling_records_ONE_tripped_fact_at_the_transition(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, global_auth_fail_threshold=3,
                            global_auth_fail_window_seconds=60.0,
                            global_auth_cooldown_seconds=120.0))
    for _ in range(2):
        srv._record_auth_failure("5.5.5.5", "login")
    assert _ceiling_events(srv) == []                 # under the threshold

    srv._record_auth_failure("5.5.5.5", "login")      # the 3rd trips it
    tripped = _ceiling_events(srv, "tripped")
    assert len(tripped) == 1

    # ...and it carries the numbers that explain WHY it tripped, so the fact is
    # readable years later without knowing what the config said that day.
    e = tripped[0]
    assert e["threshold"] == 3
    assert e["window"] == 60.0
    assert e["cooldown"] == 120.0


def test_further_failures_during_the_cooldown_record_NO_second_trip(tmp_path):
    """⛔ The edge, not the state. A sustained run must not write one of these
    per attempt — that would turn a transition into a flood, and bury the
    signal in the thing that caused it."""
    srv = TalkerServer(_cfg(tmp_path, global_auth_fail_threshold=2,
                            global_auth_cooldown_seconds=120.0))
    for _ in range(20):
        srv._record_auth_failure("5.5.5.5", "login")
    assert len(_ceiling_events(srv, "tripped")) == 1
    # The per-failure facts are all still there — it is only the TRANSITION
    # that is recorded once.
    assert len(srv.events.read(types=("auth_fail",))) == 20


@pytest.mark.asyncio
async def test_the_ceiling_records_ONE_cleared_fact_when_it_lapses(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, global_auth_fail_threshold=2,
                            global_auth_cooldown_seconds=120.0))
    srv._record_auth_failure("5.5.5.5", "login")
    srv._record_auth_failure("5.5.5.5", "login")
    assert len(_ceiling_events(srv, "tripped")) == 1
    assert _ceiling_events(srv, "cleared") == []      # still cooling

    # The cooldown LAPSES — pushed into the past rather than zeroed, because
    # `note_cleared()` reads a zero as "there was never a cooldown" and would
    # report no transition at all. The next auth attempt through the gate is
    # what notices.
    srv.global_ceiling._cooldown_until = srv.clock() - 1.0
    await srv._auth_cooldown_gate()
    assert len(_ceiling_events(srv, "cleared")) == 1

    # Every later attempt passes through the same gate and records nothing.
    for _ in range(5):
        await srv._auth_cooldown_gate()
    assert len(_ceiling_events(srv, "cleared")) == 1


def test_the_ceiling_fact_carries_NO_ip_and_NO_CONTENT(tmp_path):
    """⛔ The covenant question that comes with any new type. The ceiling is
    SERVER-WIDE — there is no single peer it is about — so an `ip` here would be
    both wrong and a fact about somebody the event does not concern. Phase and
    three numbers, and nothing else."""
    srv = TalkerServer(_cfg(tmp_path, global_auth_fail_threshold=1))
    srv._record_auth_failure("5.5.5.5", "login")
    e = _ceiling_events(srv, "tripped")[0]
    assert set(e) == {"ts", "type", "phase", "threshold", "window", "cooldown"}
    for key in e:
        assert key.lower() not in ("ip", "from_ip", "account", "name", "body",
                                   "text", "message", "line", "sample")
    # No value in the record is the address that happened to be failing.
    assert "5.5.5.5" not in json.dumps(e)


def test_the_serialized_shape_is_what_an_ops_watcher_greps_for(tmp_path):
    """⛔ The exact bytes. A watcher tails this file for a fixed substring, so
    the separators matter as much as the field names: `record()` serialises with
    no spaces, exactly as `auth_fail` does, and this pins that."""
    srv = TalkerServer(_cfg(tmp_path, global_auth_fail_threshold=1,
                            global_auth_cooldown_seconds=120.0))
    srv._record_auth_failure("5.5.5.5", "login")
    raw = Path(srv.events.path).read_text(encoding="utf-8")
    assert '"type":"auth_ceiling","phase":"tripped"' in raw

    srv.global_ceiling._cooldown_until = srv.clock() - 1.0
    asyncio.run(srv._auth_cooldown_gate())
    raw = Path(srv.events.path).read_text(encoding="utf-8")
    assert '"type":"auth_ceiling","phase":"cleared"' in raw


def test_the_journal_still_says_it_too(tmp_path, caplog):
    """⚠ ADDED, not moved. A human reading the journal during an incident should
    still see the line; the event store is a second listener, not a relocation.
    (The two-sink rule is about DOMAIN FACTS not living in both places — this
    stays a `log.warning`, which is process output about the door's posture, and
    the covenant test that sweeps `log.info` calls is untouched.)"""
    srv = TalkerServer(_cfg(tmp_path, global_auth_fail_threshold=1))
    with caplog.at_level("WARNING"):
        srv._record_auth_failure("5.5.5.5", "login")
    assert any("ceiling tripped" in r.getMessage() for r in caplog.records)


def test_a_disabled_ceiling_records_nothing_at_all(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, global_auth_enabled=False,
                            global_auth_fail_threshold=1))
    for _ in range(5):
        srv._record_auth_failure("5.5.5.5", "login")
    assert _ceiling_events(srv) == []
