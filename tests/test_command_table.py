# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Data-driven permissions: the command→rank table (v1.28.0).

The two hardcoded gate sets became a persisted table in `state.json`, seeded from
a single code registry. What these tests protect:

  * that the seed is EXACTLY the pre-1.28 permissions, so an upgrading house
    behaves identically — asserted verb by verb, rank by rank;
  * that the gate keys on the CANONICAL command, so an alias can never carry a
    different rank from the command it is an alias of (`>` cannot outrank
    `.tell`) — the alias hole, closed by construction;
  * that both fail-safe directions point at LESS access: an unknown *rank* reads
    as the floor, an unknown *requirement* reads as the arch;
  * that a structurally corrupt table refuses the boot while a merely STALE one
    degrades and warns — closing the house is not a safe reading, locking one
    command down is;
  * and that anchors mean ROLES, so renaming a rank's word moves the staff
    commands with it.
"""

import json
import logging
from pathlib import Path

import pytest

import heresay.commands as C
from heresay.accounts import AccountStore
from heresay.commands import dispatch
from heresay.commands.registry import (ANCHORS, OWNER, BY_NAME, CANONICAL,
                                       CATEGORIES, COMMANDS, RESIDENT, STAFF,
                                       command_seed, privileged_commands)
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import CommandTableError, StateStore
from heresay.theme import DEFAULT_THEME, Theme, load_theme
from heresay.world import load_world

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"

# The pre-1.28 gate sets, frozen here as the behaviour-identical baseline.
# v1.29.0 removed `.keeperhelp` / `.caretakerhelp` with the screens they showed.
V127_STAFF = frozenset({"gag", "ungag", "jail", "free", "boot", "ban", "unban",
                        "staffmail", "dstaffmail"})
# `.ownerset` is `.ownerset` since v1.54.0 (with `.ownerset` kept as a
# dispatch alias); the CANONICAL name is what the seed/registry key on.
V127_ARCH = frozenset({"accounts", "promote", "demote", "mailpurge", "newusers",
                       "logins", "logoutall", "ownerset",
                       "deleteuser", "resetpassword"})

# Commands that DID NOT EXIST at v1.27, mapped to the anchor they were born at.
#
# The three tests below are historical pins: "the seed still reproduces the
# permissions v1.27 had". A command added later cannot be in that history, so it
# is named here rather than folded into V127_ARCH — which would quietly rewrite
# what the pin claims to guarantee. Adding a gated command is a deliberate act
# and this list is where it is admitted to.
SINCE_V127 = {
    "staff_actions": OWNER,      # v1.30.0 — the event log's first view
    # v1.32.0 split `.ban`; v1.33.0 brought the address half DOWN to staff, once
    # nothing could print an address any more, and added its two companions.
    "ban_ip": STAFF,
    "unban_ip": STAFF,
    "bans": STAFF,
    # v1.35.0 — the two counting views over the same event log. `.staff_calls`
    # is staff-ops (it names the people calling them); `.security` is the
    # owner's glance.
    "staff_calls": STAFF,
    "security": OWNER,
    # v1.36.0 — the builder's view of what the last load made of the house.
    "load_report": STAFF,
    # v1.50.0 — the report box's reading half. OWNER by seed so that a report
    # ABOUT a staff does not land in front of staff; the filing half
    # (`.report`) is RESIDENT and so needs no entry here. ⚠ A SEED, not a lock:
    # the operator moves either in permissions.toml like any other command.
    "read_report": OWNER,
}

WARDENS = Theme(order=("citizen", "warden", "Steward"), staff="warden",
                level_display={"staff": ("", "wardens")})


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


def _cfg(tmp_path, **over):
    base = dict(
        owner_name="Generic",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000,
    )
    base.update(over)
    return Config(**base)


def _store(tmp_path, ladder=DEFAULT_THEME, **kw):
    return StateStore(tmp_path / "state.json", command_seed=command_seed(),
                      ladder=ladder, **kw)


# ---------------------------------------------------------------------------
# 1. The registry
# ---------------------------------------------------------------------------

def test_the_registry_declares_every_command_once():
    names = [c.name for c in COMMANDS]
    assert len(names) == len(set(names)) == 69        # 66 gated + say + 2 client


def test_every_registry_entry_binds_to_a_real_handler():
    """`__init__` binds `cmd_<name>` from its own namespace — so a typo in the
    registry must not be discoverable only at dispatch time."""
    for c in COMMANDS:
        if not c.dispatchable:                        # say / the browser verbs
            continue
        assert c.name in CANONICAL
        assert callable(C._COMMANDS[c.name]), c.name


def test_the_dispatch_table_is_derived_not_hand_written():
    """Every verb in the table comes from the registry, and vice versa."""
    assert set(C._COMMANDS) == set(CANONICAL)


def test_every_command_has_a_category_from_the_frozen_set():
    for c in COMMANDS:
        assert c.category in CATEGORIES, c.name


def test_every_category_is_used():
    used = {c.category for c in COMMANDS}
    assert used == set(CATEGORIES), set(CATEGORIES) ^ used


def test_every_anchor_is_a_known_anchor():
    for c in COMMANDS:
        assert c.anchor in ANCHORS, c.name


def test_the_old_hardcoded_sets_are_gone():
    """They were the previous source of truth; they must not linger as a second
    one. (The covenant tests read `privileged_commands()` now.)"""
    assert not hasattr(C, "_WIZARD_VERBS")
    assert not hasattr(C, "_ARCH_VERBS")


# ---------------------------------------------------------------------------
# 2. The seed IS the pre-1.28 behaviour
# ---------------------------------------------------------------------------

def test_the_seed_reproduces_the_v127_permissions_exactly():
    seed = command_seed()
    for name in V127_STAFF:
        assert seed[name] == STAFF, name
    for name in V127_ARCH:
        assert seed[name] == OWNER, name
    for name, anchor in SINCE_V127.items():
        assert seed[name] == anchor, name
    for name, anchor in seed.items():
        if (name not in V127_STAFF and name not in V127_ARCH
                and name not in SINCE_V127):
            assert anchor == RESIDENT, name
    # Only GATED entries are in the table: bare speech and the browser verbs are
    # listed in help but have no rank anyone could usefully assign.
    assert set(seed) == {c.name for c in COMMANDS if c.gated}
    assert "say" not in seed and "theme" not in seed


def test_privileged_commands_is_the_old_two_sets():
    assert privileged_commands() == V127_STAFF | V127_ARCH | set(SINCE_V127)


@pytest.mark.parametrize("verb", sorted(CANONICAL))
def test_every_verb_gates_exactly_as_v127_did(verb):
    """The behaviour-identical guarantee, verb by verb and rank by rank."""
    t = DEFAULT_THEME
    canon = CANONICAL[verb]
    for rank in t.order:
        here = t.index_or_floor(rank)
        if canon in V127_ARCH or SINCE_V127.get(canon) == OWNER:
            expected = here >= t.rank_index(t.owner_rank)
        elif canon in V127_STAFF or SINCE_V127.get(canon) == STAFF:
            expected = here >= t.staff_index
        else:
            expected = True
        assert C._visible(verb, rank, t) is expected, (verb, rank)


# ---------------------------------------------------------------------------
# 3. Anchors are ROLES
# ---------------------------------------------------------------------------

def test_anchors_resolve_against_the_live_ladder():
    t = DEFAULT_THEME                            # the GENERIC ladder (v1.54.0)
    assert (t.resolve_anchor("resident"), t.resolve_anchor("staff"),
            t.resolve_anchor("owner")) == ("resident", "staff", "owner")


def test_the_legacy_anchor_words_resolve_to_NOTHING(tmp_path):
    """⛔ v1.60.0 deleted them. `floor`/`arch`/`wizard` were the pre-v1.54 role
    words, accepted for one release so an unconverted `permissions.toml` kept
    working. A value that resolves to nothing is an ERROR the caller refuses —
    which is the right answer for a word that no longer means anything."""
    t = DEFAULT_THEME
    for legacy in ("floor", "arch", "wizard"):
        assert t.resolve_anchor(legacy) is None, legacy


def test_a_staff_command_follows_a_RENAMED_staff_rank(tmp_path):
    """The whole reason the table stores anchors: rename the word, keep the rule."""
    st = _store(tmp_path, ladder=WARDENS)
    assert st.command_rank("gag", WARDENS) == "warden"
    assert st.command_rank("promote", WARDENS) == "Steward"
    assert st.command_rank("look", WARDENS) == "citizen"
    # ...and the gate agrees.
    assert C._visible("gag", "warden", WARDENS, st)
    assert not C._visible("gag", "citizen", WARDENS, st)


def test_a_rank_may_not_be_named_after_an_anchor(tmp_path, caplog):
    """A reserved role-anchor word as a RUNG would make a table value ambiguous
    with the ROLE. ⚠ The reserved-from-order set is the pure-role words
    `newb`/`floor` (`theme.ANCHOR_NAMES`); the level ids
    `resident`/`staff`/`owner` are NOT reserved, since a house legitimately
    names its anchor rank after its level. So the collision word here is a
    pure-role anchor (`newb`), not `staff`. ⚠ v1.60.0 left `newb` the ONLY
    reserved word: `floor`/`arch`/`wizard` went with the aliases that made them
    ambiguous, so a house may name a rung `floor` now and nothing reads it as a
    role."""
    p = tmp_path / "theme.toml"
    p.write_text('[ranks]\norder=["resident","newb","staff","owner"]\n',
                 encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        t = load_theme(p)
    assert "newb" not in t.order
    assert t.order == ("resident", "staff", "owner")
    assert "reserved role anchor" in caplog.text


def test_the_anchor_check_is_case_insensitive(tmp_path, caplog):
    p = tmp_path / "theme.toml"
    p.write_text('[ranks]\norder=["resident","Newb","owner"]\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        t = load_theme(p)
    assert "Newb" not in t.order


# ---------------------------------------------------------------------------
# 4. The gate is keyed on the canonical command
# ---------------------------------------------------------------------------

ALIAS_SETS = [(c.name, a) for c in COMMANDS for a in c.aliases]


@pytest.mark.parametrize("canon,alias", ALIAS_SETS)
def test_an_alias_inherits_its_commands_rank(canon, alias, tmp_path):
    st = _store(tmp_path)
    st.commands[canon] = OWNER          # raise the command...
    for rank in DEFAULT_THEME.order:
        assert C._visible(alias, rank, DEFAULT_THEME, st) == \
            C._visible(canon, rank, DEFAULT_THEME, st), (alias, rank)


def test_raising_tell_gates_the_bare_prefix_too(world, tmp_path):
    """F1: `>` used to be handled BEFORE the gate. It is an alias now."""
    st = _store(tmp_path)
    res = FakeSession(world, name="Res", state=st)
    FakeSession(world, name="Bob", state=st)
    res.clear()
    dispatch(res, "> Bob hello")
    assert "Unknown command" not in res.text     # floor: works

    st.commands["tell"] = STAFF                  # raise it
    res.clear()
    dispatch(res, "> Bob hello")
    assert "Unknown command: .>" in res.text     # the prefix is gated too
    res.clear()
    dispatch(res, ".tell Bob hello")
    assert "Unknown command: .tell" in res.text


def test_the_emote_prefix_is_gated_with_emote(world, tmp_path):
    st = _store(tmp_path)
    st.commands["emote"] = OWNER
    res = FakeSession(world, name="Res", state=st)
    res.clear()
    dispatch(res, ";waves")
    assert "Unknown command" in res.text


def test_bare_speech_is_never_gated(world, tmp_path):
    """`.say` has no dotted form; gating it would not be a permission, it would
    be a mute."""
    st = _store(tmp_path)
    st.commands["emote"] = OWNER
    res = FakeSession(world, name="Res", state=st)
    res.clear()
    dispatch(res, "hello everyone")
    assert "Unknown command" not in res.text


# ---------------------------------------------------------------------------
# 5. Fail-safe: unlisted -> arch
# ---------------------------------------------------------------------------

def test_an_unlisted_command_is_arch_only(tmp_path):
    st = _store(tmp_path)
    assert st.command_rank("brand_new_verb", DEFAULT_THEME) == "owner"


def test_removing_a_command_from_the_table_locks_it_down(tmp_path, world):
    st = _store(tmp_path)
    del st.commands["look"]
    assert not C._visible("look", "resident", DEFAULT_THEME, st)
    assert C._visible("look", "owner", DEFAULT_THEME, st)


def test_a_stale_value_degrades_to_arch_and_does_not_fail(tmp_path, caplog):
    """A rename orphan or a typo. Locked down + counted — the house still opens."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": {
        "gag": "staff", "look": "resident", "boot": "warden"}}), encoding="utf-8")
    st = StateStore(p, command_seed=command_seed(), ladder=DEFAULT_THEME)
    assert st.unresolvable_commands == 1
    assert st.command_rank("boot", DEFAULT_THEME) == "owner"
    assert st.command_rank("gag", DEFAULT_THEME) == "staff"


