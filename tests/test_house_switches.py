# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The display switches — dim / time / helpstyle — as `.ownerset` keys.

`defaults.txt` is gone (v1.17.0). These three were the last thing in it, and a
whole operator file, a loader, a bespoke error class and a config key for three
words was more machinery than the idea deserved. They are ordinary house settings
now, with one honest difference from the message keys: a session reads them ONCE,
at start, so a change reaches a resident at their NEXT LOGIN.

These tests pin both halves — that they work, and that the next-login limit is
what it is rather than an accident.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch, house_switch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import HOUSE_DEFAULTS, SWITCH_KEYS, StateStore

from conftest import FakeSession, seed_account

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"
STRONG = "brass-otter-hum-9"


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "state.json")


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw")
    return store


# ---- the built-ins are what defaults.txt used to say ----------------------

def test_the_builtins_are_the_old_shipped_values():
    """Behaviour-identical to the file that is gone: a house that sets nothing
    looks exactly as it did in 1.16."""
    assert HOUSE_DEFAULTS["dim"] == "off"
    assert HOUSE_DEFAULTS["time"] == "off"
    assert HOUSE_DEFAULTS["helpstyle"] == "long"


def test_house_switch_types_them_the_way_a_session_wants():
    assert house_switch(None, "dim") is False
    assert house_switch(None, "time") is False
    assert house_switch(None, "helpstyle") == "long"


def test_a_garbled_helpstyle_falls_back_rather_than_breaking_help(tmp_path):
    """Validation refuses one at the door, but a value hand-edited into
    state.json must not make .help unrenderable."""
    st = StateStore(tmp_path / "state.json")
    st.settings["helpstyle"] = "terse"
    assert house_switch(st, "helpstyle") == "long"


# ---- .ownerset sets them ---------------------------------------------

@pytest.mark.parametrize("key,value", [("dim", "on"), ("time", "on"),
                                       ("helpstyle", "short")])
def test_ownerset_sets_a_switch(world, clock, state, key, value):
    s = FakeSession(world, name="Boss", level="arch", clock=clock, state=state)
    dispatch(s, f".ownerset {key} {value}")
    assert state.house_setting(key) == value


@pytest.mark.parametrize("key,bad", [("dim", "maybe"), ("time", "sometimes"),
                                     ("helpstyle", "terse")])
def test_a_bad_switch_value_is_refused(world, clock, state, key, bad):
    s = FakeSession(world, name="Boss", level="arch", clock=clock, state=state)
    s.clear()
    dispatch(s, f".ownerset {key} {bad}")
    assert "must be" in s.text
    assert state.house_setting(key) == HOUSE_DEFAULTS[key]      # unchanged


def test_switch_values_are_case_forgiving(world, clock, state):
    s = FakeSession(world, name="Boss", level="arch", clock=clock, state=state)
    dispatch(s, ".ownerset dim ON")
    assert state.house_setting("dim") == "on"


def test_the_switches_are_listed_by_bare_caretakerset(world, clock, state):
    s = FakeSession(world, name="Boss", level="arch", clock=clock, state=state)
    dispatch(s, ".ownerset")
    for key in SWITCH_KEYS:
        assert key in s.text, key


def test_a_switch_resets_to_its_default(world, clock, state):
    s = FakeSession(world, name="Boss", level="arch", clock=clock, state=state)
    dispatch(s, ".ownerset dim on")
    dispatch(s, ".ownerset dim default")
    assert state.house_setting("dim") == HOUSE_DEFAULTS["dim"]


# ---- an unset resident follows the house; their own .set wins -------------

def test_an_unset_resident_gets_the_house_switch(world, clock, state):
    state.set_house_setting("dim", "on")
    state.set_house_setting("helpstyle", "short")
    s = FakeSession(world, name="Corvid", clock=clock, state=state)
    assert s.dim_system is True
    assert s.help_style == "short"


def test_a_residents_own_set_overrides_the_house(world, clock, state, accounts):
    state.set_house_setting("dim", "on")
    s = FakeSession(world, name="Corvid", clock=clock, state=state, accounts=accounts)
    assert s.dim_system is True                    # follows the house...
    dispatch(s, ".set dim off")
    assert s.dim_system is False                   # ...until they choose
    assert accounts.get("Corvid").settings["dim"] is False


