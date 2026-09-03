# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Custom comings-and-goings, under .set: enter_msg / exit_msg / afk_msg /
login_msg / logout_msg.

Each is one line of the resident's own words, stored on the account and rendered
to whichever room should hear it. An unset one falls through to the house default
(`.ownerset`, live), which is never stored.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore, migrate_settings
from heresay.commands import (
    AFK_DEFAULT, MSG_COMMANDS, MSG_MAX, clean_message, dispatch, resident_message,
)
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import HOUSE_DEFAULTS

from conftest import FakeClock, FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
VERBS = ["enter_msg", "exit_msg", "afk_msg"]


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw")
    return store


def _session(world, accounts, clock, name="Corvid"):
    return FakeSession(world, name=name, clock=clock, accounts=accounts)


# ---- show / set / reset --------------------------------------------------

@pytest.mark.parametrize("verb", VERBS)
def test_bare_shows_the_house_default(world, accounts, clock, verb):
    s = _session(world, accounts, clock)
    dispatch(s, f".set {verb}")
    assert "house default" in s.text
    assert HOUSE_DEFAULTS[verb] in s.text


@pytest.mark.parametrize("verb", VERBS)
def test_set_then_show(world, accounts, clock, verb):
    s = _session(world, accounts, clock)
    dispatch(s, f".set {verb} does a small careful thing.")
    assert s.resident.messages[verb] == "does a small careful thing."
    s.clear()
    dispatch(s, f".set {verb}")
    # Showing one previews how it will actually read.
    shape = MSG_COMMANDS[verb][1]
    assert shape.format(name="Corvid", text="does a small careful thing.") in s.text
    assert "house default" not in s.text


def test_showing_afk_msg_previews_the_afk_wording(world, accounts, clock):
    # Four of the five follow the name directly; .afk reads differently, and the
    # preview says so rather than implying "Corvid gone to see about...".
    s = _session(world, accounts, clock)
    dispatch(s, ".set afk_msg gone to see about a badger")
    assert "Corvid is AFK: gone to see about a badger" in s.text
    s.clear()
    dispatch(s, ".set enter_msg wanders in")
    assert "Corvid wanders in" in s.text


@pytest.mark.parametrize("verb", VERBS)
def test_default_resets_and_forgets_the_key(world, accounts, clock, verb):
    s = _session(world, accounts, clock)
    dispatch(s, f".set {verb} something of my own.")
    assert accounts.get("Corvid").settings.get(verb) == "something of my own."
    dispatch(s, f".set {verb} default")
    assert verb not in s.resident.messages
    # Forgotten, not overwritten with the default — so the house wording can
    # still move later without dragging everyone's stored copy along.
    assert verb not in accounts.get("Corvid").settings
    assert resident_message(s, s.resident, verb) == HOUSE_DEFAULTS[verb]


@pytest.mark.parametrize("verb", VERBS)
def test_persists_across_relogin(world, accounts, clock, verb):
    s = _session(world, accounts, clock)
    dispatch(s, f".set {verb} comes back around.")
    acct = accounts.get("Corvid")
    # A fresh session loads the way the server's _load_prefs does.
    fresh = FakeSession(world, name="Corvid2", clock=clock, accounts=accounts)
    fresh.resident.messages = {
        k: acct.settings[k] for k in MSG_COMMANDS if isinstance(acct.settings.get(k), str)
    }
    assert resident_message(fresh, fresh.resident, verb) == "comes back around."


# ---- cleaning ------------------------------------------------------------