def test_the_server_warns_about_stale_values(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": {"boot": "warden"}}),
                 encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        TalkerServer(_cfg(tmp_path))
    assert "locked to" in caplog.text


def test_a_DISABLED_value_is_not_counted_as_a_stale_rank(tmp_path, caplog):
    """⛔ `disabled` is the one legal value that is not a rank, so the
    unresolvable count has to exclude it — and did not until v1.60.0.

    The warning it produced was false twice over: the operator had written a
    value the engine documents, and the command is ABSENT rather than locked to
    the owner. ⚠ Found by the fresh-install rehearsal, because the shipped
    skeleton disables `.reboot` — so every new house met a warning about its own
    untouched `permissions.toml`.
    """
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": {"reboot": "disabled"}}),
                 encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        srv = TalkerServer(_cfg(tmp_path))
    assert srv.state.unresolvable_commands == 0
    assert "locked to" not in caplog.text
    # ...and it is still genuinely off, which is the behaviour that was right
    # all along — only the count was wrong.
    assert srv.state.command_disabled("reboot")


# ---------------------------------------------------------------------------
# 6. Fail-fast: structural corruption
# ---------------------------------------------------------------------------

def test_a_non_object_table_refuses_to_boot(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": ["gag", "boot"]}),
                 encoding="utf-8")
    with pytest.raises(CommandTableError) as e:
        StateStore(p, command_seed=command_seed(), ladder=DEFAULT_THEME)
    msg = str(e.value)
    assert "Cannot start" in msg and "must be an object" in msg
    assert "re-seed" in msg


def test_a_non_string_value_refuses_to_boot(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": {"gag": 3}}),
                 encoding="utf-8")
    with pytest.raises(CommandTableError) as e:
        StateStore(p, command_seed=command_seed(), ladder=DEFAULT_THEME)
    msg = str(e.value)
    assert "'gag'" in msg and "not a rank name" in msg


def test_corruption_stops_the_server_too(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": "nope"}), encoding="utf-8")
    with pytest.raises(CommandTableError):
        TalkerServer(_cfg(tmp_path))


# ---------------------------------------------------------------------------
# 7. Seed / migration -> tests/test_permissions.py
#
# ⚠ These moved with the table itself (v1.37.0). Seeding, migration, the
# self-completion and "an edit survives" are questions about `permissions.toml`
# now — state.json holds no command table to seed. What is left in this file is
# what it was always really about: the registry, the gate, and the anchors.
# ---------------------------------------------------------------------------








def test_the_table_is_live_in_memory(world, tmp_path):
    """A programmatic change takes effect on the very next command — which is
    what Phase E's editor will rely on."""
    st = _store(tmp_path)
    res = FakeSession(world, name="Res", state=st)
    res.clear()
    dispatch(res, ".gag Bob")
    assert "Unknown command: .gag" in res.text
    st.commands["gag"] = RESIDENT
    res.clear()
    dispatch(res, ".gag Bob")
    assert "Unknown command" not in res.text



# ---------------------------------------------------------------------------
# 8. F3 / F4
# ---------------------------------------------------------------------------

def test_client_verbs_keep_their_help_pages_for_residents(world, tmp_path):
    """F3: `.theme` / `.raise_prompt` are browser-side and are NOT in the table;
    the unlisted→arch rule must not swallow their pages."""
    st = _store(tmp_path)
    topics = {"theme": ".theme — a page", "raise_prompt": ".raise_prompt — a page"}
    res = FakeSession(world, name="Res", state=st, help_topics=topics)
    for verb in ("theme", "raise_prompt"):
        res.clear()
        dispatch(res, f".help {verb}")
        assert "Unknown command" not in res.text, verb
        assert "a page" in res.text


def test_deleteuser_keeps_its_own_password_gate_whatever_its_rank():
    """F4: the second gate is the CALLER's own password, checked in the session
    loop — independent of rank, so re-assigning the command cannot weaken it."""
    import inspect
    from heresay.server import TalkerServer as S
    for fn in (S._deleteuser, S._resetpassword):
        src = inspect.getsource(fn)
        assert "averify(session.resident.name" in src, fn.__name__


def test_a_non_arch_holder_still_faces_the_password_gate(tmp_path, world):
    """Granted to staff, `.deleteuser` becomes reachable — and still asks."""
    st = _store(tmp_path)
    st.commands["deleteuser"] = STAFF
    accounts = AccountStore(tmp_path / "accounts.json")
    accounts.create("Wren", "pw")
    w = FakeSession(world, name="Wiz", level="wizard", state=st, accounts=accounts,
                    config=_cfg(tmp_path))
    w.clear()
    result = dispatch(w, ".deleteuser Wren")
    assert "Unknown command" not in w.text          # reachable now
    assert result == C.DELETEUSER                   # ...and hands off to the
    assert w.pending_target == "Wren"               # two-gate prompt flow


# ---------------------------------------------------------------------------
# 9. The shipped house
# ---------------------------------------------------------------------------

def test_the_shipped_house_boots_and_seeds(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    # ⚠ RESOLVED (v1.43.0): the seed speaks in role words, the running table
    # speaks this house's rank names. On Commons that is
    # resident/staff/owner, and it is what `--write-permissions` writes.
    assert srv.state.commands == {k: srv.theme.resolve_anchor(v)
                                  for k, v in command_seed().items()}
    assert srv.state.unresolvable_commands == 0


def test_an_empty_table_reads_as_unconfigured_not_as_deny_everything(tmp_path):
    """Nobody means "lock every command to the arch" by writing `{}` — and a
    store built without a seed must not be able to brick the house either."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"version": 1, "commands": {}}), encoding="utf-8")
    st = StateStore(p, command_seed=command_seed(), ladder=DEFAULT_THEME)
    assert st.commands == command_seed()           # re-seeded

    # ...and a store that was never given a seed falls back to the registry's,
    # rather than refusing every command.
    bare = StateStore(tmp_path / "other.json")
    assert bare.commands == {}
    assert C._visible("look", "resident", DEFAULT_THEME, bare)
    assert not C._visible("gag", "resident", DEFAULT_THEME, bare)
    assert C._visible("gag", "staff", DEFAULT_THEME, bare)
