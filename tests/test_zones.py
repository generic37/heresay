# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Rank zones and the reorg collapse (v1.38.0).

Three anchors — `newb`, `floor`, `staff` — cut the ladder into four zones:

    [ newb … <floor ]  [ floor … <staff ]  [ staff … <arch ]  [ arch ]

What these tests protect:

  * ⛔ **Commons is unchanged.** The defaults collapse the first two zones
    (`newb` and `floor` both default to `order[0]`), so a house that has never
    heard of this keeps behaving exactly as it did — no newb rung, new accounts
    on the floor, no collapse. That is the first and last thing checked here.
  * the anchors validate STRICTLY (a bad name or an inverted pair refuses the
    boot) while the rest of theme.toml still falls back — the two postures, and
    why they differ.
  * new accounts start at `newb`;
  * ⚠ the collapse runs ONLY when the ladder's shape changed. A normal restart
    must never touch a rank, or a nightly reboot would quietly undo `.promote`.
"""

import json
from pathlib import Path

import pytest

from heresay.accounts import AccountStore, collapse_rank
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.theme import DEFAULT_THEME, Theme, ThemeError, load_theme

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"

# A house that has actually reorganised: a probationary rung at the bottom, two
# staff rungs, and the anchors placed by hand.
BIG = Theme(order=("newcomer", "citizen", "warden", "High Warden", "Steward"),
            resident="citizen", staff="warden")


def _theme_file(tmp_path, ladder, name="theme.toml"):
    f = tmp_path / name
    order = ", ".join(f'"{r}"' for r in ladder.order)
    f.write_text(f'[ranks]\norder = [{order}]\nnewb = "{ladder.newb_rank}"\n'
                 f'resident = "{ladder.resident_rank}"\nstaff = "{ladder.staff_rank}"\n',
                 encoding="utf-8")
    return f


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
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        reload_poll_seconds=0,
    )
    base.update(over)
    return Config(**base)


def _write_theme(text, tmp_path):
    f = tmp_path / "theme.toml"
    f.write_text(text, encoding="utf-8")
    return f


# ---------------------------------------------------------------------------
# ⛔ Commons, unchanged
# ---------------------------------------------------------------------------

def test_the_shipped_ladder_has_no_newb_zone():
    """The default that makes the whole feature invisible to an existing house:
    `newb` and `floor` are the same rank, so the newb zone is empty."""
    t = DEFAULT_THEME                            # the GENERIC ladder (v1.54.0)
    assert t.newb_rank == t.resident_rank == "resident"
    assert t.staff_rank == "staff" and t.owner_rank == "owner"
    assert t.newb_index == t.resident_index == 0


def test_the_shipped_theme_file_needs_no_anchors():
    """house/theme.toml is untouched by this release, and still loads to the
    same ladder it always did."""
    shipped = Path(__file__).resolve().parent / "fixtures" / "house" / "theme.toml"
    t = load_theme(shipped)
    assert t.order == ("resident", "staff", "owner")
    assert t.newb_rank == t.resident_rank == "resident"
    assert t.staff_rank == "staff"


def test_a_commons_shaped_house_never_collapses(tmp_path):
    """No config change, no reorg — asserted through a real boot, twice, with a
    promoted account in between."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.accounts.create("Wren", "pw-one-two-three")
    srv.accounts.set_rank("Wren", "staff")
    assert srv.accounts.reorg_ran is False

    again = TalkerServer(_cfg(tmp_path))
    assert again.accounts.reorg_ran is False
    assert again.accounts.get("Wren").rank == "staff"    # ⚠ still a staff


# ---------------------------------------------------------------------------
# The anchors
# ---------------------------------------------------------------------------

def test_the_anchors_default_to_the_bottom_and_to_staff(tmp_path):
    t = load_theme(_write_theme('[ranks]\norder = ["a", "b", "c"]\nstaff = "b"\n',
                                tmp_path))
    assert t.newb_rank == "a" and t.resident_rank == "a"
    assert t.staff_rank == "b" and t.owner_rank == "c"


def test_the_anchors_can_be_placed(tmp_path):
    t = load_theme(_theme_file(tmp_path, BIG))
    assert (t.newb_rank, t.resident_rank, t.staff_rank, t.owner_rank) == \
        ("newcomer", "citizen", "warden", "Steward")


