# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""theme.toml — the RANK LADDER and the words that come with it.

Since v1.27.0 `[ranks] order` IS the ladder and a rank's name is both its
identity and its display word; the internal identifiers that remain (the config
arch name, command verbs, STAFF_BOX) never change."""

from pathlib import Path

import pytest

import heresay.commands as C
from heresay.accounts import AccountStore
from heresay.commands import dispatch, help_text
from heresay.theme import Theme, DEFAULT_THEME, load_theme

from conftest import FakeSession, COMMONS_THEME

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


# ---- loading -------------------------------------------------------------

def test_defaults_when_missing(tmp_path):
    # Missing theme.toml -> the built-in GENERIC ladder (v1.54.0), not
    # Commons's. A house names its own ranks in house/theme.toml.
    t = load_theme(tmp_path / "absent.toml")
    assert t.order == ("resident", "staff", "owner")
    assert (t.resident_rank, t.staff_rank, t.owner_rank) == \
        ("resident", "staff", "owner")
    # ⚠ The plural is NOT the naive one here (v1.61.0): the resolved singular
    # is the engine's own default word for this level, so `level_plural`
    # returns the default's own plural — "staff", not "staffs". A house word
    # the engine did not choose still pluralises naively.
    assert (t.staff, t.staff_plural) == ("staff", "staff")


def test_default_theme_matches():
    assert DEFAULT_THEME == Theme()


def test_custom_theme_overrides(tmp_path):
    p = tmp_path / "theme.toml"
    p.write_text(
        '[ranks]\norder=["citizen","warden","Steward"]\n'
        'staff="warden"\nstaff_plural="wardens"\n', encoding="utf-8")
    t = load_theme(p)
    assert (t.staff_rank, t.owner_rank, t.staff_plural) == \
        ("warden", "Steward", "wardens")
    assert t.resident_rank == "citizen"


def test_partial_theme_fills_from_defaults(tmp_path):
    p = tmp_path / "theme.toml"
    p.write_text('[ranks]\nstaff="staff"\n', encoding="utf-8")
    t = load_theme(p)
    # Only `staff` is set; `order` is unset and the generic default fills it,
    # so the owner rung is that ladder's order[-1].
    assert t.staff_rank == "staff" and t.owner_rank == "owner"


# ---- instance_name -------------------------------------------------------

def test_instance_name_defaults_to_the_colourless_placeholder():
    """⚠ The ENGINE's default is deliberately NOT a house name. A house that
    has not named itself should read as unnamed rather than borrow one."""
    assert DEFAULT_THEME.instance_name == "the house"


def test_instance_name_missing_key_is_the_default(tmp_path):
    p = tmp_path / "theme.toml"
    p.write_text('[ranks]\nwizard="warden"\n', encoding="utf-8")   # no instance_name
    assert load_theme(p).instance_name == "the house"


def test_instance_name_in_theme_toml_is_IGNORED_and_warned_about(tmp_path, caplog):
    """⛔ It MOVED to `config.toml [world]` in v1.60.0, so that configuring a
    house is one file — and there is no fallback read, deliberately. A file
    still carrying the key would otherwise sit there looking authoritative
    while the house answered to something else, so the load says so."""
    import logging
    p = tmp_path / "theme.toml"
    p.write_text('instance_name = "Mos Eisley"\n[ranks]\norder=["citizen","Steward"]\n',
                 encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        t = load_theme(p)
    assert t.instance_name == "the house"          # NOT read
    assert t.owner_rank == "Steward"               # the rest of the file still applies
    assert "instance_name has MOVED to config.toml" in caplog.text


def test_version_line_reads_the_instance_name(world, clock):
    import heresay
    house = Theme(instance_name="Mos Eisley")
    s = FakeSession(world, name="Corvid", clock=clock, theme=house)
    s.clear()
    dispatch(s, ".version")
    assert s.out == [f"Mos Eisley, Heresay version {heresay.__version__}"]


def test_version_line_default_is_unchanged(world, clock):
    import heresay
    s = FakeSession(world, name="Corvid", clock=clock)      # default theme
    s.clear()
    dispatch(s, ".version")
    assert s.out == [f"Commons, Heresay version {heresay.__version__}"]


def test_examine_arch_line_reads_the_instance_name(world, tmp_path, clock):
    house = Theme(instance_name="Mos Eisley")
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    from heresay.config import Config
    cfg = Config(owner_name="Generic", wrap_columns=1000)
    looker = FakeSession(world, name="Watcher", clock=clock, accounts=store,
                         config=cfg, theme=house)
    dispatch(looker, ".examine Generic")
    # A generic theme with only instance_name set: the owner rung reads "owner".
    assert "The owner of Mos Eisley." in looker.text


def test_examine_arch_line_default_is_unchanged(world, tmp_path, clock):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    from heresay.config import Config
    cfg = Config(owner_name="Generic", wrap_columns=1000)
    looker = FakeSession(world, name="Watcher", clock=clock, accounts=store,
                         config=cfg)
    dispatch(looker, ".examine Generic")
    assert "The owner of Commons." in looker.text


def test_map_title_reads_the_instance_name(world, clock):
    house = Theme(instance_name="Mos Eisley")
    s = FakeSession(world, name="Corvid", clock=clock, theme=house)
    dispatch(s, ".map")
    assert "*** The map of Mos Eisley *" in s.text


def test_no_hardcoded_instance_name_in_engine():
    """Every engine-generated 'Commons' routes through theme.instance_name.
    The only file allowed to contain the literal is theme.py, which holds the
    default. Authored files (greeting/help/agreement/world) are not engine code
    and are not scanned. (The engine's OWN name, 'Heresay', IS hard-coded in
    engine code by design — it is identity, not theme — so it is not scanned
    for; only the instance name is themeable.)"""
    engine = Path(__file__).resolve().parent.parent / "heresay"
    offenders = [p.name for p in sorted(engine.glob("*.py"))
                 if p.name != "theme.py" and "Commons" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"hardcoded 'Commons' still in: {offenders}"


def test_rank_helper():
    """A rank's name IS its word now; anything off the ladder reads as the floor."""
    t = DEFAULT_THEME                          # the GENERIC ladder (v1.54.0)
    assert t.rank("resident") == "resident"
    assert t.rank("staff") == "staff"
    assert t.rank("owner") == "owner"
    assert t.rank("wizard") == "resident"      # a pre-1.27 word is not a rank
    assert t.rank("") == "resident"


