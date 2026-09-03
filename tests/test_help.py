# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.help` — one screen, GENERATED, sectioned by rank then category (v1.29.0).

Four hand-written tables (the resident grid, the short index, and two staff
screens) became one generator reading the registry crossed with the live command
table. What these tests protect:

  * that the screen CANNOT DRIFT from the permissions — reassign a command's rank
    and its entry moves to that rank's section, with no edit anywhere;
  * that what a rank is SHOWN is exactly what that rank can RUN;
  * that the rank sections appear only when there is more than one, so a
    resident's screen reads as it always has;
  * that descriptions naming a role stay theme-aware from one source;
  * and that both styles still fit both widths, with short token-safe.
"""

import tempfile
from pathlib import Path

import pytest

import heresay.commands as C
from heresay.commands import (HELP_WIDTH, NARROW_COLUMNS, dispatch, help_text,
                              staff_footer)
from heresay.commands.registry import (OWNER, CATEGORIES, CATEGORY_LABELS,
                                       COMMANDS, RESIDENT, STAFF, command_seed)
from heresay.state import StateStore
from heresay.theme import DEFAULT_THEME
from heresay.theme import Theme
from heresay.world import load_world

from conftest import FakeSession, COMMONS_THEME

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
# The reference house's ladder (Commons: resident/staff/owner), which is
# what these section-header and role-word assertions read. The engine's GENERIC
# DEFAULT_THEME is resident/staff/owner since v1.54.0 (the staff level's
# display words became staff/staff in v1.61.0).
T = COMMONS_THEME
RANKS = ("resident", "staff", "owner")
WARDENS = Theme(order=("citizen", "warden", "Steward"), staff="warden",
                level_display={"staff": ("", "wardens")})


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


@pytest.fixture
def table():
    return StateStore(Path(tempfile.mkdtemp()) / "state.json",
                      command_seed=command_seed(), ladder=T)


def rows(text):
    """The command rows — lines whose first non-space is a verb or the say form."""
    return [ln for ln in text.split("\n") if ln.strip()[:1] in (".", "<")]


# ---------------------------------------------------------------------------
# 1. Shape: fits, straight, framed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("rank", RANKS)
@pytest.mark.parametrize("style", ["long", "short"])
@pytest.mark.parametrize("width", [HELP_WIDTH, NARROW_COLUMNS])
def test_every_screen_fits_its_width(rank, style, width, table):
    for ln in help_text(T, style, rank, width, table).split("\n"):
        assert len(ln) <= width, f"{len(ln)} cols: {ln!r}"


def test_the_long_description_column_is_dead_straight(table):
    """One label column across the WHOLE screen, not per section — the arch's
    screen is where a per-section column would show."""
    text = help_text(T, "long", "owner", HELP_WIDTH, table)
    assert {ln.index(ln.split()[0]) for ln in rows(text)} == {0}
    desc_cols = set()
    for ln in rows(text):
        form = ln.strip().split("   ")[0]
        rest = ln[ln.index(form) + len(form):]
        if rest.strip():
            desc_cols.add(len(ln) - len(rest.lstrip()))
    assert len(desc_cols) == 1, sorted(desc_cols)


def test_the_screen_is_framed_and_closes_on_the_pointer(table):
    text = help_text(T, "long", "resident", HELP_WIDTH, table)
    assert text.startswith("*** Talker commands *")
    assert "More: .help <command>" in text
    assert text.rstrip().endswith("*" * HELP_WIDTH)


# ---------------------------------------------------------------------------
# 2. Sectioning by rank
# ---------------------------------------------------------------------------

def test_a_resident_screen_has_no_rank_headers(table):
    """One section means nothing to choose between: it reads as it always has."""
    for style in ("long", "short"):
        assert "[ " not in help_text(T, style, "resident", HELP_WIDTH, table)


@pytest.mark.parametrize("style", ["long", "short"])
def test_each_higher_rank_adds_its_own_section(style, table):
    staff = help_text(T, style, "staff", HELP_WIDTH, table)
    arch = help_text(T, style, "owner", HELP_WIDTH, table)
    assert "[ resident ]" in staff and "[ staff ]" in staff
    assert "[ owner ]" not in staff
    for h in ("[ resident ]", "[ staff ]", "[ owner ]"):
        assert h in arch, h


def test_the_sections_run_floor_upward(table):
    text = help_text(T, "long", "owner", HELP_WIDTH, table)
    assert text.index("[ resident ]") < text.index("[ staff ]") < \
        text.index("[ owner ]")


def test_categories_appear_only_where_that_rank_has_commands(table):
    """`moderation` is a staff thing; it must not head an empty resident block."""
    text = help_text(T, "short", "owner", HELP_WIDTH, table)
    keeper_block = text[text.index("[ staff ]"):text.index("[ owner ]")]
    resident_block = text[text.index("[ resident ]"):text.index("[ staff ]")]
    assert "moderation" in keeper_block
    assert "moderation" not in resident_block
    assert "accounts" not in resident_block


# ---------------------------------------------------------------------------
# 3. THE POINT: generated, so it cannot drift
# ---------------------------------------------------------------------------

def _section_of(text, verb):
    section = None
    for ln in text.split("\n"):
        if ln.startswith("[ "):
            section = ln.strip("[] ")
        head = ln.strip().split("   ")[0].split()
        if head[:1] == [verb]:
            return section
    return None


def test_reassigning_a_rank_moves_the_help_entry(table):
    """No code edit, no table of rows to update: the screen IS the permissions."""
    before = help_text(T, "long", "owner", HELP_WIDTH, table)
    assert _section_of(before, ".gag") == "staff"
    assert _section_of(before, ".topic") == "resident"

    table.commands["gag"] = RESIDENT
    table.commands["topic"] = OWNER

    after = help_text(T, "long", "owner", HELP_WIDTH, table)
    assert _section_of(after, ".gag") == "resident"
    assert _section_of(after, ".topic") == "owner"


def test_a_reassigned_command_reaches_the_residents_screen(table):
    assert ".gag" not in help_text(T, "long", "resident", HELP_WIDTH, table)
    table.commands["gag"] = RESIDENT
    assert ".gag" in help_text(T, "long", "resident", HELP_WIDTH, table)


def test_a_reassigned_command_leaves_the_residents_screen(table):
    assert ".topic" in help_text(T, "long", "resident", HELP_WIDTH, table)
    table.commands["topic"] = STAFF
    assert ".topic" not in help_text(T, "long", "resident", HELP_WIDTH, table)


@pytest.mark.parametrize("rank", RANKS)
def test_what_is_shown_is_exactly_what_is_runnable(rank, table):
    """The guarantee the whole phase exists for, checked command by command."""
    lines = [ln.strip() for ln in
             help_text(T, "long", rank, HELP_WIDTH, table).split("\n")]
    for c in COMMANDS:
        shown = any(ln.split("   ")[0] == form for form in c.usage for ln in lines)
        if not c.gated:
            assert shown, c.name          # speech / client verbs: always listed
            continue
        runnable = C._visible(c.name, rank, T, table)
        assert shown is runnable, (c.name, rank, shown, runnable)


def test_every_command_appears_exactly_once_on_the_arch_screen(table):
    lines = [ln.strip() for ln in
             help_text(T, "long", "owner", HELP_WIDTH, table).split("\n")]
    for c in COMMANDS:
        for form in c.usage:
            n = sum(1 for ln in lines if ln.split("   ")[0] == form)
            assert n == 1, (c.name, form, n)


# ---------------------------------------------------------------------------
# 4. Descriptions: one source, theme-aware
# ---------------------------------------------------------------------------

def test_the_role_naming_descriptions_follow_the_theme():
    text = help_text(WARDENS, "long", "Steward")
    assert "call the wardens" in text
    assert "read the shared warden letterbox" in text
    assert "Steward always in" in text
    assert " staff " not in text and " owner " not in text


def test_the_default_theme_still_says_keeper(table):
    text = help_text(T, "long", "owner", HELP_WIDTH, table)
    assert "call the staff — real trouble only" in text
    assert "owner always in" in text


def test_no_placeholder_survives_to_the_screen(table):
    for rank in RANKS:
        for style in ("long", "short"):
            text = help_text(T, style, rank, HELP_WIDTH, table)
            assert "{" not in text and "}" not in text, (rank, style)


def test_every_command_has_a_form_and_a_description():
    for c in COMMANDS:
        for form, desc in c.forms:
            assert form and desc.strip(), c.name


# ---------------------------------------------------------------------------
# 5. The alias column — the authoring constraint, now written down
# ---------------------------------------------------------------------------

def test_every_description_clears_the_alias_column(table):
    """The wide grid puts every shortcut at ONE column, which only works while
    every description is short enough to clear it. That was an unwritten rule
    until v1.29.0 — two drafts collided with it while this was being built."""
    text = help_text(T, "long", "owner", HELP_WIDTH, table)
    for c in COMMANDS:
        if not c.aliases:
            continue
        marks = "  ".join(("." + a if a.isalnum() else a) for a in c.aliases)
        line = next(ln for ln in text.split("\n")
                    if ln.strip().startswith(c.forms[0][0]) and ln.endswith(marks))
        assert line[:-len(marks)].endswith("  "), c.name


def test_short_never_splits_a_command_from_its_shortcut(table):
    for width in (HELP_WIDTH, NARROW_COLUMNS):
        text = help_text(T, "short", "owner", width, table)
        for c in COMMANDS:
            if not c.aliases:
                continue
            marks = " ".join(("." + a if a.isalnum() else a) for a in c.aliases)
            head = c.usage[0] if c.kind == "speech" else f".{c.name}"
            assert f"{head} ({marks})" in text, (c.name, width)


def test_short_uses_the_bare_dotted_name(table):
    """An index is for someone who already knows the place — and the full usage
    forms do not fit at 40."""
    text = help_text(T, "short", "resident", NARROW_COLUMNS, table)
    assert ".tell (.t >)" in text
    assert ".tell <name> <text>" not in text


# ---------------------------------------------------------------------------
# 6. What is gone
# ---------------------------------------------------------------------------

def test_the_hand_tables_are_gone():
    for name in ("_help_rows", "_help_sections", "_help_grid", "SHORT_INDEX",
                 "keeperhelp_text", "caretakerhelp_text", "staff_help_pointer",
                 "_keeper_rows", "_arch_rows", "_narrow_long_help",
                 "_narrow_short_help", "HELP_PRIMARY_WIDTH", "HELP_ALIAS_COL",
                 "WIZHELP_DESC_COL", "_alias_column"):
        assert not hasattr(C, name), name


def test_the_two_staff_verbs_are_gone():
    for verb in ("keeperhelp", "caretakerhelp"):
        assert verb not in C._COMMANDS, verb


# ---------------------------------------------------------------------------
# 7. The mail-power footer, relocated
# ---------------------------------------------------------------------------

def test_the_footer_goes_to_staff_and_not_to_residents(table):
    assert "can read anyone's mail" not in \
        help_text(T, "long", "resident", HELP_WIDTH, table)
    for rank in ("staff", "owner"):
        text = help_text(T, "long", rank, HELP_WIDTH, table)
        assert "can read anyone's mail but their own" in text
        assert "logged as facts" in text


def test_the_footer_is_theme_aware():
    text = staff_footer(WARDENS)
    assert "warden" in text and "Steward" in text
    assert " staff " not in text and " owner " not in text


def test_the_footer_wraps_at_both_widths():
    for width in (HELP_WIDTH, NARROW_COLUMNS):
        for ln in staff_footer(T, width).split("\n"):
            assert len(ln) <= width


# ---------------------------------------------------------------------------
# 8. Categories
# ---------------------------------------------------------------------------

def test_every_category_has_a_label():
    assert set(CATEGORY_LABELS) == set(CATEGORIES)
    for label in CATEGORY_LABELS.values():
        assert label and len(label) <= 16      # the short index's label column


def test_the_labels_are_the_ones_that_show(table):
    text = help_text(T, "short", "resident", HELP_WIDTH, table)
    for cat in ("talking", "world", "room", "self", "system"):
        assert CATEGORY_LABELS[cat] in text


# ---------------------------------------------------------------------------
# 9. End to end, through a session
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level,present,absent", [
    ("resident", ".look", ".gag"),
    ("wizard", ".gag", ".promote"),
    ("arch", ".promote", None),
])
def test_dispatch_renders_the_right_screen(world, level, present, absent, table):
    s = FakeSession(world, name="U" + level, level=level, state=table)
    dispatch(s, ".help")
    assert present in s.text
    if absent:
        assert absent not in s.text
