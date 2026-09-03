# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.ignore` — the per-resident category noise filter (1.16.0).

`.mute` filters a PERSON; `.ignore` filters a KIND OF LINE. Each category hides
for the person who set it and for nobody else, which is what these tests check:
every assertion pairs "hidden for the caller" with "still shown to an unset
resident standing right there".

Two things always show, and they are the interesting part: a staff arrival into
a LOCKED room, and the AFK notice you get on your own .tell.
"""

import json

import pytest

from heresay.accounts import AccountStore
from heresay.commands import IGNORE_CATEGORIES, IGNORE_SETTING, dispatch
from heresay.state import StateStore
from heresay.world import load_world

from conftest import FakeSession, WORLD


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw")
    return store


def _sess(world, clock, name="Corvid", **kw):
    return FakeSession(world, name=name, clock=clock, **kw)


# ---- the command surface -------------------------------------------------

def test_bare_ignore_lists_every_category_as_one_unit(world, clock, accounts):
    s = _sess(world, clock, accounts=accounts)
    dispatch(s, ".ignore")
    assert len(s.out) == 1                       # one screen, one unit
    for cat in IGNORE_CATEGORIES:
        assert cat in s.text, cat


def test_everything_shows_by_default(world, clock, accounts):
    s = _sess(world, clock, accounts=accounts)
    assert s.resident.ignores == {}
    for cat in IGNORE_CATEGORIES:
        assert s.resident.hears_category(cat), cat


@pytest.mark.parametrize("cat", sorted(IGNORE_CATEGORIES))
def test_set_and_unset_one_category(world, clock, accounts, cat):
    s = _sess(world, clock, accounts=accounts)
    dispatch(s, f".ignore {cat} yes")
    assert s.resident.ignores.get(cat) is True
    assert not s.resident.hears_category(cat)
    dispatch(s, f".ignore {cat} no")
    assert cat not in s.resident.ignores
    assert s.resident.hears_category(cat)


def test_an_unknown_category_is_refused(world, clock, accounts):
    s = _sess(world, clock, accounts=accounts)
    dispatch(s, ".ignore everything yes")
    assert "No such category" in s.text
    assert s.resident.ignores == {}


def test_a_non_yes_no_value_is_refused(world, clock, accounts):
    s = _sess(world, clock, accounts=accounts)
    dispatch(s, ".ignore shout maybe")
    assert "Usage: .ignore" in s.text
    assert s.resident.ignores == {}


def test_a_bare_category_is_refused(world, clock, accounts):
    """`.ignore shout` alone is ambiguous — it could mean "show me" or "hide it".
    Refusing beats guessing wrong in the direction that hides things."""
    s = _sess(world, clock, accounts=accounts)
    dispatch(s, ".ignore shout")
    assert "Usage: .ignore" in s.text
    assert s.resident.ignores == {}


def test_it_persists_across_restart(world, clock, tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    store.create("Corvid", "pw")
    s = _sess(world, clock, accounts=store)
    dispatch(s, ".ignore shout yes")
    store.flush()

    reloaded = AccountStore(path)                # a restart
    stored = reloaded.get("Corvid").settings[IGNORE_SETTING]
    assert stored == {"shout": True}
    # ...and it survives the JSON round trip as a real dict.
    assert json.loads(path.read_text())["accounts"]["corvid"]["settings"][
        IGNORE_SETTING] == {"shout": True}


def test_it_is_self_only(world, clock, accounts):
    mine = _sess(world, clock, accounts=accounts)
    theirs = _sess(world, clock, name="Wren", accounts=accounts)
    dispatch(mine, ".ignore shout yes")
    assert theirs.resident.ignores == {}
    assert theirs.resident.hears_category("shout")


# ---- enforcement, category by category -----------------------------------

def test_shout_is_hidden_for_the_ignorer_only(world, clock, accounts):
    shouter = _sess(world, clock, name="Loud", accounts=accounts)
    deaf = _sess(world, clock, name="Corvid", accounts=accounts)
    hearing = _sess(world, clock, name="Wren", accounts=accounts)
    dispatch(deaf, ".ignore shout yes")
    deaf.clear(); hearing.clear()
    dispatch(shouter, ".shout the tarts are gone")
    assert "the tarts are gone" not in deaf.text
    assert "Loud shouts: the tarts are gone" in hearing.text


def test_a_shouter_always_sees_their_own_shout(world, clock, accounts):
    """Your own copy IS the confirmation you were heard — the same rule as say
    and tell. Ignoring shouts must not make your own vanish."""
    s = _sess(world, clock, name="Loud", accounts=accounts)
    dispatch(s, ".ignore shout yes")
    s.clear()
    dispatch(s, ".shout still here")
    assert s.text.count("Loud shouts: still here") == 1


def test_room_arrival_and_departure_are_hidden_for_the_ignorer_only(
        world, clock, accounts):
    mover = _sess(world, clock, name="Mover", accounts=accounts)
    deaf = _sess(world, clock, name="Corvid", accounts=accounts)
    hearing = _sess(world, clock, name="Wren", accounts=accounts)
    dispatch(deaf, ".ignore exit_msg yes")
    deaf.clear(); hearing.clear()
    dispatch(mover, ".go oak")
    assert "slips out" not in deaf.text
    assert "Mover slips out toward the oak." in hearing.text


def test_room_entry_is_hidden_for_the_ignorer_only(world, clock, accounts):
    mover = _sess(world, clock, name="Mover", accounts=accounts)
    deaf = _sess(world, clock, name="Corvid", room="oak", accounts=accounts)
    hearing = _sess(world, clock, name="Wren", room="oak", accounts=accounts)
    dispatch(deaf, ".ignore enter_msg yes")
    deaf.clear(); hearing.clear()
    dispatch(mover, ".go oak")
    assert "Mover arrives." not in deaf.text
    assert "Mover arrives." in hearing.text


def test_afk_room_line_is_hidden_for_the_ignorer_only(world, clock, accounts):
    goer = _sess(world, clock, name="Goer", accounts=accounts)
    deaf = _sess(world, clock, name="Corvid", accounts=accounts)
    hearing = _sess(world, clock, name="Wren", accounts=accounts)
    dispatch(deaf, ".ignore afk_msg yes")
    deaf.clear(); hearing.clear()
    dispatch(goer, ".afk feeding the badger")
    assert "Goer is AFK" not in deaf.text
    assert "Goer is AFK: feeding the badger" in hearing.text


def test_the_tell_time_afk_notice_always_shows(world, clock, accounts):
    """ALWAYS-SHOW #2. The AFK note on your own .tell answers a command you just
    typed; hiding it would silently remove the reason your tell goes unanswered.
    `.ignore afk_msg` is about ambient noise, and that line is not ambient."""
    sender = _sess(world, clock, name="Corvid", accounts=accounts)
    away = _sess(world, clock, name="Wren", accounts=accounts)
    dispatch(away, ".afk feeding the badger")
    dispatch(sender, ".ignore afk_msg yes")
    sender.clear()
    dispatch(sender, ".tell Wren you there?")
    assert "Wren is AFK: feeding the badger" in sender.text


def test_atmospherics_are_hidden_for_the_ignorer_only(world, clock, accounts):
    """The echo goes out with no speaker (so no mute can hide it) — but a
    person's own `.ignore atmospherics` still can, because that is a choice
    about noise rather than about who is speaking."""
    deaf = _sess(world, clock, name="Corvid", accounts=accounts)
    hearing = _sess(world, clock, name="Wren", accounts=accounts)
    dispatch(deaf, ".ignore atmospherics yes")
    deaf.clear(); hearing.clear()
    world.broadcast_room(world.start_room, None, "Somewhere, a clock ticks.",
                         dim=False, category="atmospherics")
    assert "clock ticks" not in deaf.text
    assert "Somewhere, a clock ticks." in hearing.text


# ---- ALWAYS-SHOW #1: staff into a locked room ----------------------------

def test_staff_arrival_into_a_locked_room_always_shows(world, clock, accounts):
    """A shut door is people choosing to be alone. Staff passing through it is
    the one arrival nobody may filter away — 'no invisible watching' holds
    against the noise filter too."""
    occupant = _sess(world, clock, name="Corvid", room="cabin", accounts=accounts)
    dispatch(occupant, ".ignore enter_msg yes")
    dispatch(occupant, ".lock")
    assert world.is_locked("cabin")
    staff = _sess(world, clock, name="Wizzy", level="wizard", accounts=accounts)
    occupant.clear()
    dispatch(staff, ".go cabin")
    assert "Wizzy arrives." in occupant.text


def test_an_ordinary_arrival_into_an_UNlocked_room_is_still_filtered(
        world, clock, accounts):
    """The exception is the LOCK, not the rank: a staff walking into an open
    room is ordinary movement and obeys the filter like anyone."""
    occupant = _sess(world, clock, name="Corvid", room="cabin", accounts=accounts)
    dispatch(occupant, ".ignore enter_msg yes")
    staff = _sess(world, clock, name="Wizzy", level="wizard", accounts=accounts)
    occupant.clear()
    dispatch(staff, ".go cabin")
    assert "Wizzy arrives." not in occupant.text


# ---- moderation lines are NOT filterable ---------------------------------

def test_jail_and_free_room_lines_are_not_filtered(tmp_path, clock):
    """`.ignore enter_msg`/`exit_msg` cover ordinary movement. A jailing and a
    release are moderation facts the room should see, in the same spirit as the
    locked-room exception."""
    w = load_world(WORLD, "atrium", "holding_cell")
    st = StateStore(tmp_path / "state.json")
    staff = FakeSession(w, name="Wizzy", level="wizard", clock=clock, state=st)
    watcher = FakeSession(w, name="Corvid", clock=clock, state=st)
    culprit = FakeSession(w, name="Shady", clock=clock, state=st)
    dispatch(watcher, ".ignore enter_msg yes")
    dispatch(watcher, ".ignore exit_msg yes")
    watcher.clear()
    dispatch(staff, ".jail Shady")
    assert "Shady is hauled away" in watcher.text
    watcher.clear()
    dispatch(staff, ".free Shady")
    assert "Shady arrives." in watcher.text


# ---- login / logout, over a real door ------------------------------------
#
# The two world-wide categories cannot be exercised on a fake session: the line
# is emitted by the server's presence path, not by a command handler. So these go
# through a real listener, the way a resident actually meets them.

import asyncio

from heresay.config import Config
from heresay.server import TalkerServer

from conftest import seed_account

STRONG = "brass-otter-hum-9"


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
        # These tests need four people on the box at once (a staff, an ignorer,
        # a control, and the person coming and going), so the per-IP caps have to
        # be lifted — every connection here is 127.0.0.1.
        max_conns_per_ip=20,
        churn_max_in_window=50,
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


async def _start(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def _c(r, t=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(4096), t)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _s(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _new(port, name, pw=STRONG):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _c(r); await _s(w, name)
    await _c(r); await _s(w, "yes")
    await _c(r); await _s(w, pw)
    await _c(r); await _s(w, pw)
    await _c(r); await _s(w, "y")
    await _c(r, 1.5)
    return r, w


async def _login(port, name, pw=STRONG):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _c(r); await _s(w, name)
    await _c(r); await _s(w, pw)
    await _c(r, 1.5)
    return r, w


@pytest.mark.asyncio
async def test_login_and_logout_hidden_for_the_ignorer_only(tmp_path):
    srv, port = await _start(_cfg(tmp_path))
    try:
        rd, wd = await _new(port, "Deaf")
        rh, wh = await _new(port, "Hearing")
        await _c(rd)
        await _s(wd, ".ignore login_msg yes"); await _c(rd)
        await _s(wd, ".ignore logout_msg yes"); await _c(rd)
        await _c(rh)

        rb, wb = await _new(port, "Bram")           # a login
        deaf, hearing = await _c(rd, 1.0), await _c(rh, 1.0)
        assert "[Bram enters the world]" not in deaf
        assert "[Bram enters the world]" in hearing

        await _s(wb, ".quit"); await _c(rb); wb.close()
        deaf, hearing = await _c(rd, 1.0), await _c(rh, 1.0)
        assert "[Bram leaves the world]" not in deaf
        assert "[Bram leaves the world]" in hearing
        for w in (wd, wh):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_jailed_login_is_hidden_by_the_login_msg_ignore(tmp_path):
    """The maintainer's call, and the reason login_msg covers both: a jailed login is
    still a login. Someone who has switched logins off should not find the jail
    ones leaking through a second category they never turned on."""
    seed_account(AccountStore(tmp_path / "accounts.json"), "Wizzy", STRONG)
    srv, port = await _start(_cfg(tmp_path, owner_name="Wizzy"))
    try:
        rw, ww = await _login(port, "Wizzy")            # the staff who jails
        rd, wd = await _new(port, "Deaf")
        rh, wh = await _new(port, "Hearing")
        await _c(rw)
        await _s(wd, ".ignore login_msg yes"); await _c(rd)

        rb, wb = await _new(port, "Shady")
        await _c(rw); await _c(rd); await _c(rh)
        await _s(ww, ".jail Shady"); await _c(rw); await _c(rb)
        await _s(wb, ".quit"); await _c(rb); wb.close()
        await _c(rw); await _c(rd); await _c(rh)

        rb2, wb2 = await _login(port, "Shady")          # the jailed relog
        deaf, hearing = await _c(rd, 1.0), await _c(rh, 1.0)
        assert "[*** Shady arrives (jail) ***]" in hearing   # the unset resident sees it
        assert "Shady" not in deaf                          # the ignorer sees nothing
        for w in (ww, wd, wh, wb2):
            w.close()
    finally:
        await srv.aclose()


# ---- tab_alert: the resident's own opt-out for the web tab mark (1.19.0) ----
#
# The odd category. The other seven are enforced HERE, at delivery; this one is
# enforced in the browser, because the thing it turns off is a browser thing. So
# the server's whole job is to remember the setting and to TELL the web client —
# at login, and again on every change. On a socket door there is no tab to mark,
# so storing it is a harmless no-op and no frame is ever sent.

from heresay.transport import CTRL_TAB_ALERT


def test_tab_alert_is_a_category_and_defaults_to_shown(world, clock, accounts):
    s = _sess(world, clock, accounts=accounts)
    assert "tab_alert" in IGNORE_CATEGORIES
    dispatch(s, ".ignore")
    assert "tab_alert" in s.text
    assert s.resident.hears_category("tab_alert")     # default: the mark is on


def test_setting_it_pushes_a_control_frame_to_a_web_session(world, clock, accounts):
    """The push carries the whole client picture (v1.22.0 added the house bounds,
    title and favicon), so assert on the tab_alert frame within it rather than on
    the list being exactly one long."""
    s = _sess(world, clock, accounts=accounts)
    s.controls.clear()
    dispatch(s, ".ignore tab_alert yes")
    assert CTRL_TAB_ALERT + "off" in s.controls, s.controls
    s.controls.clear()
    dispatch(s, ".ignore tab_alert no")
    assert CTRL_TAB_ALERT + "on" in s.controls, s.controls


def test_the_frame_is_never_output(world, clock, accounts):
    """A control frame is not a line: it must not reach the screen."""
    s = _sess(world, clock, accounts=accounts)
    s.clear()
    dispatch(s, ".ignore tab_alert yes")
    assert "\x01" not in s.text
    assert "tab_alert: hidden." in s.text          # the ordinary confirmation


def test_a_socket_session_gets_no_frame_but_still_stores_it(world, clock, accounts):
    """No tab to mark on a socket door — the setting is remembered (it follows
    the account to a browser) and simply never pushed."""
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts,
                    supports_control_frames=False)
    dispatch(s, ".ignore tab_alert yes")
    assert s.controls == []                        # nothing on the wire
    assert s.resident.ignores.get("tab_alert") is True      # but remembered
    assert accounts.get("Corvid").settings[IGNORE_SETTING] == {"tab_alert": True}


def test_the_other_categories_push_nothing(world, clock, accounts):
    """Only the client-side ones are pushed; the delivery filters are enforced
    server-side and have nothing to tell the browser."""
    s = _sess(world, clock, accounts=accounts)
    for cat in ("shout", "login_msg", "enter_msg", "atmospherics"):
        s.controls.clear()
        dispatch(s, f".ignore {cat} yes")
        assert s.controls == [], cat


def test_a_bad_tab_alert_value_is_refused_and_pushes_nothing(world, clock, accounts):
    s = _sess(world, clock, accounts=accounts)
    s.controls.clear()
    dispatch(s, ".ignore tab_alert maybe")
    assert "Usage: .ignore" in s.text
    assert s.controls == []
    assert s.resident.ignores == {}


def test_it_persists_across_a_restart(world, clock, tmp_path):
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    store.create("Corvid", "pw")
    s = _sess(world, clock, accounts=store)
    dispatch(s, ".ignore tab_alert yes")
    store.flush()

    reloaded = AccountStore(path)                  # a restart
    assert reloaded.get("Corvid").settings[IGNORE_SETTING] == {"tab_alert": True}


# ---- `default` on .ignore (v1.25.0) --------------------------------------

def test_a_category_can_be_reset_to_its_default(world, accounts):
    s = FakeSession(world, name="Corvid", accounts=accounts)
    dispatch(s, ".ignore shout yes")
    assert s.resident.ignores.get("shout") is True
    s.clear()
    dispatch(s, ".ignore shout default")
    assert "shout" not in s.resident.ignores
    assert "default" in s.text                     # says what it did


def test_ignore_default_resets_every_category(world, accounts):
    s = FakeSession(world, name="Corvid", accounts=accounts)
    for cat in ("shout", "atmospherics", "login_msg"):
        dispatch(s, f".ignore {cat} yes")
    assert len(s.resident.ignores) == 3
    dispatch(s, ".ignore default")
    assert s.resident.ignores == {}
    assert accounts.get("Corvid").settings["ignore"] == {}


def test_default_means_shown_for_every_category(world, accounts):
    """No category has a house-configurable default — unlike `.set`, where
    default means the HOUSE value. Here it is always the built-in "no"."""
    from heresay.commands.settings import IGNORE_CATEGORIES
    s = FakeSession(world, name="Corvid", accounts=accounts)
    for cat in IGNORE_CATEGORIES:
        dispatch(s, f".ignore {cat} yes")
        dispatch(s, f".ignore {cat} default")
        assert cat not in s.resident.ignores, cat


def test_a_bad_value_is_still_refused(world, accounts):
    s = FakeSession(world, name="Corvid", accounts=accounts)
    dispatch(s, ".ignore shout maybe")
    assert "Usage" in s.text
    assert "shout" not in s.resident.ignores


def test_resetting_tab_alert_repushes_it_to_the_client(world, accounts):
    """It is enforced in the browser, so the browser has to be told — on a
    reset exactly as on a set."""
    s = FakeSession(world, name="Corvid", accounts=accounts)
    dispatch(s, ".ignore tab_alert yes")
    s.controls.clear()
    dispatch(s, ".ignore tab_alert default")
    assert any(c.startswith("\x01tab_alert") for c in s.controls), s.controls


def test_ignore_default_repushes_the_client_settings(world, accounts):
    s = FakeSession(world, name="Corvid", accounts=accounts)
    dispatch(s, ".ignore tab_alert yes")
    s.controls.clear()
    dispatch(s, ".ignore default")
    assert any(c.startswith("\x01tab_alert") for c in s.controls), s.controls