def test_cap_is_sixty_characters(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set enter_msg " + "x" * 200)
    stored = s.resident.messages["enter_msg"]
    assert len(stored) == MSG_MAX == 60
    assert stored == "x" * 60


def test_control_characters_are_stripped(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set enter_msg arrives\x07\x00 with a \x1b[31mbang\x1b.")
    stored = s.resident.messages["enter_msg"]
    assert stored == "arrives with a [31mbang."
    assert not any(ord(c) < 32 for c in stored)


def test_newlines_and_runs_collapse_to_one_line(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set enter_msg  strolls \n\n in    slowly. ")
    assert s.resident.messages["enter_msg"] == "strolls in slowly."


def test_clean_message_directly():
    assert clean_message("  a \t b \n c  ") == "a b c"
    assert clean_message("\x00\x01\x02") == ""
    assert len(clean_message("y" * 99)) == MSG_MAX


# ---- movement announcements ---------------------------------------------

def test_default_movement_wording_is_unchanged(world, accounts, clock):
    a = _session(world, accounts, clock)
    watcher = FakeSession(world, name="Wren", clock=clock)          # atrium
    dest = FakeSession(world, name="Bob", room="oak", clock=clock)
    dispatch(a, ".go oak")
    assert "Corvid slips out toward the oak." in watcher.text      # names the room
    assert "Corvid arrives." in dest.text


def test_custom_exit_msg_replaces_the_whole_line_and_hides_the_destination(
        world, accounts, clock):
    a = _session(world, accounts, clock)
    watcher = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".set exit_msg melts into the hedge.")
    watcher.clear()
    dispatch(a, ".go oak")
    assert "Corvid melts into the hedge." in watcher.text
    assert "oak" not in watcher.text                 # the mystery is the point
    assert "slips out" not in watcher.text


def test_custom_enter_msg_greets_the_destination_only(world, accounts, clock):
    a = _session(world, accounts, clock)
    origin = FakeSession(world, name="Wren", clock=clock)
    dest = FakeSession(world, name="Bob", room="oak", clock=clock)
    dispatch(a, ".set enter_msg unfolds out of the air.")
    origin.clear(); dest.clear()
    dispatch(a, ".go oak")
    assert "Corvid unfolds out of the air." in dest.text
    assert "unfolds" not in origin.text               # the origin hears the exit
    assert "Corvid slips out toward the oak." in origin.text


def test_the_mover_does_not_hear_their_own_announcements(world, accounts, clock):
    a = _session(world, accounts, clock)
    FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".set enter_msg unfolds out of the air.")
    dispatch(a, ".set exit_msg melts into the hedge.")
    a.clear()
    dispatch(a, ".go oak")
    assert "melts into the hedge" not in a.text
    assert "unfolds out of the air" not in a.text


# ---- AFK -----------------------------------------------------------------

def test_bare_afk_uses_the_house_default_when_afk_msg_is_unset(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".afk")
    assert AFK_DEFAULT in s.text
    assert s.resident.afk_message == AFK_DEFAULT


def test_bare_afk_uses_your_afk_msg(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set afk_msg gone to see about a badger")
    s.clear()
    dispatch(s, ".afk")
    assert s.resident.afk_message == "gone to see about a badger"
    assert AFK_DEFAULT not in s.text


def test_afk_with_text_still_overrides_per_use(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set afk_msg gone to see about a badger")
    dispatch(s, ".afk just this once")
    assert s.resident.afk_message == "just this once"
    # ...and the stored default is untouched for next time.
    dispatch(s, ".afk")
    assert s.resident.afk_message == "gone to see about a badger"


# ---- cross-door: login / logout over a real socket ----------------------

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
        disconnect_grace_seconds=0.2,
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
    await _send(w, pw); await _collect(r)
    await _send(w, "y"); await _collect(r, timeout=1.5)
    return r, w


async def _login(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name); await _collect(r); await _send(w, pw)
    await _collect(r, timeout=1.0)
    return r, w


@pytest.mark.asyncio
async def test_defaults_over_the_wire_are_unchanged(server):
    srv, port = server
    rw, ww = await _new(port, "Wren")
    rc, wc = await _new(port, "Corvid", "corvidpw")
    assert "[Corvid enters the world]" in await _collect(rw, timeout=1.0)
    await _send(wc, ".quit")
    await _collect(rc); wc.close()
    assert "[Corvid leaves the world]" in await _collect(rw, timeout=1.0)
    ww.close()


# ---- the fold into .set --------------------------------------------------

def test_the_standalone_verbs_are_gone(world, accounts, clock):
    """They were five near-identical top-level verbs for one idea; .set owns it
    now, and the old spellings get the ordinary unknown-command reply."""
    s = _session(world, accounts, clock)
    for verb in VERBS + ["inmsg", "outmsg", "afkmsg"]:
        s.clear()
        dispatch(s, f".{verb} something")
        assert f"Unknown command: .{verb}" in s.text, verb
        assert verb not in (s.resident.messages or {})


def test_bare_set_shows_every_setting(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set")
    out = s.text
    assert "dim        off" in out and "time       off" in out
    assert "helpstyle  long" in out
    for verb in VERBS:                              # the three room/afk prefs
        assert verb in out, verb
    # All EIGHT topics are marked since v1.25.0: the three switches follow the
    # live house default now, exactly as the five message prefs always have, so
    # bare `.set` says so for all of them.
    assert out.count("(house default)") == 8


def test_bare_set_marks_only_the_unset_ones_as_default(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set enter_msg unfolds out of the air.")
    s.clear()
    dispatch(s, ".set")
    out = s.text
    assert "enter_msg  unfolds out of the air." in out
    assert out.count("(house default)") == 7        # the other seven of eight
    # ...and the one that IS set is not marked as a default.
    line = next(l for l in out.split("\n") if l.strip().startswith("enter_msg"))
    assert "(house default)" not in line


def test_bare_set_is_one_unit(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set")
    assert len(s.out) == 1


@pytest.mark.parametrize("verb", VERBS)
def test_set_usage_line_on_a_bad_toggle_value(world, accounts, clock, verb):
    """A message pref takes free text, so it can never be a 'bad value' — but a
    toggle can, and the usage line names the whole surface."""
    s = _session(world, accounts, clock)
    dispatch(s, ".set dim maybe")
    assert "Usage: .set <topic> <value>" in s.text
    assert "dim on|off" in s.text
    assert verb in s.text                           # the prefs are listed too


def test_unknown_set_key_gets_the_usage(world, accounts, clock):
    s = _session(world, accounts, clock)
    dispatch(s, ".set nonsense on")
    assert "Usage: .set <topic> <value>" in s.text
    assert "dim on|off" in s.text


# ---- the 1.16.0 rename ---------------------------------------------------

def test_the_old_storage_keys_migrate_and_still_take_effect(world, accounts, clock):
    """A resident who set their wording before 1.16.0 keeps it, under the new
    name, without doing anything."""
    # An account saved under the OLD names looked exactly like this.
    accounts.set_setting("Corvid", "inmsg", "unfolds out of the air.")
    accounts.set_setting("Corvid", "logoutmsg", "is swallowed by the dark.")
    acct = accounts.get("Corvid")
    acct.settings = migrate_settings(acct.settings)

    assert acct.settings["enter_msg"] == "unfolds out of the air."
    assert acct.settings["logout_msg"] == "is swallowed by the dark."
    assert "inmsg" not in acct.settings and "logoutmsg" not in acct.settings

    s = _session(world, accounts, clock)
    s.resident.messages = {                          # what _load_prefs does
        k: acct.settings[k] for k in MSG_COMMANDS
        if isinstance(acct.settings.get(k), str)
    }
    # It still drives the announcement...
    assert resident_message(s, s.resident, "enter_msg") == "unfolds out of the air."
    # ...and .set shows it as theirs, not as a default.
    dispatch(s, ".set enter_msg")
    assert "Corvid unfolds out of the air." in s.text
    assert "house default" not in s.text


def test_every_old_key_maps_and_nothing_is_dropped():
    """The rename is collision-free: five in, five out, values intact, and the
    keys that were never renamed pass through untouched."""
    old = {"inmsg": "a", "outmsg": "b", "afkmsg": "c", "loginmsg": "d",
           "logoutmsg": "e", "dim": True, "helpstyle": "short"}
    new = migrate_settings(old)
    assert new == {"enter_msg": "a", "exit_msg": "b", "afk_msg": "c",
                   "login_msg": "d", "logout_msg": "e",
                   "dim": True, "helpstyle": "short"}
    assert len(new) == len(old)                      # nothing merged, nothing lost
    assert migrate_settings(new) == new              # and it is idempotent


def test_migration_happens_on_load_and_is_written_back(tmp_path):
    """Migrate-in-place: the file itself is brought up to date, once."""
    import json
    path = tmp_path / "accounts.json"
    store = AccountStore(path)
    store.create("Corvid", "pw")
    store.set_setting("Corvid", "inmsg", "unfolds out of the air.")
    store.flush()
    # Rewrite the file the way a pre-1.16 server left it.
    data = json.loads(path.read_text())
    rec = data["accounts"]["corvid"]
    rec["settings"] = {"inmsg": "unfolds out of the air.",
                       "logoutmsg": "is swallowed by the dark."}
    path.write_text(json.dumps(data))

    reloaded = AccountStore(path)
    acct = reloaded.get("Corvid")
    assert acct.settings == {"enter_msg": "unfolds out of the air.",
                             "logout_msg": "is swallowed by the dark."}
    # ...and it was written back, so the next boot has nothing left to do.
    on_disk = json.loads(path.read_text())["accounts"]["corvid"]["settings"]
    assert on_disk == acct.settings
