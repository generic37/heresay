# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The rank ladder as ordered DATA (v1.27.0).

Three hardcoded levels became a configurable ordered ladder. What these tests
protect:

  * that the ladder really is data — a FOURTH rank works with no code change at
    all, in the ladder, in `is_staff()`, and in both gate thresholds;
  * that the `level` -> `rank` migration reads every old account correctly, once,
    and fails SAFE (unknown -> the floor) rather than open;
  * that the arch is still an IDENTITY — the config-named owner holds the top
    rank whatever is stored, which the eleven `owner_name` guards depend on;
  * that a rank name and an account name can never be the same word;
  * and that Commons, on the shipped three-rank ladder, behaves exactly as it
    did in v1.26.2.
"""

import json
import logging
from pathlib import Path

import pytest

import heresay.commands as C
from heresay.accounts import Account, AccountStore
from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer, reserved_names
from heresay.theme import DEFAULT_ORDER, DEFAULT_THEME, Theme, load_theme
from heresay.world import Resident, load_world

from conftest import FakeSession, COMMONS_THEME

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"

# The SHIPPED ladder is Commons's (resident/staff/owner), not the engine's
# GENERIC built-in DEFAULT_THEME (resident/staff/owner) since v1.54.0. Tests that
# exercise the reference house's own behaviour build it explicitly.
WL = COMMONS_THEME

# A four-rank ladder, the new rung sitting BELOW the staff line: "novice" is not
# staff, "staff" still is. Pure config — no code knows this exists.
FOUR = Theme(order=("novice", "resident", "staff", "owner"), staff="staff",
             level_display={"staff": ("", "staff")})


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


# ---------------------------------------------------------------------------
# 1. The ladder derives from `order`
# ---------------------------------------------------------------------------

def test_the_ladder_is_the_order_list():
    t = DEFAULT_THEME                            # the GENERIC ladder (v1.54.0)
    assert t.order == DEFAULT_ORDER
    assert [t.rank_index(n) for n in t.order] == [0, 1, 2]
    assert (t.resident_rank, t.staff_rank, t.owner_rank) == \
        ("resident", "staff", "owner")
    assert t.grantable_rank == "staff"         # the rung just below owner


def test_the_old_level_dict_is_gone():
    """`_LEVEL_RANK` was the entire definition of the ladder; it must not linger
    as a second source of truth."""
    assert not hasattr(C, "_LEVEL_RANK")
    assert not hasattr(C, "_rank")


def test_an_unknown_rank_reads_as_the_floor():
    """The fail-safe direction, everywhere: never MORE authority than the least."""
    t = DEFAULT_THEME
    assert t.rank_index("wizard") is None
    assert t.index_or_floor("wizard") == 0
    assert t.index_or_floor("") == 0
    assert not t.is_staff_rank("wizard")


def test_a_four_rank_ladder_needs_no_code_change():
    assert FOUR.rank_index("novice") == 0
    assert FOUR.resident_rank == "novice"
    assert FOUR.owner_rank == "owner"
    assert FOUR.staff_rank == "staff"
    assert FOUR.grantable_rank == "staff"


# ---------------------------------------------------------------------------
# 2. `[ranks]` loading — validation is never fatal
# ---------------------------------------------------------------------------

def _theme_file(tmp_path, body):
    p = tmp_path / "theme.toml"
    p.write_text(body, encoding="utf-8")
    return load_theme(p)


def test_order_is_read_from_the_file(tmp_path):
    t = _theme_file(tmp_path, '[ranks]\norder=["citizen","warden","Steward"]\n')
    assert t.order == ("citizen", "warden", "Steward")
    assert t.staff_rank == "warden"        # defaults to the rung below arch
    assert t.staff_plural == "wardens"     # follows `staff`, not the shipped word


def test_a_four_rank_order_loads(tmp_path):
    t = _theme_file(tmp_path,
                    '[ranks]\norder=["novice","resident","staff","owner"]\n'
                    'staff="staff"\n')
    assert t.order == ("novice", "resident", "staff", "owner")
    assert t.is_staff_rank("staff") and not t.is_staff_rank("resident")


def test_a_garbled_order_falls_back_and_is_never_fatal(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        t = _theme_file(tmp_path, '[ranks]\norder="not a list"\n')
    assert t.order == DEFAULT_ORDER
    assert "order must be a list" in caplog.text


def test_too_short_an_order_falls_back(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        t = _theme_file(tmp_path, '[ranks]\norder=["only"]\n')
    assert t.order == DEFAULT_ORDER


def test_a_duplicate_rank_keeps_the_first(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        t = _theme_file(tmp_path, '[ranks]\norder=["a","b","a","c"]\n')
    assert t.order == ("a", "b", "c")


def test_staff_naming_no_rank_means_top_only_and_no_error(tmp_path, caplog):
    """Restrictive, never a refusal to start."""
    with caplog.at_level(logging.WARNING):
        t = _theme_file(tmp_path,
                        '[ranks]\norder=["resident","staff","owner"]\n'
                        'staff="nobody"\n')
    assert t.staff_rank == "owner"
    assert t.is_staff_rank("owner")
    assert not t.is_staff_rank("staff")
    assert "not in order" in caplog.text


def _v2_file(tmp_path, records):
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps({"version": 2, "accounts": records}), encoding="utf-8")
    return p


def test_a_PRE_V3_FILE_NO_LONGER_MIGRATES(tmp_path):
    """⛔ `migrate_level_to_rank` was DELETED in v1.60.0 with the rest of the
    back-compat layer. A schema-v2 `accounts.json` — last written before v1.27.0
    — therefore comes back with no `rank` at all.

    ⚠ That is a demotion, not a crash, and it is the fail-safe direction: an
    unknown or absent rank reads as the floor everywhere, so such a house opens
    with everybody a resident and the owner (who is named in config, not by
    rank) still able to promote them back. Pinned so the consequence is written
    down rather than discovered.
    """
    p = _v2_file(tmp_path, {
        "wizzy": {"name": "Wizzy", "password_hash": "x", "level": "wizard"}})
    store = AccountStore(p)
    assert store.get("Wizzy").rank == ""


def test_a_v3_file_ignores_a_stray_level_field(tmp_path):
    """From v3 on, `rank` is authoritative and `level` is never consulted."""
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps({"version": 3, "accounts": {"w": {
        "name": "W", "password_hash": "x", "rank": "resident", "level": "wizard"}}}),
        encoding="utf-8")
    assert AccountStore(p).get("W").rank == "resident"


def test_unknown_stored_ranks_are_counted(tmp_path):
    """R2: a rank RENAME demotes its holders — silently, unless someone counts."""
    p = tmp_path / "accounts.json"
    p.write_text(json.dumps({"version": 3, "accounts": {
        "a": {"name": "A", "password_hash": "x", "rank": "staff"},
        "b": {"name": "B", "password_hash": "x", "rank": "warden"},
        "c": {"name": "C", "password_hash": "x", "rank": "warden"},
    }}), encoding="utf-8")
    # On the Commons ladder "staff" is known and "warden" is the orphan.
    assert AccountStore(p, ladder=WL).unknown_ranks == 2       # the two wardens
    # The count is symmetric, which is the point: on the ladder that knows
    # "warden" it is "staff" that has become the orphan.
    ok = AccountStore(p, ladder=Theme(order=("resident", "warden", "owner"),
                                      staff="warden"))
    assert ok.unknown_ranks == 1                   # the one staff


# ---------------------------------------------------------------------------
# 4. is_staff() is a threshold
# ---------------------------------------------------------------------------

def test_is_staff_reads_the_threshold(world):
    for rank, staff in (("resident", False), ("staff", True), ("owner", True)):
        r = Resident("X", "", world.start_room, rank=rank, ladder=WL)
        assert r.is_staff() is staff, rank


def test_a_rank_below_the_staff_line_is_not_staff(world):
    assert not Resident("N", "", world.start_room, rank="novice",
                        ladder=FOUR).is_staff()
    assert Resident("K", "", world.start_room, rank="staff", ladder=FOUR).is_staff()


def test_an_unknown_rank_is_not_staff(world):
    assert not Resident("X", "", world.start_room, rank="wizard").is_staff()


def test_set_rank_keeps_the_ladder_in_step(world):
    r = Resident("X", "", world.start_room, rank="resident", ladder=WL)
    assert not r.is_staff()
    r.set_rank("staff")
    assert r.is_staff() and r.rank == "staff"
    r.set_rank("")                       # empty means the floor
    assert r.rank == "resident" and not r.is_staff()


# ---------------------------------------------------------------------------
# 5. The gate: both tiers are thresholds
# ---------------------------------------------------------------------------

ARCH_VERB, KEEPER_VERB = "promote", "gag"


def test_the_arch_gate_is_a_threshold_not_an_equality():
    """It used to be `level == "arch"`. On three ranks that is the same
    predicate — which is exactly why the change is safe to make now."""
    t = WL                                       # the Commons ladder
    assert C._visible(ARCH_VERB, "owner", t)
    assert not C._visible(ARCH_VERB, "staff", t)
    assert not C._visible(ARCH_VERB, "resident", t)


def test_the_keeper_gate_is_a_threshold():
    t = WL                                       # the Commons ladder
    assert C._visible(KEEPER_VERB, "staff", t)
    assert C._visible(KEEPER_VERB, "owner", t)     # above inherits
    assert not C._visible(KEEPER_VERB, "resident", t)


def test_an_open_verb_is_open_to_everyone():
    for rank in ("resident", "staff", "owner", "wizard", ""):
        assert C._visible("look", rank, DEFAULT_THEME)


def test_a_fourth_rank_inherits_by_threshold():
    """The whole point: a rung added below arch gets the right powers for free."""
    assert not C._visible(KEEPER_VERB, "novice", FOUR)
    assert not C._visible(ARCH_VERB, "novice", FOUR)
    assert C._visible(KEEPER_VERB, "staff", FOUR)
    assert not C._visible(ARCH_VERB, "staff", FOUR)
    assert C._visible(ARCH_VERB, "owner", FOUR)


def test_gating_end_to_end_on_a_four_rank_house(world):
    novice = FakeSession(world, name="Nov", rank="novice", theme=FOUR)
    staff = FakeSession(world, name="Kee", rank="staff", theme=FOUR)
    for s, seen in ((novice, False), (staff, True)):
        s.clear()
        dispatch(s, ".gag Nobody")
        assert ("Unknown command: .gag" in s.text) is not seen


# ---------------------------------------------------------------------------
# 6. .promote / .demote step one rung
# ---------------------------------------------------------------------------

def _promo_world(world, tmp_path, theme=None):
    store = AccountStore(tmp_path / "accounts.json", ladder=theme or DEFAULT_THEME)
    store.create("Generic", "pw")
    store.create("Wren", "pw")
    arch = FakeSession(world, name="Generic", accounts=store, level="arch",
                       theme=theme, config=_cfg(tmp_path))
    return store, arch


def test_promote_steps_up_one_and_caps_below_arch(world, tmp_path):
    store, arch = _promo_world(world, tmp_path)
    dispatch(arch, ".promote Wren")
    assert store.get("Wren").rank == "staff"
    arch.clear()
    dispatch(arch, ".promote Wren")          # cap: never reaches owner
    assert store.get("Wren").rank == "staff"
    assert "rank is already staff" in arch.text
    assert "the highest you can set" in arch.text


def test_demote_steps_down_one_and_floors(world, tmp_path):
    store, arch = _promo_world(world, tmp_path)
    store.set_rank("Wren", "staff")
    dispatch(arch, ".demote Wren")
    assert store.get("Wren").rank == "resident"
    arch.clear()
    dispatch(arch, ".demote Wren")
    assert store.get("Wren").rank == "resident"
    assert "the lowest you can set" in arch.text


def test_promote_climbs_a_four_rank_ladder_one_rung_at_a_time(world, tmp_path):
    store, arch = _promo_world(world, tmp_path, theme=FOUR)
    store.set_rank("Wren", "novice")
    dispatch(arch, ".promote Wren")
    assert store.get("Wren").rank == "resident"     # one step, not a jump
    dispatch(arch, ".promote Wren")
    assert store.get("Wren").rank == "staff"
    arch.clear()
    dispatch(arch, ".promote Wren")
    assert store.get("Wren").rank == "staff"       # still capped below arch


def test_the_arch_owners_rank_is_still_fixed_in_config(world, tmp_path):
    store, arch = _promo_world(world, tmp_path)
    arch.clear()
    dispatch(arch, ".promote Generic")
    assert "rank is fixed in config" in arch.text


# ---------------------------------------------------------------------------
# 7. De-hardcoded words follow the config
# ---------------------------------------------------------------------------

WARDENS = Theme(order=("citizen", "warden", "Steward"), staff="warden",
                level_display={"staff": ("", "wardens")})


def test_the_staff_call_lines_follow_the_ladder(world):
    """⚠ The assertion is the POSITIVE one, and precisely: the rendered line uses
    the theme's own plural. ⛔ The negative it used to carry — `"staff" not in
    res.text` — could not survive a differently-themed tree: on a ladder whose
    staff word is `staff`, the word also appears in the command echo, so the
    check failed for a reason that had nothing to do with the ladder."""
    res = FakeSession(world, name="Res", theme=WARDENS)
    dispatch(res, ".call_staff the roof is on fire")
    assert WARDENS.level_plural("staff") == "wardens"      # the theme under test
    assert "No wardens" in res.text


def test_the_letterbox_lines_follow_the_ladder(world, tmp_path):
    from heresay.mail import MailStore
    mail = MailStore(tmp_path / "m.json", tmp_path / "m.key")
    w = FakeSession(world, name="W", rank="warden", theme=WARDENS, mail=mail)
    dispatch(w, ".staffmail")
    assert "No warden mail." in w.text


def test_the_examine_rank_line_follows_the_ladder(world, tmp_path):
    store = AccountStore(tmp_path / "accounts.json", ladder=WARDENS)
    store.create("Wren", "pw")
    store.set_rank("Wren", "warden")
    s = FakeSession(world, name="Looker", accounts=store, theme=WARDENS)
    dispatch(s, ".examine Wren")
    assert "Their rank here is warden." in s.text


def test_the_help_sections_are_named_for_the_ladders_ranks():
    """`staff_help_pointer` and the two staff screens are gone (v1.29.0); the one
    generated screen headers its sections with the ladder's own words."""
    from heresay.commands import help_text
    text = help_text(WARDENS, "long", "Steward")
    assert "[ citizen ]" in text and "[ warden ]" in text and "[ Steward ]" in text
    assert " staff " not in text and " owner " not in text


def test_the_staff_footer_follows_the_ladder():
    from heresay.commands import help_text, staff_footer
    assert "warden" in staff_footer(WARDENS) and "Steward" in staff_footer(WARDENS)
    text = help_text(WARDENS, "long", "warden")
    assert "can read anyone's mail but their own" in text
    assert " staff " not in text and " owner " not in text


# ---------------------------------------------------------------------------
# 8. Reserved names, and the rank/account collision
# ---------------------------------------------------------------------------

def test_every_rank_name_is_reserved():
    """⚠ v1.44.0 RETIRED the `{"staff", "owner"}` hardcode. On Commons
    both are RANK names, so the rank rule already covered them and the constant
    said nothing the ladder did not — while on a house with another vocabulary it
    reserved two words that house has no use for, which is the operator's call
    and not the engine's. They can have them back in `forbidden_names.txt`."""
    assert reserved_names(WL) >= {"staff", "owner", "resident"}
    # A re-themed house reserves ITS words, and ONLY its words.
    r = reserved_names(WARDENS)
    assert {"citizen", "warden", "steward"} <= r
    assert not ({"staff", "owner"} & r)
    # ...unless the operator says so, which is the whole point of the file.
    assert "staff" in reserved_names(WARDENS, {"staff"})


def test_a_rank_that_collides_with_an_account_refuses_to_boot(tmp_path):
    """A rank word IS an identity — the house must not run with both."""
    theme = tmp_path / "theme.toml"
    theme.write_text('[ranks]\norder=["resident","warden","owner"]\n'
                     'staff="warden"\n', encoding="utf-8")
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Warden", "pw")               # the collision, cased differently
    with pytest.raises(ValueError) as e:
        TalkerServer(_cfg(tmp_path, theme_file=str(theme)))
    msg = str(e.value)
    assert 'rank "warden"' in msg
    assert 'account "Warden"' in msg
    assert ".deleteuser" in msg


def test_deleting_the_colliding_account_resolves_it(tmp_path):
    theme = tmp_path / "theme.toml"
    theme.write_text('[ranks]\norder=["resident","warden","owner"]\n'
                     'staff="warden"\n', encoding="utf-8")
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Warden", "pw")
    store.delete("Warden")
    TalkerServer(_cfg(tmp_path, theme_file=str(theme)))     # boots


def test_renaming_the_rank_also_resolves_it(tmp_path):
    theme = tmp_path / "theme.toml"
    theme.write_text('[ranks]\norder=["resident","staff","owner"]\n',
                     encoding="utf-8")
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Warden", "pw")
    TalkerServer(_cfg(tmp_path, theme_file=str(theme)))     # boots


def test_the_shipped_house_boots(tmp_path):
    """Commons's own ladder collides with nothing."""
    TalkerServer(_cfg(tmp_path))


# ---------------------------------------------------------------------------
# 9. Commons is behaviour-identical
# ---------------------------------------------------------------------------

def test_the_shipped_ladder_is_the_v1_26_behaviour(world):
    """Same three ranks, same staff line, same gate answers, same words."""
    t = WL                                       # the shipped ladder is Commons's
    assert t.order == ("resident", "staff", "owner")
    # Staff exactly where it was.
    assert [t.is_staff_rank(r) for r in t.order] == [False, True, True]
    # The two tiers answer exactly as the pre-1.27 sets did.
    assert [C._visible("gag", r, t) for r in t.order] == [False, True, True]
    assert [C._visible("promote", r, t) for r in t.order] == [False, False, True]


def test_the_shipped_words_are_unchanged(world):
    """The de-hardcoding must not have moved a single default word."""
    res = FakeSession(world, name="Res")
    dispatch(res, ".call_staff help")
    assert "Your call has gone out to the staff." in res.text or "No staff awake" in res.text
    w = FakeSession(world, name="Wiz", level="wizard")
    dispatch(w, ".gag Res")
    assert "You have been gagged by the staff." in res.text