def test_a_house_change_moves_a_live_session(world, clock, state):
    """INVERTED in v1.25.0, and the inversion IS the release.

    These three used to be read once at session start, so a owner's change
    reached a resident only at their NEXT LOGIN — an arch had to reboot or make
    everyone relog. They now resolve at use-time like every other house key, so
    a change reaches a connected session on its very next line.
    """
    s = FakeSession(world, name="Corvid", clock=clock, state=state)
    assert s.dim_system is False
    state.set_house_setting("dim", "on")
    assert s.dim_system is True                    # no relogin, no reboot
    state.set_house_setting("dim", "off")
    assert s.dim_system is False                   # and back again


def test_a_house_change_moves_time_and_helpstyle_too(world, clock, state):
    s = FakeSession(world, name="Corvid", clock=clock, state=state)
    assert s.show_time is False
    assert s.help_style == "long"
    state.set_house_setting("time", "on")
    state.set_house_setting("helpstyle", "short")
    assert s.show_time is True
    assert s.help_style == "short"


def test_an_override_holder_is_unmoved_by_a_house_change(world, clock, state,
                                                         accounts):
    """The precedence guard, from the other side: choosing your own value opts
    you OUT of following the house, until you say `default`."""
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set dim off")
    state.set_house_setting("dim", "on")
    assert s.dim_system is False                   # theirs, not the house's
    dispatch(s, ".set dim default")
    assert s.dim_system is True                    # ...until they hand it back


# ---- defaults.txt is gone ------------------------------------------------

def test_the_defaults_file_is_gone_from_the_repo():
    assert not (REPO / "tests" / "fixtures" / "house" / "defaults.txt").exists()


def test_the_loader_and_its_error_class_are_gone():
    import heresay.commands as c
    for name in ("load_defaults", "DefaultsError", "BUILTIN_DEFAULTS",
                 "DEFAULT_KEYS", "RETIRED_KEYS"):
        assert not hasattr(c, name), name


def _server_cfg(tmp_path, **over):
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
        house_dir=str(REPO / "tests" / "fixtures" / "house"),
        entry_room="atrium",
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


def test_a_server_starts_with_no_defaults_file_anywhere(tmp_path):
    """Nothing is read for defaults any more, so there is nothing to be missing."""
    srv = TalkerServer(_server_cfg(tmp_path))
    assert not hasattr(srv, "defaults")