# ---- internal identifiers untouched --------------------------------------

def test_promote_persists_the_rank_name_and_shows_it(world, tmp_path, clock):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    store.create("Wren", "pw")
    arch = FakeSession(world, name="Generic", clock=clock, accounts=store, level="arch")
    wren = FakeSession(world, name="Wren", clock=clock, accounts=store, level="resident")
    dispatch(arch, ".promote Wren")
    # What lands on disk is the LADDER NAME (v1.27.0), not a separate internal word.
    reloaded = AccountStore(tmp_path / "accounts.json")
    assert reloaded.get("Wren").rank == "staff"
    # But the confirmations speak the theme.
    assert "Wren's rank is now staff." in arch.text
    assert "Your rank is now staff." in wren.text
    arch.clear(); wren.clear()
    dispatch(arch, ".demote Wren")
    assert AccountStore(tmp_path / "accounts.json").get("Wren").rank == "resident"
    assert "Wren's rank is now resident." in arch.text
    assert "Your rank is resident again." in wren.text


# ---- routed output strings (spot checks) ---------------------------------

def test_gag_announce_is_themed(world, clock):
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    bob = FakeSession(world, name="Bob", clock=clock)
    obs = FakeSession(world, name="Obs", clock=clock)
    dispatch(wiz, ".gag Bob")
    assert "Bob has been silenced by the staff." in obs.text
    assert "You have been gagged by the staff." in bob.text


def test_accounts_column_is_themed(world, tmp_path, clock):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    store.create("Wren", "pw")
    store.set_rank("Wren", "staff")
    # The roster shows Generic as the owner only when config names it so.
    cfg = Config(owner_name="Generic", wrap_columns=1000)
    arch = FakeSession(world, name="Generic", clock=clock, accounts=store,
                       level="arch", config=cfg)
    dispatch(arch, ".accounts")
    assert "staff" in arch.text and "owner" in arch.text
    assert "wizard" not in arch.text and "arch" not in arch.text


def test_the_help_sections_are_themed():
    """`.keeperhelp` is gone (v1.29.0); the one screen headers each section with
    the rank's own word, so a retheme moves them."""
    # Commons's ladder, so the section headers read in its rank words.
    text = help_text(COMMONS_THEME, "long", "owner")
    assert "[ staff ]" in text and "[ owner ]" in text


