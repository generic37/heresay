# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""refusal: — the per-room denial line a resident sees at a wizard_only door.

The backroom's wording ("The door does not know you.") lives in world/rooms/backroom.txt
now, not in code; a wizard_only room that declares no `refusal:` falls back to the
neutral built-in. Same line for both entry routes, .go and .join.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.commands import REFUSAL_DEFAULT, dispatch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.world import load_world

from conftest import FakeSession
from test_world import write_world, HALL
from test_integration import collect, login_new, send

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"
DECK_REFUSAL = "The door does not know you."


# ---- the neutral default (command layer) ----------------------------------

def _wizard_only_world(tmp_path, cave_header):
    return load_world(write_world(tmp_path, {
        "hall.txt": HALL,
        "cave.txt": f"name: cave\nexits: hall\n{cave_header}\n\nA cave.\n",
    }), "hall")


def test_a_staff_only_room_without_refusal_uses_the_neutral_default(tmp_path, clock):
    world = _wizard_only_world(tmp_path, "staff_only: yes")
    r = FakeSession(world, name="Ada", room="hall", clock=clock)
    r.clear()
    dispatch(r, ".go cave")
    assert r.resident.room_id == "hall"                # turned away
    assert r.out[-1] == REFUSAL_DEFAULT == "You may not enter."


def test_a_staff_only_room_uses_its_own_refusal_when_it_has_one(tmp_path, clock):
    world = _wizard_only_world(tmp_path, "staff_only: yes\nrefusal: keep out, mortal.")
    r = FakeSession(world, name="Ada", room="hall", clock=clock)
    r.clear()
    dispatch(r, ".go cave")
    assert r.out[-1] == "keep out, mortal."


def test_staff_are_not_refused(tmp_path, clock):
    world = _wizard_only_world(tmp_path, "staff_only: yes\nrefusal: keep out, mortal.")
    wiz = FakeSession(world, name="Wiz", room="hall", clock=clock, level="wizard")
    dispatch(wiz, ".go cave")
    assert wiz.resident.room_id == "cave"              # a staff walks in


# ---- the shipped backroom, over a real socket ---------------------------------

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


@pytest.mark.asyncio
async def test_go_deck_is_refused_in_the_decks_own_words(server):
    srv, port = server
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        await login_new(r, w, "Ada")
        await send(w, ".go backroom")
        assert DECK_REFUSAL in await collect(r)
        await send(w, ".quit")
        await collect(r)
    finally:
        w.close()


@pytest.mark.asyncio
async def test_join_into_deck_is_refused_the_same_way(server):
    """The arch stands on the backroom and invites a resident; the resident's .join
    is refused with the same line as .go."""
    srv, port = server
    ra, wa = await asyncio.open_connection("127.0.0.1", port)   # the arch, on backroom
    rb, wb = await asyncio.open_connection("127.0.0.1", port)   # the resident
    try:
        await login_new(ra, wa, "Generic")     # owner_name -> arch, staff
        await send(wa, ".go backroom")
        await collect(ra)
        await login_new(rb, wb, "Bob")
        await send(wa, ".invite Bob")
        await collect(ra); await collect(rb)
        await send(wb, ".join Generic")
        assert DECK_REFUSAL in await collect(rb)
        for wr, rd in ((wa, ra), (wb, rb)):
            await send(wr, ".quit")
            await collect(rd)
    finally:
        wa.close()
        wb.close()