def test_a_stray_defaults_file_key_is_ignored_not_fatal(tmp_path):
    """An operator's config still carrying `defaults_file` must load. The key is
    obsolete, and an obsolete key is ignored — never a refuse-to-start."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        f'[storage]\n'
        f'house_dir = "{REPO / "tests" / "fixtures" / "house"}"\n'
        f'defaults_file = "/nowhere/defaults.txt"\n'
        f'accounts_file = "{tmp_path / "accounts.json"}"\n'
        f'mail_file = "{tmp_path / "mailboxes.json"}"\n'
        f'mail_key_file = "{tmp_path / "mail.key"}"\n'
        f'revtell_file = "{tmp_path / "revtells.json"}"\n'
        f'bans_file = "{tmp_path / "bans.json"}"\n'
        f'state_file = "{tmp_path / "state.json"}"\n'
        f'[world]\nentry_room = "atrium"\n'
        f'[moderation]\nowner = "Generic"\n'
        f'[listeners]\nplain_port = 0\nws_port = 0\ntls_port = 0\n',
        encoding="utf-8")
    from heresay.config import load_config
    cfg = load_config(cfg_path)
    assert not hasattr(cfg, "defaults_file")
    srv = TalkerServer(cfg)                        # starts fine
    assert srv.world.start_room == "atrium"


# ---- through a real login ------------------------------------------------

@pytest.mark.asyncio
async def test_the_house_switch_reaches_a_real_login(tmp_path):
    from test_integration import collect, login_new
    cfg = _server_cfg(tmp_path)
    srv = TalkerServer(cfg)
    srv.state.set_house_setting("helpstyle", "short")
    await srv.start()
    port = srv._servers[0].sockets[0].getsockname()[1]
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await login_new(r, w, "Newbie")
        await collect(r)
        w.write(b".help\n")
        await w.drain()
        out = await collect(r)
        assert "talking" in out and "the world" in out      # the short index
        assert ".emote <text>" not in out                   # not the long grid
        w.write(b".quit\n")
        await w.drain()
        await collect(r)
    finally:
        await srv.aclose()
        w.close()


# ---- the uniform `default` surface (v1.25.0) -----------------------------
#
# `.set` and `.ignore` answer to one grammar now: <topic> <value>,
# <topic> default, and a bare default that resets the whole family. These pin
# the edges of that, and in particular the two topics deliberately OUTSIDE it.

def test_each_switch_can_be_handed_back_to_the_house(world, clock, state, accounts):
    for key, mine, house in (("dim", "off", "on"),
                             ("time", "off", "on"),
                             ("helpstyle", "short", "long")):
        s = FakeSession(world, name="Corvid", clock=clock, state=state,
                        accounts=accounts)
        dispatch(s, f".set {key} {mine}")
        assert not s.follows_house(key)
        dispatch(s, f".set {key} default")
        assert s.follows_house(key), key
        assert key not in accounts.get("Corvid").settings, key


def test_a_bare_switch_shows_its_value_and_the_house_marker(world, clock, state,
                                                            accounts):
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set dim")
    assert "(house default)" in s.text
    s.clear()
    dispatch(s, ".set dim on")
    s.clear()
    dispatch(s, ".set dim")
    assert "(house default)" not in s.text, "it is theirs now"


def test_set_default_resets_every_preference_topic(world, clock, state, accounts):
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set dim on")
    dispatch(s, ".set time on")
    dispatch(s, ".set helpstyle short")
    dispatch(s, ".set enter_msg wanders in")
    dispatch(s, ".set logout_msg slips away")
    dispatch(s, ".set default")
    assert s.resident.switches == {}
    assert s.resident.messages == {}
    for key in ("dim", "time", "helpstyle", "enter_msg", "logout_msg"):
        assert key not in accounts.get("Corvid").settings, key


def test_set_default_does_not_touch_remember(world, clock, state, accounts):
    """A bulk display-prefs reset must not sign your phone out."""
    accounts.set_setting("Corvid", "remember", True)
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set default")
    assert accounts.get("Corvid").settings.get("remember") is True


def test_set_default_does_not_touch_the_name(world, clock, state, accounts):
    """Identity, not a display preference — so a tidy-up cannot rename you."""
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set name CORVID")
    assert s.resident.name == "CORVID"
    dispatch(s, ".set default")
    assert s.resident.name == "CORVID"


def test_set_name_default_is_not_a_thing(world, clock, state, accounts):
    """`name` has no default to return to: .set name overwrites the account's
    display name in place, so the registered capitalization is not recorded
    anywhere. Refused rather than silently meaning something else."""
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set name default")
    assert s.resident.name == "Corvid"             # unchanged
    assert "only change the capitalization" in s.text


def test_a_house_change_reaches_a_real_session_on_its_next_line(world, state,
                                                                tmp_path):
    """Through a REAL Session and a real transport, not a fake: the value the
    wire actually carries changes with no relogin. This is the thing an arch
    could previously only get by rebooting."""
    from heresay.config import Config
    from heresay.session import Session
    from heresay.world import Resident
    from conftest import FakeClock, RecordingTransport

    tr = RecordingTransport(supports_ansi=True)
    s = Session(tr, world, None, Config(wrap_columns=78), clock=FakeClock(),
                wall_clock=lambda: 0.0, state=state)
    s.resident = Resident("Corvid", "", world.start_room)
    s.resident.session = s

    s.send("before")
    assert "\x1b[2m" not in tr.text, "dim starts off"

    state.set_house_setting("dim", "on")           # the owner, mid-session
    tr.frames.clear()
    s.send("after")
    assert "\x1b[2m" in tr.text, "the very next line is dimmed"


def test_a_switch_survives_a_grace_reattach(world, clock, state, accounts):
    """The overrides live on the RESIDENT, not the session — so a dropped
    connection that reattaches keeps them, exactly like the message prefs."""
    s = FakeSession(world, name="Corvid", clock=clock, state=state,
                    accounts=accounts)
    dispatch(s, ".set dim on")
    res = s.resident
    assert res.switches == {"dim": True}
    res.session = None                             # the connection drops
    assert res.switches == {"dim": True}           # ...and the choice is still his
