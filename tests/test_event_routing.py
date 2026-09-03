# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where each domain fact ends up (v1.30.1).

v1.30.0 built the store and routed five kinds of fact; eleven were left in
journald. This routes the rest, so **journald holds process output only** — the
startup lines, malformed-config warnings and tracebacks — and every fact about a
person or an address lives in `events.jsonl`.

These are end-to-end: a real server, real sockets, the real file. What they
protect is the routing itself, which a unit test of the store cannot see: that
the fact is recorded at all, with the right type and enum value, and that it
does NOT also go to the log (the whole point being that no fact lives in both).
"""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from heresay.commands import dispatch
from heresay.config import Config
from heresay.events import (ACCOUNT_NAMED_SECURITY_EVENTS,
                            ACCOUNT_SECURITY_EVENTS, CONNECTION_DIRS,
                            EVENT_TYPES, REFUSAL_REASONS)
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
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def collect(reader, timeout=0.3):
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


async def send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


def _of(srv, type_):
    return srv.events.read(types=(type_,))


# ---- refused: six reasons, one type ---------------------------------------
#
# One type rather than six, because "who was turned away, and why" is one
# question. The reason is the answer, not a different question.

@pytest.mark.asyncio
async def test_a_banned_ip_is_recorded_as_refused(tmp_path):
    """⚠ Driven through the WEB door with a forwarded address, because since
    v1.32.0 **loopback is never IP-refused** — it is the local console and the
    arch's guaranteed way back in. A test connecting from 127.0.0.1 can no
    longer be refused at all, so it would assert nothing about the routing.

    `trusted_proxy` is the supported way to present a real remote address (it is
    how the production Caddy front works), which makes this an honest end-to-end
    test of a non-exempt address rather than a stub.
    """
    from websockets.asyncio.client import connect as ws_connect
    srv = TalkerServer(_cfg(tmp_path, trusted_proxy="127.0.0.1"))
    await srv.start()
    try:
        ws_port = srv._ws_server.sockets[0].getsockname()[1]
        srv.bans.ban("203.0.113.9", "ip", hours=1)
        try:
            async with ws_connect(f"ws://127.0.0.1:{ws_port}",
                                  additional_headers={
                                      "X-Forwarded-For": "203.0.113.9"}) as ws:
                await _ws_collect(ws)
        except Exception:
            pass                      # refused and closed is the expected end
        rows = _of(srv, "refused")
        assert [(e["ip"], e["reason"]) for e in rows] == [("203.0.113.9", "ip_banned")]
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_loopback_is_never_refused_and_records_nothing(tmp_path):
    """The other half of the same rule, asserted rather than assumed: the local
    door opens even while its own address is banned, and no refusal is recorded
    because none happened."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        srv.bans.ban("127.0.0.1", "ip", hours=1)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        assert "refused" not in (await collect(r)).lower()
        w.close()
        assert _of(srv, "refused") == []
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_connection_cap_is_recorded_as_refused(tmp_path):
    """A burst-prone one — which is exactly why it is not fsynced."""
    srv, port = await _start(_cfg(tmp_path, max_conns_per_ip=1))
    conns = []
    try:
        for _ in range(3):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            await collect(r, timeout=0.2)
            conns.append(w)
        reasons = [e["reason"] for e in _of(srv, "refused")]
        assert reasons and set(reasons) == {"conn_cap"}
    finally:
        for w in conns:
            w.close()
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_banned_account_name_is_recorded_as_refused(tmp_path):
    """Post-auth, so it is a different refusal from the IP one — and says so."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Bob", "correct horse battery")
        srv.bans.ban("Bob", "name", hours=1)
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "Bob")
        await collect(r); await send(w, "correct horse battery")
        assert "banned" in (await collect(r)).lower()
        w.close()
        assert [e["reason"] for e in _of(srv, "refused")] == ["banned_name"]
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_reserved_name_is_recorded_as_refused(tmp_path):
    """A RANK NAME is absolutely reserved — no console exemption — so the
    plaintext door reaches this one. (v1.44.0 retired the `staff`/`owner`
    hardcode; on the shipped ladder they are ranks, so this still holds.)"""
    srv, port = await _start(_cfg(tmp_path))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "staff")
        assert "isn't available" in (await collect(r)).lower()
        w.close()
        assert [e["reason"] for e in _of(srv, "refused")] == ["reserved_name"]
    finally:
        await srv.aclose()


# The arch-name and rate-limit refusals both EXEMPT the local console, and the
# plaintext door IS the console (that is what lets the operator bootstrap the
# arch on a fresh box). So they are unreachable through it by design, and these
# two go through the WEB door — which is an ordinary network door — rather than
# being asserted against a hand-recorded event that would prove nothing.

async def _ws_say(ws_port, line):
    from websockets.asyncio.client import connect as ws_connect
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await _ws_collect(ws)                    # greeting + name prompt
        await ws.send(line)
        return await _ws_collect(ws)


async def _ws_collect(ws, timeout=0.4):
    out = []
    while True:
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout)
        except (asyncio.TimeoutError, Exception):
            break
        if isinstance(frame, (bytes, bytearray)):
            frame = frame.decode(errors="replace")
        if not frame.startswith("\x01"):
            out.append(frame)
    return "".join(out)


@pytest.mark.asyncio
async def test_the_arch_name_from_a_network_door_is_recorded_as_refused(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, owner_name="Generic"))
    await srv.start()
    try:
        ws_port = srv._ws_server.sockets[0].getsockname()[1]
        assert "isn't available" in (await _ws_say(ws_port, "Generic")).lower()
        assert [e["reason"] for e in _of(srv, "refused")] == ["owner_name"]
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_registration_rate_limiting_is_recorded(tmp_path):
    srv = TalkerServer(_cfg(tmp_path, registration_burst=0))
    await srv.start()
    try:
        ws_port = srv._ws_server.sockets[0].getsockname()[1]
        assert "too quickly" in (await _ws_say(ws_port, "Newbie")).lower()
        assert [e["reason"] for e in _of(srv, "refused")] == ["rate_limited"]
    finally:
        await srv.aclose()


def test_every_documented_refusal_reason_is_reachable():
    """The enum is a promise about what a reader might meet. Each of the six is
    exercised by a test above (banned_name and the two registration ones by
    name), so a value nobody emits cannot sit in the table looking supported."""
    assert set(REFUSAL_REASONS) == {"ip_banned", "conn_cap", "banned_name",
                                    "reserved_name", "owner_name", "rate_limited"}


# ---- resume folds into connection -----------------------------------------

def test_resume_is_a_direction_not_a_type():
    """A resume is a kind of arrival, so a view asking "who has been connecting?"
    gets it in the same list — no separate type to remember to query."""
    assert CONNECTION_DIRS == ("open", "close", "resume")
    assert "resume" not in EVENT_TYPES


@pytest.mark.asyncio
async def test_a_token_resume_is_recorded_as_a_connection(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    acct = seed_account(srv.accounts, "Corvid", "correct horse battery")
    token = srv.resume.issue(acct.name)
    assert srv.resume.resolve(token) == "corvid"   # the casefold identity
    srv.events.record("connection", dir="resume", ip="203.0.113.5")

    rows = srv.events.read(types=("connection",))
    assert [(e["dir"], e["ip"]) for e in rows] == [("resume", "203.0.113.5")]
    # No name and no token, exactly as the journald line held neither.
    assert set(rows[0]) == {"ts", "type", "dir", "ip"}


# ---- takeover, flood, account security ------------------------------------

@pytest.mark.asyncio
async def test_a_takeover_records_the_account_and_the_displaced_address(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Corvid", "correct horse battery")
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        await collect(r1); await send(w1, "Corvid")
        await collect(r1); await send(w1, "correct horse battery")
        await collect(r1, timeout=0.5)

        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        await collect(r2); await send(w2, "Corvid")
        await collect(r2); await send(w2, "correct horse battery")
        await collect(r2)                      # "already connected... (y/n)"
        await send(w2, "y")
        await collect(r2, timeout=0.5)

        rows = _of(srv, "takeover")
        assert len(rows) == 1
        assert rows[0]["account"] == "Corvid"
        assert rows[0]["from_ip"] == "127.0.0.1"
        w1.close(); w2.close()
    finally:
        await srv.aclose()


def test_recovery_events_are_recorded_against_the_account(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    srv.events.record("account_security", account="Corvid",
                      event="password_reset_recovery")
    srv.events.record("account_security", account="Corvid",
                      event="recovery_codes_regenerated")
    rows = _of(srv, "account_security")
    assert [e["event"] for e in rows] == ["password_reset_recovery",
                                          "recovery_codes_regenerated"]
    assert all(e["account"] == "Corvid" for e in rows)
    # The code itself is never a field — there is nowhere for it to go.
    assert all(set(e) == {"ts", "type", "account", "event"} for e in rows)


@pytest.mark.asyncio
async def test_reconsent_is_recorded_WITHOUT_the_account_name(tmp_path):
    """⛔ The one `account_security` event that names nobody (v1.30.2).

    The journald line this replaced was name-less; v1.30.1 briefly named the
    account and v1.30.2 took the name back out, before either shipped. What the
    fact is FOR is "the revision is landing" — a count over time — and that needs
    no name. The IP stays, as the original line had it.
    """
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Oldtimer", "correct horse battery")
        srv.accounts.record_agreement("Oldtimer", "a-stale-hash")
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r); await send(w, "Oldtimer")
        await collect(r); await send(w, "correct horse battery")
        await collect(r, timeout=0.5)          # the revised agreement
        await send(w, "y")
        await collect(r, timeout=0.5)
        w.close()

        rows = [e for e in _of(srv, "account_security")
                if e["event"] == "reconsent"]
        assert len(rows) == 1
        assert "account" not in rows[0]                 # THE point of this test
        assert set(rows[0]) == {"ts", "type", "ip", "event"}
        assert rows[0]["ip"] == "127.0.0.1"
        # ...and the name is nowhere in ANY account_security row, under any key.
        # (Scoped to the type since v1.32.0: the `login` event legitimately ties
        # this account to an address — that is its entire purpose — so a
        # file-wide grep would now be asserting the wrong thing.)
        for e in _of(srv, "account_security"):
            assert "Oldtimer" not in json.dumps(e)
    finally:
        await srv.aclose()


def test_only_reconsent_is_de_named(tmp_path):
    """The asymmetry is the design, so it is pinned as data rather than left to
    the paragraph that explains it: a credential event without its account is no
    record at all, and a consent count needs no account to be useful."""
    srv = TalkerServer(_cfg(tmp_path))
    for ev in ACCOUNT_NAMED_SECURITY_EVENTS:
        srv.events.record("account_security", account="Corvid", event=ev)
    srv.events.record("account_security", ip="127.0.0.1", event="reconsent")

    named = {e["event"] for e in _of(srv, "account_security") if "account" in e}
    assert named == set(ACCOUNT_NAMED_SECURITY_EVENTS)
    assert "reconsent" not in named
    # The two lists differ by exactly the one event.
    assert (set(ACCOUNT_SECURITY_EVENTS) - set(ACCOUNT_NAMED_SECURITY_EVENTS)
            == {"reconsent"})


def test_a_staff_call_records_the_caller_and_nothing_else(world, clock, tmp_path):
    """The call TEXT is conversation, so it is not written down — here or
    anywhere. Who called is a fact, and the reason it is worth keeping: this is
    a rate-limited emergency channel, and abuse of it is a thing to see."""
    srv = TalkerServer(_cfg(tmp_path))
    wiz = FakeSession(world, name="Wizzy", clock=clock, level="wizard",
                      events=srv.events)
    a = FakeSession(world, name="Corvid", clock=clock, events=srv.events)
    dispatch(a, ".call_staff the ceiling is falling")
    rows = _of(srv, "staff_call")
    assert [e["account"] for e in rows] == ["Corvid"]
    assert set(rows[0]) == {"ts", "type", "account"}
    assert "ceiling" not in srv.events.path.read_text(encoding="utf-8")


# ---- journald is process-level only ----------------------------------------

@pytest.mark.asyncio
async def test_no_domain_fact_reaches_the_log(tmp_path, caplog):
    """THE point of the split: a fact lives in exactly one place. Drive several
    of the routed paths and assert the log carries none of them — not the IPs,
    not the names, not the old phrasings."""
    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Bob", "correct horse battery")
        with caplog.at_level(logging.INFO, logger="heresay"):
            # A refusal the loopback exemption does not cover: a banned NAME,
            # post-auth. (Banning 127.0.0.1 would no longer refuse anything.)
            srv.bans.ban("Bob", "name", hours=1)
            r, w = await asyncio.open_connection("127.0.0.1", port)
            await collect(r); await send(w, "Bob")
            await collect(r); await send(w, "correct horse battery")
            await collect(r, timeout=0.5)
            w.close()
            await asyncio.sleep(0.05)
        text = caplog.text.lower()
        for phrase in ("connection refused", "connection open", "connection close",
                       "resume login", "takeover", "staff call",
                       "re-consent", "recovery codes", "sustained flooding",
                       "wizard action", "auth failure", "self-deleted"):
            assert phrase not in text, phrase
        # ...and the fact IS in the store.
        assert [e["reason"] for e in _of(srv, "refused")] == ["banned_name"]
    finally:
        await srv.aclose()


def test_the_engine_emits_no_domain_facts_to_the_log_by_construction():
    """A source sweep, so a NEW `log.info` naming a person or an address fails
    here rather than in review. Every surviving one is process output: counts,
    paths, ports — things about the process, not about anybody."""
    import inspect
    import heresay.server as S
    import heresay.commands.staff as ST
    import heresay.reload as RL
    import heresay.mail as MA
    import heresay.revtell as RT
    import heresay.boxfiles as BF

    # v1.37.0 added three, and all three are the same kind of thing the rest are:
    # counts, paths, and a diff of FIELD NAMES and RANK WORDS. A reload line can
    # say ".gag staff → floor"; it can never say who anybody is.
    # ⚠ v1.44.0's `forbidden names:` earns its place the same way: a COUNT and a
    # PATH. The list's CONTENTS never reach the log — the grandfather warning is
    # a count too, deliberately (forbidden.py), so nothing here can name anybody.
    # ⚠ v1.52.5 put the two STORES under this sweep as well, and they are the
    # modules where it matters most: they are the only ones holding message
    # bodies at all. Their lines are the same kind of thing as the rest — a
    # COUNT of boxes and the PATHS they were split into, never a key and never
    # a word. (`boxfiles` names a FILE when it skips an unreadable one, which
    # is a path too, and warns rather than infos.)
    allowed = ("favicon:", "world:", "one-way passages:", "echoes:",
               "help topics:", "listener on", "purged ",
               "permissions:", "config watch:", "reload:", "forbidden names:",
               "mail:", "revtell:")
    for mod in (S, ST, RL, MA, RT, BF):
        src = inspect.getsource(mod)
        for line in src.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("log.info("):
                continue
            assert any(a in stripped for a in allowed), (mod.__name__, stripped)


def test_the_reader_treats_the_new_types_as_just_more_rows(tmp_path):
    """`.staff_actions` still shows only `staff_action`; the rest are recorded
    for views not yet written. The shared reader needs no per-type knowledge —
    which is what makes the next view a filter rather than a feature."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.events.record("refused", ip="1.1.1.1", reason="ip_banned")
    srv.events.record("staff_action", actor="W", action="gag", target="B")
    srv.events.record("takeover", account="C", from_ip="2.2.2.2")

    assert len(srv.events.read()) == 3                       # all of them
    assert len(srv.events.read(types=("staff_action",))) == 1
    assert {e["type"] for e in srv.events.read(types=("refused", "takeover"))} \
        == {"refused", "takeover"}
    # Newest-first and limit work the same whatever the type.
    assert srv.events.read(newest_first=True, limit=1)[0]["type"] == "takeover"


def test_account_security_events_are_the_documented_three():
    assert set(ACCOUNT_SECURITY_EVENTS) == {"password_reset_recovery",
                                            "recovery_codes_regenerated",
                                            "reconsent"}
