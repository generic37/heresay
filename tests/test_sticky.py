# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sticky 'current correspondent' shared by .tell/.remote (and >/< prefixes)."""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import NO_CORRESPONDENT, dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def accounts(tmp_path):
    """A REAL store — the misdelivery guard asks it whether a word is somebody's
    name, and the conftest fake deliberately echoes every name back as a match,
    which would make that question meaningless here."""
    store = AccountStore(tmp_path / "accounts.json")
    for n in ("Corvid", "Bob", "Generic"):
        store.create(n, "correct horse battery")
    return store


# ---- slot setting & defaulting -------------------------------------------

def test_explicit_tell_sets_slot_then_bare_word_defaults(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hello")
    assert a.resident.correspondent == "Bob"
    b.clear()
    dispatch(a, ".t hey")                       # "hey" unresolved -> defaults to Bob
    assert ">> Corvid says: hey" in b.text


def test_slot_is_shared_tell_then_remote(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hi")
    b.clear()
    dispatch(a, ".r waves")                     # remote defaults to the same slot
    assert ">> Corvid waves" in b.text


def test_slot_is_shared_remote_prefix_then_tell(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    al = FakeSession(world, name="Ada", clock=clock)
    dispatch(a, "<Ada bows")                  # remote via '<' sets the slot
    assert a.resident.correspondent == "Ada"
    al.clear()
    dispatch(a, ".t hey")                       # tell defaults to Ada
    assert ">> Corvid says: hey" in al.text


def test_resolving_first_word_wins_over_slot(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    c = FakeSession(world, name="Cara", clock=clock)
    dispatch(a, ".tell Bob hi")                 # slot = Bob
    b.clear(); c.clear()
    dispatch(a, ".t Cara over here")            # "Cara" resolves -> goes to Cara
    assert ">> Corvid says: over here" in c.text
    assert "over here" not in b.text
    assert a.resident.correspondent == "Cara"  # slot updated to Cara


def test_unresolvable_first_word_falls_through_whole_text(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hi")
    b.clear()
    dispatch(a, ".t will you come")             # no "Will" online -> whole text to Bob
    assert ">> Corvid says: will you come" in b.text


def test_naming_a_REAL_person_who_is_offline_never_falls_through(world, clock,
                                                                 accounts):
    """⛔ THE MISDELIVERY GUARD (v1.52.0), and it fixes an older bug than the
    release that found it.

    `.tell Wren hello` with Wren OFFLINE used to fall straight through to the
    sticky correspondent and send "Wren hello" to whoever you last spoke to — a
    private message read by somebody it was never addressed to. Dropping
    abbreviation made that far easier to reach, which is how it was noticed.

    The discriminator is whether the first word is somebody's NAME. If it is,
    they meant a person and are told that person is not here; if it is not, it
    is plainly the start of a sentence and the fallback does its job.
    """
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    b = FakeSession(world, name="Bob", clock=clock, accounts=accounts)
    dispatch(a, ".tell Bob hi")                # slot = Bob
    a.clear(); b.clear()
    # "Generic" is a real account in the `accounts` fixture, and is not online.
    dispatch(a, ".t Generic something private")
    assert "no one called" in a.text.lower()
    assert "something private" not in b.text, "it went to the wrong person"


def test_a_word_that_names_NOBODY_still_falls_through(world, clock, accounts):
    """⚠ The other half, and why the guard is not simply "refuse everything".

    `.t will you come` has to keep working — "will" is a word, not a botched
    name, and the fallback is the whole point of the sticky correspondent.
    """
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    b = FakeSession(world, name="Bob", clock=clock, accounts=accounts)
    dispatch(a, ".tell Bob hi")
    b.clear()
    dispatch(a, ".t will you come")
    assert ">> Corvid says: will you come" in b.text


# ---- no-slot / bare-with-slot messages -----------------------------------

def test_no_correspondent_messages(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    for line in (".t hey", ".t", "<waves", ".r hi", ">hey"):
        a.clear()
        dispatch(a, line)
        assert NO_CORRESPONDENT in a.text, line


def test_semicolon_is_emote_not_tell(world, clock):
    # ';' is the emote shortcut, not a private-message prefix: it reaches the
    # room, and it never sets or reads the sticky correspondent slot.
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hi")                # slot = Bob
    b.clear()
    dispatch(a, ";waves")
    assert "Corvid waves" in b.text            # the ROOM hears it (both are here)
    assert ">> Corvid says: waves" not in b.text
    assert a.resident.correspondent == "Bob"   # slot untouched


def test_private_payload_never_leads_with_whitespace(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    for line in ("> Bob   hi", ".tell Bob   hi"):
        b.clear()
        clock.t += 5
        dispatch(a, line)
        assert b.out[-1] == ">> Corvid says: hi", line
    for line in ("<Bob   waves", ".remote Bob   waves"):
        b.clear()
        clock.t += 5
        dispatch(a, line)
        assert b.out[-1] == ">> Corvid waves", line


def test_bare_with_slot(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hi")
    a.clear()
    dispatch(a, ".t")
    assert "Tell Bob what?" in a.text
    a.clear()
    dispatch(a, ".r")
    assert "Remote Bob what?" in a.text


# ---- offline correspondent, and what does NOT set the slot ---------------

def test_offline_correspondent_errors_but_keeps_slot(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hi")                # slot = Bob
    b.resident.session = None                  # Bob disconnects
    a.clear()
    dispatch(a, ".t hey")
    assert "no one called Bob online" in a.text
    assert a.resident.correspondent == "Bob"   # slot retained (may return)


def test_receiving_does_not_set_slot(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, ".tell Bob hi")
    assert b.resident.correspondent is None    # Bob received; his slot untouched


def test_failed_send_does_not_set_slot(world, clock):
    b = FakeSession(world, name="Bob", clock=clock)
    gil = FakeSession(world, name="Gil", clock=clock)
    gil.resident.gagged = True
    dispatch(gil, ".tell Bob hi")              # gagged -> blocked, not a success
    assert gil.resident.correspondent is None


# ---- cross-door + disconnect clears (real socket) ------------------------

@pytest.fixture
async def server(tmp_path):
    cfg = Config(
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
    srv = TalkerServer(cfg)
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


async def _collect(reader, timeout=0.4):
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


async def _send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _new(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name); await _collect(r)
    await _send(w, "yes"); await _collect(r); await _send(w, pw); await _collect(r)
    await _send(w, pw); await _collect(r)                 # agreement text
    await _send(w, "y"); await _collect(r, timeout=1.5)   # + recovery hashing
    return r, w


async def _login(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name); await _collect(r); await _send(w, pw)
    return r, w, await _collect(r)


@pytest.mark.asyncio
async def test_sticky_cross_door_and_cleared_on_reconnect(server):
    srv, port = server
    ra, wa = await _new(port, "Corvid", "corvidpw")
    rb, wb = await _new(port, "Bob")
    await _collect(ra)

    # Explicit tell sets the slot; a bare-word .t then defaults to Bob.
    await _send(wa, ".tell Bob first hello")
    assert ">> Corvid says: first hello" in await _collect(rb)
    await _send(wa, ".t and again")
    assert ">> Corvid says: and again" in await _collect(rb)

    # ';' is the emote shortcut across a real door: Bob (same room) hears it as
    # an emote, spacing after the prefix makes no difference, and the sticky
    # slot set above is untouched — so a later bare .t still reaches Bob.
    await _send(wa, ";waves")
    out = await _collect(rb)
    assert "Corvid waves" in out
    assert ">> Corvid says: waves" not in out
    await _send(wa, ";  waves")
    out = await _collect(rb)
    assert "Corvid waves" in out
    assert "Corvid  waves" not in out
    await _send(wa, ".t still here")
    assert ">> Corvid says: still here" in await _collect(rb)

    # Disconnect clears the slot: after re-login, .t has no correspondent.
    await _send(wa, ".quit")
    await _collect(ra); wa.close()
    await asyncio.sleep(0.2)
    ra2, wa2, _ = await _login(port, "Corvid", "corvidpw")
    await _send(wa2, ".t hey")
    assert NO_CORRESPONDENT in await _collect(ra2)

    for w in (wb, wa2):
        w.close()
