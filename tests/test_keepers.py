# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Staff: the .call_staff call, the shared staff letterbox, .examine rank lines,
and the reserved names — including cross-door delivery and login notices."""

import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as ws_connect

import heresay.commands as C
from conftest import seed_account, COMMONS_THEME
from heresay.accounts import AccountStore
from heresay.commands import (dispatch, STAFF_BOX, keeper_called_msg,
                              keeper_refused_msg)

# The two staff-call lines take their level word from the ladder now (v1.27.0);
# FakeSession/the shipped server use the Commons theme, so these assert its
# configured "staff" wording.
KEEPER_CALLED_MSG = keeper_called_msg(COMMONS_THEME)
KEEPER_REFUSED = keeper_refused_msg(COMMONS_THEME)
from heresay.config import Config
from heresay.mail import MailStore
from heresay.server import TalkerServer

from conftest import FakeSession, FakeClock

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def store(tmp_path):
    s = AccountStore(tmp_path / "accounts.json")
    s.create("Generic", "pw")
    s.create("Wiz", "pw")
    s.set_rank("Wiz", "staff")
    s.create("Res", "pw")
    return s


@pytest.fixture
def mail(tmp_path):
    return MailStore(tmp_path / "mb.json", tmp_path / "mk.key")


# ---- .call_staff (call a staff) ---------------------------------------------

def test_keeper_reaches_all_online_staff_with_room(world, clock):
    res = FakeSession(world, name="Corvid", clock=clock)               # atrium
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    arch = FakeSession(world, name="Generic", clock=clock, level="arch")
    dispatch(res, ".call_staff the tinker won't stop shouting")
    for staff in (wiz, arch):
        assert "Corvid (atrium) calls for the staff: the tinker won't stop shouting" \
            in staff.text
    assert KEEPER_CALLED_MSG in res.text


def test_keeper_no_staff_online_fallback(world, clock):
    res = FakeSession(world, name="Corvid", clock=clock)   # the only one connected
    dispatch(res, ".call_staff help")
    assert "No staff awake just now. Leave staff mail: .mail_staff <text>" in res.text


def test_keeper_works_while_gagged(world, clock):
    res = FakeSession(world, name="Corvid", clock=clock)
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    res.resident.gagged = True
    dispatch(res, ".call_staff appeal please")
    assert "calls for the staff: appeal please" in wiz.text
    assert KEEPER_CALLED_MSG in res.text


def test_keeper_rate_limited(world):
    clock = FakeClock()
    res = FakeSession(world, name="Corvid", clock=clock)
    FakeSession(world, name="Wiz", clock=clock, level="wizard")
    dispatch(res, ".call_staff first")
    res.clear()
    dispatch(res, ".call_staff second")
    assert KEEPER_REFUSED in res.text
    clock.t = res.config.staff_call_seconds + 1
    res.clear()
    dispatch(res, ".call_staff third")
    assert KEEPER_CALLED_MSG in res.text


def test_keeper_empty_usage(world, clock):
    res = FakeSession(world, name="Corvid", clock=clock)
    dispatch(res, ".call_staff")
    assert "Usage: .call_staff <text>" in res.text


def test_keeper_records_the_fact_not_the_text(world, clock, caplog, tmp_path):
    """WHO called is kept; WHAT they said is not, anywhere.

    v1.30.1 moved the destination — this was a journald line naming a resident,
    and journald is process output now — but the property being protected is
    unchanged and is the one that matters: the call itself is conversation, so it
    is never written down. Both halves are asserted, in both places.
    """
    from heresay.events import EventStore
    events = EventStore(tmp_path / "events.jsonl")
    res = FakeSession(world, name="Corvid", clock=clock, events=events)
    FakeSession(world, name="Wiz", clock=clock, level="wizard", events=events)

    with caplog.at_level("INFO"):
        dispatch(res, ".call_staff a secret grievance")

    rows = events.read(types=("staff_call",))
    assert [e["account"] for e in rows] == ["Corvid"]     # the fact
    assert set(rows[0]) == {"ts", "type", "account"}      # and only the fact
    assert "grievance" not in events.path.read_text(encoding="utf-8")
    # Nothing at all in the process log — not the name, not the words.
    joined = " ".join(r.message for r in caplog.records)
    assert "Corvid" not in joined and "grievance" not in joined


def test_keeper_visible_to_all_levels(world, clock):
    # It is FOR residents: a resident typing it is never told "unknown command".
    res = FakeSession(world, name="Corvid", clock=clock)
    dispatch(res, ".call_staff x")
    assert "Unknown command" not in res.text


# ---- staff mail ---------------------------------------------------------

def test_mailkeeper_one_line_and_staff_read(world, clock, mail):
    res = FakeSession(world, name="Corvid", clock=clock, mail=mail)
    dispatch(res, ".mail_staff the well is dry again")
    assert "Left with the staff." in res.text
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard", mail=mail)
    dispatch(wiz, ".staffmail")
    assert "from Corvid" in wiz.text and "the well is dry again" in wiz.text


def test_mailkeeper_editor(world, clock, mail):
    res = FakeSession(world, name="Corvid", clock=clock, mail=mail)
    dispatch(res, ".mail_staff")
    assert res.in_editor()
    res.feed_editor("line one")
    res.feed_editor("line two")
    res.feed_editor(".end")
    assert "Left with the staff." in res.text
    assert mail.count(STAFF_BOX) == 1


def test_keepermail_and_dkeepermail_staff_only(world, clock, mail):
    # A resident cannot even see the staff verbs.
    res = FakeSession(world, name="Res", clock=clock, mail=mail, level="resident")
    dispatch(res, ".staffmail")
    assert "Unknown command: .staffmail" in res.text
    res.clear()
    dispatch(res, ".dstaffmail 1")
    assert "Unknown command: .dstaffmail" in res.text


def test_dkeepermail_clears_for_all_staff(world, clock, mail):
    sender = FakeSession(world, name="Corvid", clock=clock, mail=mail)
    dispatch(sender, ".mail_staff first thing")
    w1 = FakeSession(world, name="Wiz", clock=clock, level="wizard", mail=mail)
    w2 = FakeSession(world, name="Generic", clock=clock, level="arch", mail=mail)
    dispatch(w1, ".dstaffmail 1")
    assert "cleared for all" in w1.text
    w2.clear()
    dispatch(w2, ".staffmail")
    assert "No staff mail." in w2.text            # shared box: gone for everyone


def test_keeper_letterbox_full_message(world, clock, mail):
    res = FakeSession(world, name="Corvid", clock=clock, mail=mail)
    for _ in range(mail.cap):
        mail.deliver(STAFF_BOX, "Corvid", "x")
    res.clear()
    # A fresh mail token each call; fill happened directly, so the bucket is full.
    dispatch(res, ".mail_staff one too many")
    assert "The staff letterbox is full." in res.text


def test_keeper_mail_encrypted_at_rest(world, clock, tmp_path):
    from heresay import boxfiles
    mail = MailStore(tmp_path / "mb.json", tmp_path / "mk.key")
    res = FakeSession(world, name="Corvid", clock=clock, mail=mail)
    dispatch(res, ".mail_staff CANARYPHRASE42 in the raw bytes")
    # ⚠ One file per box since v1.52.5 — same two assertions, read from the box
    # files instead of the single store, with the box key as the FILENAME (still
    # plaintext, still what the purge reads).
    d = boxfiles.box_dir(tmp_path / "mb.json")
    raw = b"".join(p.read_bytes() for p in d.rglob("*") if p.is_file())
    assert b"CANARYPHRASE42" not in raw            # body is encrypted on disk
    assert STAFF_BOX in [k for k, _ in boxfiles.read_boxes(d)]


def test_keeper_mail_purge_participates(world, clock, tmp_path):
    mail = MailStore(tmp_path / "mb.json", tmp_path / "mk.key", purge_days=180)
    mail.deliver(STAFF_BOX, "Corvid", "old note", timestamp=0.0)   # epoch -> ancient
    removed = mail.purge(now=200 * 86400)
    assert removed == 1 and mail.count(STAFF_BOX) == 0


# ---- .examine rank lines -------------------------------------------------

def _examine(world, clock, store, target, cfg=None):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=store,
                    config=cfg or Config(owner_name="Generic", wrap_columns=1000))
    dispatch(s, f".examine {target}")
    return s.text


def test_examine_rank_lines(world, clock, store):
    assert "Their rank here is staff." in _examine(world, clock, store, "Wiz")
    assert "The owner of Commons." in _examine(world, clock, store, "Generic")
    res_text = _examine(world, clock, store, "Res")
    assert "their rank here is staff" not in res_text and "owner" not in res_text


# ---- live server: cross-door call, login notice, reserved names ----------

@pytest.fixture
async def server(tmp_path):
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0,
        ws_host="127.0.0.1", ws_port=0,
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
    srv = TalkerServer(cfg)
    await srv.start()
    plain_port = srv._servers[0].sockets[0].getsockname()[1]
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        yield srv, plain_port, ws_port
    finally:
        await srv.aclose()


async def sock_connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


async def sock_collect(reader, timeout=0.4):
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


async def sock_send(writer, text):
    writer.write((text + "\n").encode())
    await writer.drain()


async def sock_create(port, name, pw="swordfish-x9"):
    r, w = await sock_connect(port)
    await sock_collect(r)
    await sock_send(w, name)
    await sock_collect(r)
    await sock_send(w, "yes")
    await sock_collect(r)
    await sock_send(w, pw)
    await sock_collect(r)
    await sock_send(w, pw)
    await sock_collect(r)
    await sock_send(w, "y")
    return r, w, await sock_collect(r, timeout=1.0)


async def sock_login(port, name, pw="swordfish-x9"):
    r, w = await sock_connect(port)
    await sock_collect(r)
    await sock_send(w, name)
    await sock_collect(r)
    await sock_send(w, pw)
    return r, w, await sock_collect(r, timeout=1.0)


async def test_keeper_call_crosses_doors(server):
    srv, plain_port, ws_port = server
    # A staff on the web door, a resident on the socket door. The arch (Generic)
    # can only be REGISTERED from the local console now (C1), so seed the account
    # and log in on the web door — where it is staff by config-named rank.
    seed_account(srv.accounts, "Generic", "swordfish-x9")
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as kws:
        await _ws_login(kws, "Generic", "swordfish-x9")
        r, w, _ = await sock_create(plain_port, "Corvid")
        await sock_send(w, ".call_staff the badger is asleep in the teapot")
        assert "Your call has gone out to the staff." in await sock_collect(r)
        got = "\n".join(await _ws_collect(kws))
        assert "Corvid (atrium) calls for the staff: the badger is asleep in" in got
        w.close()


async def test_keeper_mail_login_notice_staff_only(server):
    srv, plain_port, ws_port = server
    # A resident leaves staff mail, then quits.
    r, w, _ = await sock_create(plain_port, "Res")
    await sock_send(w, ".mail_staff the courier is loitering")
    assert "Left with the staff." in await sock_collect(r)
    await sock_send(w, ".quit")
    await sock_collect(r)
    w.close()
    await asyncio.sleep(0.1)
    # The arch (staff) logs in and is told at login.
    r2, w2, intro = await sock_create(plain_port, "Generic")
    assert "You have staff mail." in intro
    w2.close()
    # A returning resident is NOT told.
    r3, w3, intro3 = await sock_login(plain_port, "Res")
    assert "You have staff mail." not in intro3
    w3.close()


@pytest.mark.parametrize("reserved", ["staff", "Staff", "owner", "OWNER"])
async def test_reserved_names_refused(server, reserved):
    """⚠ Still refused with the hardcode GONE (v1.44.0) and no
    `forbidden_names.txt` anywhere — because on Commons these are rank names,
    and every rank is reserved. That is the whole argument for retiring the
    constant, so it is asserted here rather than assumed."""
    srv, plain_port, ws_port = server
    assert not srv.forbidden_names                 # no operator list at all
    r, w = await sock_connect(plain_port)
    await sock_collect(r)
    await sock_send(w, reserved)
    out = await sock_collect(r)
    assert "That name isn't available." in out
    w.close()


# ---- ws helpers ----------------------------------------------------------

async def _ws_collect(ws, timeout=0.5):
    frames = []
    while True:
        try:
            f = await asyncio.wait_for(ws.recv(), timeout)
        except (asyncio.TimeoutError, Exception):
            break
        if isinstance(f, (bytes, bytearray)):
            f = f.decode(errors="replace")
        if f.startswith("\x01"):
            continue
        frames.append(f)
    return frames


async def _ws_login(ws, name, pw="swordfish-x9"):
    await _ws_collect(ws)          # greeting + name prompt
    await ws.send(name)
    await _ws_collect(ws)          # password prompt
    await ws.send(pw)
    return "\n".join(await _ws_collect(ws, timeout=1.0))


async def _ws_create(ws, name, pw="swordfish-x9"):
    await _ws_collect(ws)
    await ws.send(name)
    await _ws_collect(ws)
    await ws.send("yes")
    await _ws_collect(ws)
    await ws.send(pw)
    await _ws_collect(ws)
    await ws.send(pw)
    await _ws_collect(ws)
    await ws.send("y")
    await _ws_collect(ws)               # the remember-me question (web door)
    await ws.send("n")
    return "\n".join(await _ws_collect(ws, timeout=1.0))