@pytest.mark.parametrize("body,why", [
    ('[ranks]\norder = ["a","b","c"]\nresident = "nope"\n', "an unknown resident anchor"),
    ('[ranks]\norder = ["a","b","c"]\nresident = "c"\nstaff = "b"\n', "resident above staff"),
])
def test_a_bad_anchor_refuses_the_boot(tmp_path, body, why):
    """⚠ The ONE strict thing in theme.toml, and the reasoning is what it DOES:
    a typo in `instance_name` is cosmetic, a typo in `floor` decides who counts
    as established — and unlike `staff` there is no restrictive direction to
    fall back to. Refusing is the only reading that does not pick a policy on
    the operator's behalf.

    ⚠ v1.41.0 removed two cases from this list, and removing them was the fix:
    `newb` is DERIVED now (`order[0]`), so "an unknown newb" and "newb above
    floor" are not mistakes anybody can make. See the retirement test below.
    """
    with pytest.raises(ThemeError):
        load_theme(_write_theme(body, tmp_path))


def test_a_retired_newb_key_warns_and_is_ignored(tmp_path, caplog):
    """⚠ `newb` was a SETTING in v1.38.0 and is derived in v1.41.0. A house that
    already wrote the line must not be closed by it — the same posture as
    world.py's retired `entry:`/`jail:` headers: accepted, ignored, and warned
    about, so the ignoring is never silent."""
    with caplog.at_level("WARNING"):
        t = load_theme(_write_theme(
            '[ranks]\norder = ["a","b","c"]\nnewb = "b"\nresident = "b"\nstaff = "c"\n',
            tmp_path))
    assert t.newb_rank == "a"                    # derived, not what was written
    assert t.resident_rank == "b"                   # the configured one still counts
    assert "newb is retired" in caplog.text


def test_a_nonsense_newb_key_is_not_even_an_error(tmp_path, caplog):
    """It names nothing on the ladder, and that is fine: nothing reads it."""
    with caplog.at_level("WARNING"):
        t = load_theme(_write_theme(
            '[ranks]\norder = ["a","b","c"]\nnewb = "nope"\n', tmp_path))
    assert t.newb_rank == "a"


def test_newb_is_always_the_bottom_of_the_ladder():
    """⛔ The A-5 fix at the root. While `newb` was configurable a rank could sit
    BELOW it — reachable, because `.demote` floors at `order[0]` — and such a
    rank fell outside every zone, so the reorg collapse RAISED its holder to
    `newb`. A collapse that promotes anybody is the wrong shape of migration.
    Nothing can be below the bottom, so the orphan cannot exist."""
    for order in (("a","b"), ("a","b","c"), ("a","b","c","d","e")):
        t = Theme(order=order, staff=order[-2])
        assert t.newb_rank == order[0]
        assert t.newb_index == 0
        # ...and therefore no rank can collapse UPWARDS.
        for r in order:
            assert t.index_or_floor(t.level_anchor(r)) <= t.index_or_floor(r), r


def test_everything_else_in_the_file_still_falls_back(tmp_path, caplog):
    """The posture the anchors are the exception to, kept: a malformed `order`
    or a `staff` naming nothing is still a warning and a fallback, because a
    words file with a typo must not close the house."""
    with caplog.at_level("WARNING"):
        t = load_theme(_write_theme('[ranks]\norder = "not a list"\n', tmp_path))
    assert t.order == DEFAULT_THEME.order
    with caplog.at_level("WARNING"):
        t = load_theme(_write_theme('[ranks]\norder = ["a","b"]\nstaff = "x"\n',
                                    tmp_path))
    assert t.staff_rank == "b"                       # the top rank alone


# ---------------------------------------------------------------------------
# The zones
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rank,zone", [
    ("newcomer", "newcomer"),
    ("citizen", "citizen"),
    ("warden", "warden"),
    ("High Warden", "warden"),       # a second staff rung is still the staff zone
    ("Steward", "warden"),           # arch is handled BY NAME, not by zone
    ("gone", "newcomer"),            # ⚠ a vanished rank -> the LEAST authority
    ("", "newcomer"),
])
def test_zone_anchor(rank, zone):
    assert BIG.level_anchor(rank) == zone


def test_an_unknown_rank_zones_to_newb_not_the_floor():
    """Everywhere else in the engine an unknown rank reads as the FLOOR, because
    the floor was the bottom. With a newb zone it no longer is, and "we cannot
    place you" has to mean the least authority."""
    assert BIG.level_anchor("who?") == BIG.newb_rank != BIG.resident_rank


# ---------------------------------------------------------------------------
# New accounts start at newb
# ---------------------------------------------------------------------------

