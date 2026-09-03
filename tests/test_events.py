# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The engine's operational event log — one JSONL store, ELEVEN types (v1.32.0).

What these tests protect:

  * every type round-trips: written, read back, right fields;
  * the closed type set AND its three sub-enums are pinned, not just documented;
  * ONE retention window trims every type, on append AND at startup;
  * a torn final line is skipped on read rather than raising — a log that will
    not open because its last byte is missing has failed at its only job;
  * the accountability-grade types are fsynced and the burst-prone ones are not;
  * ⛔ the covenant: no message content can reach the file, structurally, for
    EVERY type — the field scan and the live canary both sweep the whole set.
"""

import json
import os
import time
from pathlib import Path

import pytest

from heresay.commands import dispatch
from heresay.config import Config
from heresay.events import (escape_sample, ACCOUNT_NAMED_SECURITY_EVENTS,
                            ACCOUNT_SECURITY_EVENTS, CONNECTION_DIRS, DAY_SECONDS,
                            AUTH_CEILING_PHASES, DURABLE_TYPES, EVENT_TYPES,
                            REFUSAL_REASONS, EventStore)
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


class FakeWall:
    """An injectable wall clock, like the ban and mail stores use."""

    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def wall():
    return FakeWall()


@pytest.fixture
def store(tmp_path, wall):
    return EventStore(tmp_path / "events.jsonl", retention_days=10.0,
                      wall_clock=wall)


def _lines(path):
    return [l for l in Path(path).read_text(encoding="utf-8").split("\n") if l]


# ---- every declared type round-trips --------------------------------------

def test_every_declared_type_round_trips(store, wall):
    """The schema is the contract: `ts`, `type`, then facts."""
    facts = {
        "connection": dict(dir="open", ip="1.2.3.4"),
        "login": dict(account="Corvid", ip="1.2.3.4"),
        "auth_fail": dict(ip="5.6.7.8", phase="login"),
        "login_timeout": dict(ip="9.9.9.9"),
        "refused": dict(ip="1.1.1.1", reason="ip_banned"),
        "flood_disconnect": dict(ip="2.2.2.2"),
        "takeover": dict(account="Corvid", from_ip="3.3.3.3"),
        "account_security": dict(ip="1.1.1.1", event="reconsent"),
        "account_deleted": dict(name="Corvid"),
        "staff_call": dict(account="Bob"),
        "staff_mail": dict(account="Bob"),
        # ⛔ NO FACTS AT ALL, and that is the type's whole design (v1.57.0).
        "report_filed": dict(),
        "staff_action": dict(actor="Wizzy", action="jail", target="Bob"),
        "bad_frame": dict(ip="4.4.4.4", reason="too_many_lines",
                          sample="'a\\nb'"),
        "auth_ceiling": dict(phase="tripped", threshold=50, window=60.0,
                             cooldown=120.0),
    }
    assert set(facts) == set(EVENT_TYPES)          # the table stays covered
    for t, f in facts.items():
        store.record(t, **f)

    got = store.read()
    assert len(got) == len(EVENT_TYPES)
    for entry, (t, f) in zip(got, facts.items()):
        assert entry["type"] == t
        assert entry["ts"] == wall.t
        for k, v in f.items():
            if k == "sample":
                # ⛔ `sample` is the ONE field the store rewrites on the way in:
                # it is `repr()`-escaped and capped by `record()` rather than by
                # the caller, so no future call site can write raw attacker
                # bytes into a file somebody later `cat`s. Round-tripping it
                # unchanged would mean the escape had not happened.
                assert entry[k] == escape_sample(v)
                assert entry[k] != v
            else:
                assert entry[k] == v


def test_one_json_object_per_line(store):
    store.record("connection", dir="open", ip="1.2.3.4")
    store.record("connection", dir="close", ip="1.2.3.4")
    lines = _lines(store.path)
    assert len(lines) == 2
    for l in lines:
        assert isinstance(json.loads(l), dict)


def test_the_file_is_owner_only(tmp_path, wall):
    """It names IPs and accounts, so it gets the account file's permissions —
    from the moment it exists, not after the first write."""
    s = EventStore(tmp_path / "events.jsonl", wall_clock=wall)
    assert s.path.exists()                          # created up front
    assert (os.stat(s.path).st_mode & 0o777) == 0o600
    s.record("login_timeout", ip="1.1.1.1")
    assert (os.stat(s.path).st_mode & 0o777) == 0o600


def test_an_unknown_type_is_dropped_not_written(store, caplog):
    """The type set is CLOSED: a typo must not invent a category no view shows
    and no covenant test knows about."""
    with caplog.at_level("WARNING"):
        store.record("gossip", text="something")
    assert store.read() == []
    assert "gossip" in caplog.text                  # the name; never the payload


def test_reading_filters_and_orders(store, wall):
    for i in range(3):
        wall.t += 10
        store.record("staff_action", actor="Wizzy", action=f"a{i}", target="Bob")
    wall.t += 10
    store.record("connection", dir="open", ip="1.2.3.4")

    acts = store.read(types=("staff_action",))
    assert [e["action"] for e in acts] == ["a0", "a1", "a2"]        # oldest first
    newest = store.read(types=("staff_action",), newest_first=True)
    assert [e["action"] for e in newest] == ["a2", "a1", "a0"]
    assert [e["action"] for e in store.read(types=("staff_action",),
                                            newest_first=True, limit=2)] == ["a2", "a1"]


def test_file_position_breaks_a_timestamp_tie(store):
    """Two actions inside one clock tick are ordinary — a script, a fast pair of
    commands, a coarse clock. "Whichever the sort left first" is not an answer a
    moderation log may give: what was appended later happened later."""
    for action in ("gag", "ungag"):
        store.record("staff_action", actor="W", action=action, target="B")
    assert [e["action"] for e in store.read()] == ["gag", "ungag"]
    assert [e["action"] for e in store.read(newest_first=True)] == ["ungag", "gag"]


# ---- retention ------------------------------------------------------------

def test_retention_trims_on_append(tmp_path, wall):
    s = EventStore(tmp_path / "events.jsonl", retention_days=1.0, wall_clock=wall)
    s.record("connection", dir="open", ip="old")
    wall.t += 2 * DAY_SECONDS                        # the first one ages out
    s.record("connection", dir="open", ip="new")
    ips = [e["ip"] for e in s.read()]
    assert ips == ["new"]
    assert len(_lines(s.path)) == 1                  # really gone from the file


def test_retention_trims_at_startup(tmp_path, wall):
    """A house that was down for a fortnight must not come back holding a
    fortnight of facts."""
    path = tmp_path / "events.jsonl"
    first = EventStore(path, retention_days=1.0, wall_clock=wall)
    first.record("connection", dir="open", ip="old")
    assert len(_lines(path)) == 1

    wall.t += 5 * DAY_SECONDS
    second = EventStore(path, retention_days=1.0, wall_clock=wall)
    assert second.read() == []                       # trimmed by the constructor
    assert _lines(path) == []


def test_one_window_covers_every_type(tmp_path, wall):
    """ONE policy, deliberately: no per-type retention table."""
    s = EventStore(tmp_path / "events.jsonl", retention_days=1.0, wall_clock=wall)
    s.record("connection", dir="open", ip="1.1.1.1")
    s.record("auth_fail", ip="1.1.1.1", phase="login")
    s.record("login_timeout", ip="1.1.1.1")
    s.record("account_deleted", name="Gone")
    s.record("staff_action", actor="W", action="gag", target="B")
    assert len(s.read()) == 5
    wall.t += 2 * DAY_SECONDS
    s.record("connection", dir="close", ip="2.2.2.2")
    assert [e["type"] for e in s.read()] == ["connection"]      # only the new one


def test_a_zero_window_keeps_everything(tmp_path, wall):
    s = EventStore(tmp_path / "events.jsonl", retention_days=0, wall_clock=wall)
    s.record("connection", dir="open", ip="1.1.1.1")
    wall.t += 3650 * DAY_SECONDS
    s.record("connection", dir="close", ip="1.1.1.1")
    assert len(s.read()) == 2


def test_trim_does_not_rewrite_when_nothing_aged_out(store, wall):
    store.record("connection", dir="open", ip="1.1.1.1")
    before = store.path.stat().st_mtime_ns
    assert store.trim() == 0
    assert store.path.stat().st_mtime_ns == before


# ---- a torn final line ----------------------------------------------------

def test_a_torn_final_line_is_skipped_on_read(store):
    store.record("connection", dir="open", ip="1.1.1.1")
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1700000000.0, "type": "wizard_act')   # a crash mid-append
    got = store.read()
    assert len(got) == 1                              # the good line, no exception
    assert got[0]["ip"] == "1.1.1.1"


def test_an_append_after_a_torn_write_is_not_swallowed_by_it(store, wall):
    """The wrinkle this test found: a torn line does not end in a newline, so
    appending straight onto it would FUSE the fragment to the next event and lose
    a good record to an old accident. The writer starts a fresh line instead."""
    store.record("connection", dir="open", ip="keep")
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 170000')                     # a crash mid-append
    wall.t += 100
    store.record("connection", dir="close", ip="also-keep")
    assert [e["ip"] for e in store.read()] == ["keep", "also-keep"]


def test_a_torn_final_line_survives_a_trim(store, wall):
    """trim() must not become a quiet data-loss path for a half-written line —
    read tolerates it, and that is enough."""
    store.record("connection", dir="open", ip="old")
    wall.t += 100 * DAY_SECONDS
    store.record("connection", dir="close", ip="new")   # trims "old" away
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 170000')
    store.trim()
    assert '{"ts": 170000' in store.path.read_text(encoding="utf-8")


def test_a_wholly_empty_file_reads_as_nothing(store):
    assert store.read() == []
    assert store.count() == 0


# ---- durability -----------------------------------------------------------

def test_the_accountability_types_are_fsynced(store, monkeypatch):
    """On disk before the command that caused them returns. The others are cheap
    facts that arrive in bursts — a flush is enough, and fsyncing every refused
    connection on a busy door would be a lot of disk for very little."""
    synced = []
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(fd))
    # The cheap ones: flush only, however many arrive.
    for t, f in (("connection", dict(dir="open", ip="1.1.1.1")),
                 ("refused", dict(ip="1.1.1.1", reason="conn_cap")),
                 ("refused", dict(ip="1.1.1.1", reason="rate_limited")),
                 ("flood_disconnect", dict(ip="1.1.1.1")),
                 ("auth_fail", dict(ip="1.1.1.1", phase="login")),
                 ("login_timeout", dict(ip="1.1.1.1")),
                 ("staff_call", dict(account="Bob"))):
        store.record(t, **f)
    assert synced == []

    store.record("staff_action", actor="W", action="ban", target="B")
    store.record("account_security", ip="1.1.1.1", event="reconsent")
    store.record("takeover", account="W", from_ip="2.2.2.2")
    assert len(synced) == 3
    assert set(DURABLE_TYPES) == {"staff_action", "account_security", "takeover"}
    assert set(DURABLE_TYPES) <= set(EVENT_TYPES)


def test_a_write_failure_never_raises(tmp_path, wall, caplog):
    """A failed append must not take a session down with it."""
    s = EventStore(tmp_path / "events.jsonl", wall_clock=wall)
    s.path.unlink()
    s.path.mkdir()                                    # now un-appendable
    with caplog.at_level("WARNING"):
        s.record("connection", dir="open", ip="1.1.1.1")   # no exception
    assert "could not append" in caplog.text


def test_a_disabled_store_is_silent(tmp_path):
    s = EventStore(tmp_path / "events.jsonl", enabled=False)
    s.record("staff_action", actor="W", action="gag", target="B")
    assert s.read() == [] and s.trim() == 0
    assert not (tmp_path / "events.jsonl").exists()


# ---- ⛔ the covenant ------------------------------------------------------
#
# Same spirit as the mail-power guard: a structural check, not a hopeful one.

CONTENT_SHAPED = ("text", "body", "line", "message", "msg", "content",
                  "said", "says", "speech", "topic", "profile", "desc",
                  "description", "buffer", "review", "revtell", "mail")


def test_no_declared_type_carries_a_content_shaped_field(tmp_path, wall):
    """Every field the engine actually writes is a fact about who/where/what —
    never about what was SAID. Checked against what the code records, so a new
    field with a content-shaped name fails here rather than in production."""
    s = EventStore(tmp_path / "events.jsonl", wall_clock=wall)
    written = (
        ("connection", dict(dir="open", ip="1.2.3.4")),
        ("connection", dict(dir="resume", ip="1.2.3.4")),
        ("login", dict(account="Corvid", ip="1.2.3.4")),
        ("auth_fail", dict(ip="1.2.3.4", phase="login")),
        ("login_timeout", dict(ip="1.2.3.4")),
        ("refused", dict(ip="1.2.3.4", reason="ip_banned")),
        ("flood_disconnect", dict(ip="1.2.3.4")),
        ("takeover", dict(account="Corvid", from_ip="5.6.7.8")),
        # BOTH shapes: the named events and the de-named `reconsent` carry
        # different fields, so the scan must see each of them.
        ("account_security", dict(account="Corvid",
                                  event="password_reset_recovery")),
        ("account_security", dict(ip="1.2.3.4", event="reconsent")),
        ("account_deleted", dict(name="Corvid")),
        ("staff_call", dict(account="Corvid")),
        ("staff_mail", dict(account="Corvid")),
        # v1.57.0 — the emptiest row in the sweep, deliberately: a report has a
        # reporter, an accused and some words, and this records NONE of them.
        # The sweep still has to SEE it, or a later field added here would slip
        # in behind a type nobody was scanning.
        ("report_filed", dict()),
        ("staff_action", dict(actor="W", action="gag", target="B")),
        # ⛔ v1.52.0, and the ONE type that carries a fragment of what was sent.
        # `sample` is deliberately NOT in CONTENT_SHAPED: it is not content in
        # the covenant's sense — it fires only on a frame the engine has already
        # judged hostile and is closing the connection over, never on anything a
        # resident legitimately said. The canary test below is what keeps that
        # true; if it ever has to be relaxed, this exception has stopped being
        # narrow. See the reasoning beside BAD_FRAME_REASONS in events.py.
        ("bad_frame", dict(ip="1.2.3.4", reason="control_byte",
                           account="Corvid", sample="'x'")),
        # v1.53.0 — the server-wide ceiling's two edges. Nothing here CAN be
        # content: a phase word and three configured numbers.
        ("auth_ceiling", dict(phase="tripped", threshold=50, window=60.0,
                              cooldown=120.0)),
    )
    # The sweep is only worth what it covers: every declared type must appear.
    assert {t for t, _ in written} == set(EVENT_TYPES)
    for t, f in written:
        s.record(t, **f)
    for entry in s.read():
        for key in entry:
            assert key.lower() not in CONTENT_SHAPED, (entry["type"], key)


@pytest.mark.asyncio
async def test_nothing_anyone_says_reaches_the_event_file(tmp_path, world, clock):
    """The canary test, in the shape `.rev` already uses: speak, emote, think,
    shout, tell, set a topic, write a profile — then grep every byte the store
    holds for the words. A real server, a real file.
    """
    canary = "zarquon7-canary-phrase"
    cfg = Config(
        owner_name="Generic",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        events_file=str(tmp_path / "events.jsonl"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000,
    )
    srv = TalkerServer(cfg)
    seed_account(srv.accounts, "Wizzy", "password-1234")
    seed_account(srv.accounts, "Bob", "password-1234")

    a = FakeSession(srv.world, name="Wizzy", clock=clock, rank="owner",
                    events=srv.events, mail=srv.mail, state=srv.state,
                    accounts=srv.accounts, config=cfg)
    b = FakeSession(srv.world, name="Bob", clock=clock, events=srv.events,
                    mail=srv.mail, state=srv.state, accounts=srv.accounts,
                    config=cfg)

    dispatch(a, f"{canary} spoken aloud")
    dispatch(a, f".e emotes {canary}")
    dispatch(a, f".think {canary}")
    dispatch(a, f".shout {canary}")
    dispatch(a, f".tell Bob {canary}")
    dispatch(a, f".topic {canary}")
    dispatch(a, f".desc {canary}")
    dispatch(a, f".mail Bob {canary}")
    dispatch(a, f".mail_staff {canary}")
    # .call_staff RECORDS (v1.30.1) — the caller's name, never the call itself,
    # which is conversation and is therefore not written down anywhere.
    dispatch(b, f".call_staff {canary}")
    # ⛔ v1.57.0 — .report RECORDS now (a bare `report_filed`), so the canary has
    # to go through it: the type's promise is that the fact is written and the
    # words are not, and this is the line that proves the second half.
    dispatch(b, f".report Wizzy {canary}")
    # ...and a staff action, which also records — names, never words.
    dispatch(a, ".gag Bob")

    raw = srv.events.path.read_text(encoding="utf-8")
    assert canary not in raw
    # The facts ARE there — and are only facts.
    acts = srv.events.read(types=("staff_action",))
    assert [(e["actor"], e["action"], e["target"]) for e in acts] == \
        [("Wizzy", "gag", "Bob")]
    calls = srv.events.read(types=("staff_call",))
    assert [e["account"] for e in calls] == ["Bob"]
    assert all(set(e) == {"ts", "type", "account"} for e in calls)
    # .mail_staff records too (v1.35.0) — the writer's name and NOTHING else.
    # The note itself went into the letterbox, which is mail: read by the people
    # it was addressed to, never by a log.
    mails = srv.events.read(types=("staff_mail",))
    assert [e["account"] for e in mails] == ["Wizzy"]
    assert all(set(e) == {"ts", "type", "account"} for e in mails)


def test_the_type_table_is_the_documented_set():
    """Adding one is a deliberate act with a covenant question attached, so the
    table is pinned here as well as documented. v1.30.1 took it from five to ten,
    which is what emptying journald of domain facts cost."""
    assert EVENT_TYPES == (
        "connection", "login", "auth_fail", "login_timeout", "refused",
        "flood_disconnect", "takeover", "account_security", "account_deleted",
        "staff_call", "staff_mail",
        # v1.57.0 — a report is waiting. `{ts, type}` and NOTHING else, so an
        # external watcher can nudge the reader without the engine ever
        # reaching the network and without a word of the report leaving the box.
        "report_filed",
        "staff_action",
        # v1.52.0 — a frame a door judged hostile and hung up on. ⛔ The one type
        # carrying a fragment of what was sent; see events.py for why that is a
        # narrow exception rather than a loosened rule.
        "bad_frame",
        # v1.53.0 — the server-wide auth ceiling tripping or clearing. The one
        # notable security transition that used to exist only as journal prose.
        "auth_ceiling",
    )


def test_the_sub_enums_are_pinned_too():
    """A `reason`/`event`/`dir` word is as closed as the type set: a reader can
    enumerate what it might meet, and a typo is a value nobody filters on."""
    assert CONNECTION_DIRS == ("open", "close", "resume")
    assert REFUSAL_REASONS == ("ip_banned", "conn_cap", "banned_name",
                               "reserved_name", "owner_name", "rate_limited")
    assert ACCOUNT_SECURITY_EVENTS == ("password_reset_recovery",
                                       "recovery_codes_regenerated", "reconsent")
    assert AUTH_CEILING_PHASES == ("tripped", "cleared")
    # Which of them name an account is data too (v1.30.2): `reconsent` does not,
    # and the difference is exactly that one event.
    assert ACCOUNT_NAMED_SECURITY_EVENTS == ("password_reset_recovery",
                                             "recovery_codes_regenerated")
    from heresay.events import BAD_FRAME_REASONS
    assert BAD_FRAME_REASONS == ("control_byte", "too_many_lines", "too_large")
    assert set(ACCOUNT_SECURITY_EVENTS) - set(ACCOUNT_NAMED_SECURITY_EVENTS) \
        == {"reconsent"}
