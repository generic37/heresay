# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The 1.16.0 house-default model: `.ownerset` is the house, `.set` is you.

The room and afk wording used to live in defaults.txt and needed a restart. It
now lives beside login/logout in `.ownerset` — one scheme, one home, live —
and these tests prove the two halves that matter: a owner's phrase moves
every resident who has not chosen their own, and a resident's own still wins.

Also here: the `release_msg` mirror of `jail_msg`, the house desc/profile
defaults, and the state.json key migration.
"""

import json

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.state import (HOUSE_DEFAULTS, SETTINGS_VERSION, StateStore,
                           jail_presence_line, migrate_house_settings,
                           presence_line)
from heresay.world import load_world

from conftest import FakeSession, WORLD


@pytest.fixture
def state(tmp_path):
    return StateStore(tmp_path / "state.json")


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw")
    return store


def _sess(world, clock, state, name="Corvid", **kw):
    return FakeSession(world, name=name, clock=clock, state=state, **kw)


# ---- the room wording is a house setting now -----------------------------

def test_ownerset_enter_msg_changes_an_unset_residents_arrival(
        world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset enter_msg materialises.")
    mover = _sess(world, clock, state, name="Mover", accounts=accounts)
    watcher = _sess(world, clock, state, name="Wren", room="oak", accounts=accounts)
    watcher.clear()
    dispatch(mover, ".go oak")
    assert "Mover materialises." in watcher.text
    assert "Mover arrives." not in watcher.text


def test_ownerset_exit_msg_keeps_the_room_substitution(
        world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset exit_msg drifts off toward the <room>.")
    mover = _sess(world, clock, state, name="Mover", accounts=accounts)
    origin = _sess(world, clock, state, name="Wren", accounts=accounts)
    origin.clear()
    dispatch(mover, ".go oak")
    assert "Mover drifts off toward the oak." in origin.text


def test_ownerset_afk_msg_changes_a_bare_afk(world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset afk_msg away with the fairies")
    s = _sess(world, clock, state, name="Corvid", accounts=accounts)
    dispatch(s, ".afk")
    assert s.resident.afk_message == "away with the fairies"


def test_a_personal_set_still_overrides_the_house(world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset enter_msg materialises.")
    mover = _sess(world, clock, state, name="Mover", accounts=accounts)
    dispatch(mover, ".set enter_msg strides in.")
    watcher = _sess(world, clock, state, name="Wren", room="oak", accounts=accounts)
    watcher.clear()
    dispatch(mover, ".go oak")
    assert "Mover strides in." in watcher.text
    assert "materialises" not in watcher.text


def test_the_house_moves_live_without_a_relogin(world, clock, state, accounts):
    """The whole reason it moved out of defaults.txt: no restart, no re-login."""
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    s = _sess(world, clock, state, name="Corvid", accounts=accounts)
    dispatch(s, ".set enter_msg")
    assert HOUSE_DEFAULTS["enter_msg"] in s.text
    dispatch(boss, ".ownerset enter_msg materialises.")
    s.clear()
    dispatch(s, ".set enter_msg")                # same session, no relog
    assert "materialises." in s.text
    assert "house default" in s.text


def test_set_default_returns_to_the_house_value_not_the_builtin(
        world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset enter_msg materialises.")
    s = _sess(world, clock, state, name="Corvid", accounts=accounts)
    dispatch(s, ".set enter_msg strides in.")
    s.clear()
    dispatch(s, ".set enter_msg default")
    assert "materialises." in s.text                       # the house's value
    assert "arrives." not in s.text                        # not the built-in


def test_bare_set_shows_the_house_values_marked_as_such(
        world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset afk_msg away with the fairies")
    s = _sess(world, clock, state, name="Corvid", accounts=accounts)
    dispatch(s, ".set")
    assert "away with the fairies   (house default)" in s.text


# ---- the ownerset listing --------------------------------------------

def test_the_listing_names_every_key(world, clock, state):
    s = _sess(world, clock, state, name="Boss", level="arch")
    dispatch(s, ".ownerset")
    for k in HOUSE_DEFAULTS:
        assert k in s.text, k


def test_the_render_shapes_are_the_engines(world, clock, state):
    """A owner sets WORDS; the brackets and the stars are not theirs to
    remove, which is what keeps a coming or going recognisable as one."""
    assert presence_line("Corvid", "enters the world") == "[Corvid enters the world]"
    assert jail_presence_line("Corvid", "arrives (jail)") == \
        "[*** Corvid arrives (jail) ***]"


# ---- release_msg ----------------------------------------------------------

def _jail_setup(tmp_path, clock):
    w = load_world(WORLD, "atrium", "holding_cell")
    st = StateStore(tmp_path / "state.json")
    staff = FakeSession(w, name="Wizzy", level="wizard", clock=clock, state=st)
    culprit = FakeSession(w, name="Shady", clock=clock, state=st)
    return w, st, staff, culprit


def test_freeing_shows_the_release_line(tmp_path, clock):
    _w, _st, staff, culprit = _jail_setup(tmp_path, clock)
    dispatch(staff, ".jail Shady")
    culprit.clear()
    dispatch(staff, ".free Shady")
    assert HOUSE_DEFAULTS["release_msg"] in culprit.text
    assert "You have been released." in culprit.text


def test_release_msg_is_ownerset_able(tmp_path, clock):
    _w, st, staff, culprit = _jail_setup(tmp_path, clock)
    st.set_house_setting("release_msg", "The well lets you go.")
    dispatch(staff, ".jail Shady")
    culprit.clear()
    dispatch(staff, ".free Shady")
    assert "The well lets you go." in culprit.text
    assert "You have been released." not in culprit.text


def test_jail_msg_and_release_msg_are_a_pair(tmp_path, clock):
    _w, _st, staff, culprit = _jail_setup(tmp_path, clock)
    culprit.clear()
    dispatch(staff, ".jail Shady")
    assert HOUSE_DEFAULTS["jail_msg"] in culprit.text
    culprit.clear()
    dispatch(staff, ".free Shady")
    assert HOUSE_DEFAULTS["release_msg"] in culprit.text


# ---- the house desc / profile defaults -----------------------------------

def test_desc_ships_empty_so_nothing_changes_out_of_the_box(
        world, clock, state, accounts):
    s = _sess(world, clock, state, name="Corvid", accounts=accounts)
    FakeSession(world, name="Wren", clock=clock, state=state)
    s.clear()
    dispatch(s, ".who")
    assert "has no description" in s.text


def test_a_house_desc_shows_for_an_unset_resident(world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset desc a curious newcomer")
    watcher = _sess(world, clock, state, name="Corvid", accounts=accounts)
    FakeSession(world, name="Wren", clock=clock, state=state)
    watcher.clear()
    dispatch(watcher, ".look")
    assert "   Wren a curious newcomer" in watcher.text
    watcher.clear()
    dispatch(watcher, ".who")
    assert "a curious newcomer" in watcher.text
    assert "has no description" not in watcher.text


def test_a_residents_own_desc_overrides_the_house(world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset desc a curious newcomer")
    them = _sess(world, clock, state, name="Wren", accounts=accounts)
    dispatch(them, ".desc all in a golden afternoon")
    watcher = _sess(world, clock, state, name="Corvid", accounts=accounts)
    watcher.clear()
    dispatch(watcher, ".look")
    assert "   Wren all in a golden afternoon" in watcher.text
    wren_line = next(l for l in watcher.text.split("\n") if "Wren" in l)
    assert "curious newcomer" not in wren_line
    # ...while Boss, who has written none, still shows the house's words.
    assert "   Boss a curious newcomer" in watcher.text


def test_the_house_profile_is_written_through_the_editor(world, clock, state, accounts):
    """Multi-line, so it uses the SAME editor `.profile` itself uses rather than
    a command argument — prose does not fit on a command line."""
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset profile edit")
    assert boss.in_editor()
    boss.feed_editor("Nobody here has written a profile yet.")
    boss.feed_editor("Ask them about it.")
    boss.feed_editor(".end")
    assert not boss.in_editor()
    assert state.house_setting("profile") == (
        "Nobody here has written a profile yet.\nAsk them about it.")


def test_the_listing_summarises_a_multi_line_profile(world, clock, state, accounts):
    """One line per key: a pasted profile would wreck the screen, so the listing
    says how big it is instead of what it says."""
    state.set_house_setting("profile", "one\ntwo\nthree")
    s = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(s, ".ownerset")
    assert "profile: <3 lines>" in s.text
    assert "two" not in s.text


def test_the_house_profile_shows_in_examine_for_an_unset_resident(
        world, clock, state, tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Wren", "pw")
    state.set_house_setting("profile", "Not yet written.")
    s = _sess(world, clock, state, name="Corvid", accounts=store)
    dispatch(s, ".examine Wren")
    assert "Not yet written." in s.text
    assert "They keep their own counsel." not in s.text


def test_a_residents_own_profile_overrides_the_house(world, clock, state, tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Wren", "pw")
    store.set_profile("Wren", "I am a real person with real words.")
    state.set_house_setting("profile", "Not yet written.")
    s = _sess(world, clock, state, name="Corvid", accounts=store)
    dispatch(s, ".examine Wren")
    assert "I am a real person with real words." in s.text
    assert "Not yet written." not in s.text


def test_profile_default_clears_the_house_profile(world, clock, state, accounts):
    state.set_house_setting("profile", "one\ntwo")
    s = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(s, ".ownerset profile default")
    assert state.house_setting("profile") == ""


def test_profile_with_a_stray_argument_is_refused(world, clock, state, accounts):
    s = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(s, ".ownerset profile some words typed inline")
    assert "multi-line" in s.text
    assert state.house_setting("profile") == ""


# ---- the state.json migration --------------------------------------------

def test_the_remap_is_one_pass_and_enter_msg_lands_on_login_msg():
    """The one place this migration could mean the wrong thing: `enter_msg` is
    on BOTH sides of the rename with different meanings. The stored old value is
    a world-login phrase and must land on login_msg — leaving the NEW enter_msg
    (room arrival) unset, at its own default."""
    old = {"enter_msg": "tumbles down the hole",
           "leave_msg": "wanders off",
           "enter_msg_jailed": "is dragged in",
           "jail_reply_notice": "no replies",
           "jail_msg": "You've been jailed.",
           "broadcast_scope": "entry"}
    new = migrate_house_settings(old)
    assert new["login_msg"] == "tumbles down the hole"
    assert "enter_msg" not in new              # the NEW room key starts unset
    assert new["logout_msg"] == "wanders off"
    assert new["jail_logon_msg"] == "is dragged in"
    assert new["jail_reply_msg"] == "no replies"
    assert new["jail_msg"] == "You've been jailed."      # untouched
    assert new["broadcast_scope"] == "entry"             # untouched
    assert len(new) == len(old)                          # nothing merged or lost


def test_a_pre_116_state_file_migrates_on_load_and_is_written_back(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "allow_new_accounts": False,
        "allow_logins": True,
        "settings": {"enter_msg": "tumbles down the hole",
                     "leave_msg": "wanders off",
                     "enter_msg_jailed": "is dragged in",
                     "jail_reply_notice": "no replies"},
        "jailed": ["shady"],
    }))
    st = StateStore(path)
    assert st.house_setting("login_msg") == "tumbles down the hole"
    assert st.house_setting("logout_msg") == "wanders off"
    assert st.house_setting("jail_logon_msg") == "is dragged in"
    assert st.house_setting("jail_reply_msg") == "no replies"
    # The NEW room key was never stored, so it is at its own default.
    assert st.house_setting("enter_msg") == HOUSE_DEFAULTS["enter_msg"]
    # Everything else in the file is intact.
    assert st.allow_new_accounts is False
    assert st.jailed == {"shady"}
    # ...and it was written back, stamped, so it cannot run a second time.
    on_disk = json.loads(path.read_text())
    assert on_disk["settings_version"] == SETTINGS_VERSION
    assert on_disk["settings"]["login_msg"] == "tumbles down the hole"
    assert "enter_msg" not in on_disk["settings"]


def test_a_migrated_file_is_not_migrated_again(tmp_path):
    """Idempotence, and the reason for the version stamp: if the remap ran twice
    it would move the (new) room enter_msg onto login_msg and lose it."""
    path = tmp_path / "state.json"
    st = StateStore(path)
    st.set_house_setting("enter_msg", "materialises.")     # the NEW room key
    st.set_house_setting("login_msg", "tumbles in")
    reloaded = StateStore(path)
    assert reloaded.house_setting("enter_msg") == "materialises."
    assert reloaded.house_setting("login_msg") == "tumbles in"


def test_a_legacy_bracketed_value_still_self_heals(tmp_path):
    """The pre-1.14 template guard, carried through the rename: a stored
    `[{name} …]` under a BRACKETED key falls back to the default rather than
    double-bracketing."""
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "version": 1,
        "settings": {"enter_msg": "[{name} enters the world]"},
    }))
    st = StateStore(path)
    assert "login_msg" not in st.settings
    assert st.house_setting("login_msg") == HOUSE_DEFAULTS["login_msg"]


# ---------------------------------------------------------------------------
# ⛔ The HOUSE phrase is bounded exactly as the personal one is (v1.52.4, W4)
#
# `.set login_msg` runs its text through `clean_message` — one line, printable,
# whitespace collapsed, capped. `.ownerset login_msg` stored `parts[1].strip()`
# raw, so the phrase broadcast to EVERYONE on every login was the unbounded one
# while the phrase only its owner's arrivals carry was capped. The house line is
# the one seen more, not less.
# ---------------------------------------------------------------------------

from heresay.commands._shared import DESC_MAX, MSG_MAX      # noqa: E402


def _house(state, key):
    return state.house_setting(key)


@pytest.mark.parametrize("key", ["login_msg", "logout_msg", "jail_logon_msg",
                                 "enter_msg", "exit_msg", "afk_msg",
                                 "jail_msg", "release_msg", "jail_reply_msg"])
def test_a_house_phrase_is_capped_like_the_personal_one(world, clock, state,
                                                        accounts, key):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, f".ownerset {key} " + "x" * 500)
    assert len(_house(state, key)) == MSG_MAX


@pytest.mark.parametrize("key", ["login_msg", "enter_msg", "jail_msg"])
def test_a_house_phrase_collapses_ragged_whitespace(world, clock, state,
                                                    accounts, key):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, f".ownerset {key}   enters    the \t world  ")
    assert _house(state, key) == "enters the world"


def test_the_house_cap_is_the_SAME_CALL_the_personal_one_makes(world, clock,
                                                               state, accounts):
    """⚠ Identical, not merely similar: the same helper, so the two cannot drift
    the next time either is tuned."""
    from heresay.commands._shared import clean_message
    raw = "  a  phrase\tof   " + "y" * 200
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, f".ownerset login_msg {raw}")

    res = _sess(world, clock, state, name="Corvid", accounts=accounts)
    dispatch(res, f".set login_msg {raw}")
    assert _house(state, "login_msg") == res.resident.messages["login_msg"]
    assert _house(state, "login_msg") == clean_message(raw)


def test_the_house_desc_takes_DESC_MAX_not_MSG_MAX(world, clock, state, accounts):
    """⚠ Per-key, matching the personal command each one shadows: `desc` is the
    default for a resident's own `.desc`, which allows 120 — capping the house's
    at 60 would make it the one description nobody could write."""
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset desc " + "z" * 500)
    assert len(_house(state, "desc")) == DESC_MAX

    res = _sess(world, clock, state, name="Corvid", accounts=accounts)
    dispatch(res, ".desc " + "z" * 500)
    assert len(res.resident.description) == DESC_MAX


def test_a_short_phrase_is_untouched(world, clock, state, accounts):
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset exit_msg slips out toward the <room>.")
    assert _house(state, "exit_msg") == "slips out toward the <room>."


def test_the_enum_int_and_multiline_keys_are_untouched(world, clock, state,
                                                       accounts):
    """The cap applies to PHRASES only — the typed keys keep their own
    validation, and `profile` is prose written through the editor, where a
    one-line 60-character cap would be nonsense."""
    boss = _sess(world, clock, state, name="Boss", level="arch", accounts=accounts)
    dispatch(boss, ".ownerset broadcast_scope entry")
    assert _house(state, "broadcast_scope") == "entry"
    dispatch(boss, ".ownerset raise_prompt_max 9")
    assert _house(state, "raise_prompt_max") == "9"
    boss.clear()
    dispatch(boss, ".ownerset dim on")
    assert _house(state, "dim") == "on"

    long_profile = "\n".join("line %d of a house profile" % n for n in range(10))
    state.set_house_setting("profile", long_profile)
    assert _house(state, "profile") == long_profile      # multi-line, uncapped
