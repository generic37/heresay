# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.set` preferences: storage, persistence, and socket-only ANSI / time prefix."""

from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch, help_text
from heresay.config import Config
from heresay.session import ANSI_DIM, Session
from heresay.world import Resident, load_world

from conftest import FakeClock, FakeSession, RecordingTransport

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw")
    return store


def test_set_shows_defaults(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set")
    # The overview is a column now that .set covers eight settings, not two.
    assert "  dim        off" in s.text and "  time       off" in s.text
    assert "  helpstyle  long" in s.text          # the house default, shown as a value


def test_set_dim_persists_to_account(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set dim on")
    assert s.dim_system is True
    assert accounts.get("Corvid").settings.get("dim") is True   # persisted


def test_set_time_persists_and_relogin_loads_it(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set time on")
    dispatch(s, ".set dim on")
    # Simulate a relogin: a fresh session loads prefs the way the server does.
    acct = accounts.get("Corvid")
    fresh = FakeSession(world, name="Corvid2", clock=clock, accounts=accounts)
    fresh.dim_system = bool(acct.settings.get("dim", False))
    fresh.show_time = bool(acct.settings.get("time", False))
    assert fresh.dim_system and fresh.show_time


def test_set_bad_usage(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set dim maybe")
    assert "Usage:" in s.text


# ---- the color -> dim rename: no synonym, no migration -------------------

def test_set_color_is_no_longer_a_setting(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set color on")
    assert "Usage:" in s.text                       # the ordinary unknown-setting error
    assert s.dim_system is False
    assert "color" not in accounts.get("Corvid").settings


def test_usage_line_names_dim_not_color(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set nonsense on")
    assert "dim on|off" in s.text
    assert "color" not in s.text


def test_stored_color_key_loads_and_is_ignored(world, accounts, clock):
    """An account written before the rename still loads; the old key is simply
    never read, so the setting reverts to its default (the maintainer's call: no migration)."""
    accounts.set_setting("Corvid", "color", True)
    acct = accounts.get("Corvid")                    # loads without error
    assert acct.settings["color"] is True            # the key is untouched on disk
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    s.dim_system = bool(acct.settings.get("dim", False))   # what the server does
    assert s.dim_system is False
    dispatch(s, ".set")
    assert "  dim        off" in s.text


# ---- helpstyle -----------------------------------------------------------

def test_helpstyle_defaults_to_long(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    assert s.help_style == "long"
    dispatch(s, ".help")
    assert help_text(s.theme) == s.out[-1]
    assert "helpstyle" not in accounts.get("Corvid").settings   # unset stays unset


def test_helpstyle_short_switches_the_screen_and_persists(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set helpstyle short")
    assert s.help_style == "short"
    assert accounts.get("Corvid").settings.get("helpstyle") == "short"
    s.clear()
    dispatch(s, ".help")
    assert s.out[-1] == help_text(s.theme, "short")
    assert "administrative  .reboot" in s.text
    assert "say something to the room" not in s.text     # the long grid is gone


def test_helpstyle_switches_back(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set helpstyle short")
    dispatch(s, ".set helpstyle long")
    assert s.help_style == "long"
    assert accounts.get("Corvid").settings.get("helpstyle") == "long"
    s.clear()
    dispatch(s, ".help")
    assert s.out[-1] == help_text(s.theme)


def test_helpstyle_is_case_forgiving_and_rejects_anything_else(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set helpstyle SHORT")
    assert s.help_style == "short"
    s.clear()
    dispatch(s, ".set helpstyle terse")
    assert "Usage:" in s.text
    assert s.help_style == "short"                        # unchanged by a bad value


def test_helpstyle_survives_a_relogin(world, accounts, clock):
    """What the server does at login: an unknown or absent value is the house
    default, so nothing has to be migrated."""
    from heresay.commands import HELP_STYLES, SET_HELPSTYLE

    def loaded(value):
        if value is not None:
            accounts.set_setting("Corvid", SET_HELPSTYLE, value)
        style = accounts.get("Corvid").settings.get(SET_HELPSTYLE)
        return style if style in HELP_STYLES else "long"

    assert loaded(None) == "long"
    assert loaded("short") == "short"
    assert loaded("nonsense") == "long"


def test_the_usage_line_names_helpstyle(world, accounts, clock):
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    dispatch(s, ".set nonsense on")
    assert "helpstyle long|short" in s.text


# ---- rendering (real Session over a recording transport) -----------------

def _real_session(supports_ansi=True):
    world = load_world(WORLD, "atrium", "holding_cell")
    cfg = Config(wrap_columns=1000)
    tr = RecordingTransport(supports_ansi=supports_ansi)
    sess = Session(tr, world, None, cfg, clock=FakeClock(), wall_clock=lambda: 0.0)
    sess.resident = Resident("Corvid", "", world.start_room)
    return sess, tr


def test_dim_fades_system_lines_on_socket():
    sess, tr = _real_session(supports_ansi=True)
    sess.dim_system = True
    sess.send("A wizard arrives.", dim=True)     # system line
    sess.send("Corvid says: hi", dim=False)       # speech line
    assert ANSI_DIM in tr.frames[0]               # system dimmed
    assert ANSI_DIM not in tr.frames[1]           # speech untouched


def test_dim_ignored_on_web():
    sess, tr = _real_session(supports_ansi=False)   # the browser door
    sess.dim_system = True
    sess.send("A wizard arrives.", dim=True)
    assert "\x1b[" not in tr.text                  # never send ANSI to the web


def test_time_prefix_applied():
    sess, tr = _real_session(supports_ansi=True)
    sess.show_time = True
    sess.send("hello")
    # 00:00 local for a wall clock fixed at epoch 0 (UTC) — accept any HH:MM.
    import re
    assert re.match(r"\d\d:\d\d hello", tr.frames[0].strip())