def test_a_new_account_starts_at_newb(tmp_path):
    store = AccountStore(tmp_path / "accounts.json", ladder=BIG)
    acct = store.create("Wren", "pw-one-two-three")
    assert acct.rank == "newcomer"
    # ...and it is WRITTEN, not left empty: an empty rank reads as the floor
    # everywhere, which on this ladder would be a free promotion at sign-up.
    on_disk = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert on_disk["accounts"]["wren"]["rank"] == "newcomer"


def test_on_the_shipped_ladder_that_is_still_the_floor(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    assert store.create("Wren", "pw-one-two-three").rank == "resident"


@pytest.mark.asyncio
async def test_a_new_account_is_a_resident_end_to_end(tmp_path):
    """Commons's sign-up, unchanged."""
    srv = TalkerServer(_cfg(tmp_path))
    acct = await srv.accounts.acreate("Corvid", "pw-one-two-three")
    assert acct.rank == "resident"
    assert srv._rank_for(acct) == "resident"


# ---------------------------------------------------------------------------
# The reorg collapse
# ---------------------------------------------------------------------------

def _accounts_file(tmp_path, ranks, fingerprint=None):
    payload = {"version": 3,
               "accounts": {k.casefold(): {"name": k, "password_hash": "x",
                                           "rank": v}
                            for k, v in ranks.items()}}
    if fingerprint is not None:
        payload["rank_fingerprint"] = fingerprint
    (tmp_path / "accounts.json").write_text(json.dumps(payload), encoding="utf-8")


def test_the_fingerprint_covers_the_shape_and_nothing_else():
    # The generic default shape (v1.54.0), built by hand, is the same shape.
    same = Theme(order=("resident", "staff", "owner"), staff="staff")
    assert same.rank_fingerprint() == DEFAULT_THEME.rank_fingerprint()
    # Writing the defaults out explicitly is the same shape.
    explicit = Theme(order=("resident", "staff", "owner"),
                     resident="resident", staff="staff")
    assert explicit.rank_fingerprint() == DEFAULT_THEME.rank_fingerprint()
    # Cosmetics are not shape.
    assert Theme(instance_name="Elsewhere", favicon="x.png",
                 level_display={"staff": ("", "keeperfolk")}).rank_fingerprint() \
        == DEFAULT_THEME.rank_fingerprint()
    # A new rung, or a moved anchor, is.
    assert BIG.rank_fingerprint() != DEFAULT_THEME.rank_fingerprint()
    moved = Theme(order=BIG.order, resident="warden", staff="warden")
    assert moved.rank_fingerprint() != BIG.rank_fingerprint()


def test_the_first_load_after_the_upgrade_records_but_does_not_collapse(tmp_path):
    """⚠ An existing accounts.json has no fingerprint. Nothing has CHANGED — the
    file simply predates the field — and demoting a house for upgrading would be
    the worst possible introduction to the feature."""
    _accounts_file(tmp_path, {"Wren": "staff", "Bob": "resident"})
    store = AccountStore(tmp_path / "accounts.json")
    assert store.reorg_ran is False
    assert store.get("Wren").rank == "staff"
    on_disk = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert on_disk["rank_fingerprint"] == DEFAULT_THEME.rank_fingerprint()


def test_an_unchanged_ladder_never_touches_a_rank(tmp_path):
    _accounts_file(tmp_path, {"Wren": "staff"},
                   fingerprint=DEFAULT_THEME.rank_fingerprint())
    store = AccountStore(tmp_path / "accounts.json")
    assert store.reorg_ran is False and store.reorg_collapsed == 0
    assert store.get("Wren").rank == "staff"


def test_a_reorg_collapses_every_zone_to_its_anchor(tmp_path):
    """The whole behaviour, in one table. Everyone lands at the bottom of the
    tier they were in, and is re-promoted deliberately."""
    _accounts_file(tmp_path, {
        "Generic": "owner",       # the arch, by NAME
        "Chief": "High Warden",       # a second staff rung -> the staff anchor
        "Wiz": "warden",              # already the staff anchor
        "Old": "citizen",             # the floor zone
        "New": "newcomer",            # already newb
        "Ghost": "wizard",            # a rank this ladder never had -> newb
    }, fingerprint="something-else")
    store = AccountStore(tmp_path / "accounts.json", ladder=BIG,
                         owner_name="Generic")
    assert store.reorg_ran is True
    got = {a.name.casefold(): a.rank for a in store.all()}
    assert got == {
        "generic": "Steward",         # arch, always
        "chief": "warden",            # collapsed down its tier
        "wiz": "warden",
        "old": "citizen",
        "new": "newcomer",
        "ghost": "newcomer",          # unplaceable -> the least authority
    }
    assert store.reorg_collapsed == 3          # Generic, Chief, Ghost moved


def test_the_arch_is_never_demoted_by_a_collapse(tmp_path):
    """Identified by NAME, not by rank: the owner is named in config and holds
    the top rank regardless of what is stored, so a collapse that read their
    rank would be reading the wrong thing — and this is the one account a
    collapse must never be able to move down."""
    _accounts_file(tmp_path, {"Generic": "newcomer"}, fingerprint="was-different")
    store = AccountStore(tmp_path / "accounts.json", ladder=BIG,
                         owner_name="Generic")
    assert store.get("Generic").rank == "Steward"


def test_the_collapse_writes_once_and_then_settles(tmp_path):
    _accounts_file(tmp_path, {"Wren": "High Warden"}, fingerprint="was-different")
    AccountStore(tmp_path / "accounts.json", ladder=BIG)
    on_disk = json.loads((tmp_path / "accounts.json").read_text(encoding="utf-8"))
    assert on_disk["accounts"]["wren"]["rank"] == "warden"
    assert on_disk["rank_fingerprint"] == BIG.rank_fingerprint()

    second = AccountStore(tmp_path / "accounts.json", ladder=BIG)
    assert second.reorg_ran is False            # settled
    assert second.get("Wren").rank == "warden"


def test_the_collapse_runs_through_a_real_boot(tmp_path):
    """End to end: a house reorganises its theme.toml and restarts.

    ⚠ And it shows the sharp edge, which is the honest thing to pin: Wren was a
    `staff`, and the new ladder has no such word. The engine cannot know that
    `staff` used to be the staff rank — the word is simply gone — so Wren lands
    at NEWB, not in the staff zone. Renaming a rank across a reorg drops its
    holders to the bottom, and that is precisely why the collapse is announced
    and the operator re-promotes.
    """
    srv = TalkerServer(_cfg(tmp_path))
    srv.accounts.create("Wren", "pw-one-two-three")
    srv.accounts.set_rank("Wren", "staff")

    reorganised = TalkerServer(_cfg(tmp_path,
                                    theme_file=str(_theme_file(tmp_path, BIG))))
    assert reorganised.accounts.reorg_ran is True
    assert reorganised.accounts.get("Wren").rank == "newcomer"
    assert any("reorganised" in f for f in reorganised.load_report.facts)


def test_a_reorg_that_KEEPS_its_words_keeps_the_tiers(tmp_path):
    """The other half of the same point: insert a rung without renaming
    anything, and everybody stays in the tier they were in — a staff is still
    staff, because the word survived."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.accounts.create("Wren", "pw-one-two-three")
    srv.accounts.set_rank("Wren", "staff")

    # ⚠ "novice", not "newb": `newb` joined `floor`/`staff`/`arch` as a reserved
    # ANCHOR word in v1.38.0, so a rung may not take it — the same rule those
    # three have always had, and for the same reason (a permissions value would
    # be ambiguous between the role and the rung).
    wider = Theme(order=("novice", "resident", "staff", "High Staff",
                         "owner"),
                  resident="resident", staff="staff")
    again = TalkerServer(_cfg(tmp_path,
                              theme_file=str(_theme_file(tmp_path, wider))))
    assert again.accounts.reorg_ran is True
    assert again.accounts.get("Wren").rank == "staff"       # still staff
    assert again.theme.is_staff_rank("staff")


def test_collapse_rank_is_the_documented_table():
    for stored, expected in (("High Warden", "warden"), ("warden", "warden"),
                             ("citizen", "citizen"), ("newcomer", "newcomer"),
                             ("nonesuch", "newcomer")):
        assert collapse_rank(stored, BIG) == expected
    assert collapse_rank("newcomer", BIG, is_arch=True) == "Steward"


def test_newb_is_a_reserved_rank_name(tmp_path, caplog):
    """⚠ `newb` joined `floor`/`staff`/`arch` as an anchor word, so a rung may
    no longer be called that — a permissions value would otherwise be ambiguous
    between the role and the rung. Dropped and logged, like the other three:
    never fatal, because it is still a words file."""
    from heresay.theme import ANCHOR_NAMES
    assert "newb" in ANCHOR_NAMES
    with caplog.at_level("WARNING"):
        t = load_theme(_write_theme(
            '[ranks]\norder = ["newb", "resident", "staff", "owner"]\n',
            tmp_path))
    assert "newb" not in t.order
    assert "may not contain 'newb'" in caplog.text