def test_a_custom_theme_reaches_messages(world, clock):
    # staff_plural is a PROPERTY now; a custom staff display word/plural is set
    # through level_display (v1.54.0).
    warden = Theme(order=("citizen", "warden", "Steward"), staff="warden",
                   level_display={"staff": ("warden", "wardens")})
    wiz = FakeSession(world, name="Wiz", clock=clock, level="wizard", theme=warden)
    bob = FakeSession(world, name="Bob", clock=clock, theme=warden)
    obs = FakeSession(world, name="Obs", clock=clock, theme=warden)
    dispatch(wiz, ".gag Bob")
    assert "Bob has been silenced by the wardens." in obs.text


def test_deleteme_arch_line_is_themed(world, tmp_path, clock):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    from heresay.config import Config
    cfg = Config(owner_name="Generic", wrap_columns=1000)
    arch = FakeSession(world, name="Generic", clock=clock, accounts=store,
                       level="arch", config=cfg)
    dispatch(arch, ".deleteme")
    assert "The house cannot delete its owner." in arch.text


# ---- the house favicon (v1.22.0) ------------------------------------------
#
# It lives in theme.toml rather than config.toml because it is BRANDING — it
# belongs beside the house's name, and house/ deploys with the code, so adding
# one never needs an /etc edit. Read once at startup, never fatal.

import logging

from heresay.config import Config
from heresay.server import FAVICON_MAX_BYTES, _load_favicon
from heresay.theme import load_theme

REPO_ROOT = Path(__file__).resolve().parent.parent


def _cfg(house):
    return Config(house_dir=str(house))


def test_the_favicon_key_defaults_to_none():
    assert Theme().favicon == ""


def test_theme_toml_carries_the_favicon(tmp_path):
    p = tmp_path / "theme.toml"
    p.write_text('instance_name = "Elsewhere"\nfavicon = "icons/mark.png"\n',
                 encoding="utf-8")
    t = load_theme(p)
    assert t.favicon == "icons/mark.png"


def test_an_unset_favicon_is_the_blank_icon(tmp_path):
    assert _load_favicon(_cfg(tmp_path), "") == ""


def test_a_real_file_becomes_a_data_url(tmp_path):
    (tmp_path / "mark.svg").write_text("<svg/>", encoding="utf-8")
    url = _load_favicon(_cfg(tmp_path), "mark.svg")
    assert url.startswith("data:image/svg+xml;base64,")
    assert "http" not in url            # zero external requests, by construction


def test_a_missing_file_warns_and_falls_back(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="heresay"):
        assert _load_favicon(_cfg(tmp_path), "nope.png") == ""
    assert "favicon" in caplog.text and "could not read" in caplog.text


def test_an_unsupported_type_warns_and_falls_back(tmp_path, caplog):
    (tmp_path / "notes.txt").write_text("not an icon", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="heresay"):
        assert _load_favicon(_cfg(tmp_path), "notes.txt") == ""
    assert "unsupported type" in caplog.text


def test_an_empty_file_warns_and_falls_back(tmp_path, caplog):
    (tmp_path / "mark.png").write_bytes(b"")
    with caplog.at_level(logging.WARNING, logger="heresay"):
        assert _load_favicon(_cfg(tmp_path), "mark.png") == ""
    assert "is empty" in caplog.text


def test_an_oversized_file_warns_and_falls_back(tmp_path, caplog):
    (tmp_path / "huge.png").write_bytes(b"\x00" * (FAVICON_MAX_BYTES + 1))
    with caplog.at_level(logging.WARNING, logger="heresay"):
        assert _load_favicon(_cfg(tmp_path), "huge.png") == ""
    assert "max" in caplog.text


def test_commons_ships_a_favicon_that_loads():
    """Part E, end to end: the shipped bunny is found, typed and encoded."""
    house = REPO_ROOT / "tests" / "fixtures" / "house"
    t = load_theme(house / "theme.toml")
    assert t.favicon, "Commons's theme.toml should name a favicon"
    url = _load_favicon(Config(house_dir=str(house)), t.favicon)
    assert url.startswith("data:image/svg+xml;base64,")
    assert len(url) < FAVICON_MAX_BYTES
