# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where authored content is looked for, and what the server does with it.

v1.17.0 replaced the "anchor" scheme — every content path defaulting to a bare
name beside `agreement.txt` — with ONE root, `house_dir`. A deployment keeps the
code and its authored content in /opt/heresay and the config in /etc/heresay, so
resolving content against the CONFIG's directory is the trap: it is what once
left production with no help topics for a week. These tests pin the root, the
per-file overrides, and the fact that getting it wrong is now loud.
"""

import logging
from pathlib import Path

import pytest

from heresay.commands import NO_JAIL, dispatch
from heresay.config import Config, load_config
from heresay.server import TalkerServer
from heresay.world import WorldError, load_world

from conftest import FakeSession

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"

HALL = "name: hall\nexits: cave\n\nA hall.\n"
CAVE = "name: cave\nexits: hall\n\nA cave.\n"


def _prod_shaped(tmp_path, rooms=None, **over):
    """Config in an /etc-shaped dir, authored content in an /opt-shaped one."""
    etc, opt = tmp_path / "etc", tmp_path / "opt"
    (opt / "world" / "rooms").mkdir(parents=True)
    etc.mkdir()
    (opt / "world" / "map.txt").write_text("THE MAP\n", encoding="utf-8")
    for name, text in (rooms or {"hall.txt": HALL, "cave.txt": CAVE}).items():
        (opt / "world" / "rooms" / name).write_text(text, encoding="utf-8")
    (opt / "agreement.txt").write_text("the rules\n", encoding="utf-8")
    cfg = Config(base_dir=etc, house_dir=str(opt), entry_room="hall", **over)
    return cfg, opt


# ---- house_dir: one root, everything under it ----------------------------

def test_every_content_path_hangs_off_house_dir(tmp_path):
    cfg, opt = _prod_shaped(tmp_path)
    assert cfg.house_path() == opt
    assert cfg.world_path() == opt / "world"
    assert cfg.help_path() == opt / "help"
    assert cfg.agreement_path() == opt / "agreement.txt"
    assert cfg.greeting_path() == opt / "greeting.txt"
    assert cfg.reboot_path() == opt / "reboot.txt"
    assert cfg.theme_path() == opt / "theme.toml"


def test_a_config_that_sets_only_house_dir_finds_the_world(tmp_path):
    cfg, opt = _prod_shaped(tmp_path)
    world = load_world(cfg.world_path(), cfg.entry_room)
    assert set(world.rooms) == {"hall", "cave"}


def test_the_config_relative_resolution_is_the_trap_it_would_have_been(tmp_path):
    """Stated as a test so nobody re-introduces it. The old failure was SILENT —
    resolving against the config's directory found nothing, and nothing meant a
    built-in void. There is no void now, so the same mistake is a startup error
    that names the directory it looked in."""
    cfg, opt = _prod_shaped(tmp_path)
    wrong = cfg.resolve("house") / "world"          # what /etc-relative would give
    with pytest.raises(WorldError) as exc:
        load_world(wrong, "hall")
    assert "no room files" in str(exc.value)
    assert set(load_world(cfg.world_path(), "hall").rooms) == {"hall", "cave"}


@pytest.mark.parametrize("key,attr,rel", [
    ("agreement_file", "agreement_path", "covenant.txt"),
    ("greeting_file", "greeting_path", "banner.txt"),
    ("reboot_file", "reboot_path", "boot/script.txt"),
    ("theme_file", "theme_path", "words.toml"),
    ("help_dir", "help_path", "pages"),
    ("world_dir", "world_path", "worlds/summer"),
])
def test_a_per_file_override_is_resolved_under_house_dir(tmp_path, key, attr, rel):
    """The per-file keys stay, for renaming and nesting — joined under the root
    rather than replacing it."""
    cfg, opt = _prod_shaped(tmp_path)
    setattr(cfg, key, rel)
    assert getattr(cfg, attr)() == opt / rel


def test_an_absolute_override_still_wins_outright(tmp_path):
    """What lets a config written before house_dir existed — one carrying an
    absolute path per content file — keep working with nothing changed but the
    addition of house_dir itself."""
    cfg, opt = _prod_shaped(tmp_path)
    elsewhere = tmp_path / "somewhere-else"
    (elsewhere / "rooms").mkdir(parents=True)
    (elsewhere / "rooms" / "only.txt").write_text(
        "name: only\n\nJust this.\n", encoding="utf-8")
    cfg.world_dir = str(elsewhere)
    assert cfg.world_path() == elsewhere
    assert set(load_world(cfg.world_path(), "only").rooms) == {"only"}


def test_house_dir_defaults_relative_to_the_config(tmp_path):
    """The default is `house` beside the config — right for a dev checkout, and
    exactly why an /etc-based deployment must set it explicitly."""
    cfg = Config(base_dir=tmp_path, house_dir="house")
    assert cfg.house_dir == "house"
    assert cfg.house_path() == tmp_path / "house"


# ---- obsolete keys --------------------------------------------------------

def test_obsolete_config_keys_are_ignored_not_fatal(tmp_path):
    """An operator's /etc/heresay/config.toml still names rooms_file,
    defaults_file and (pre-1.1) start_room. Those keys are gone from the code;
    reading a config that has them must be a no-op, never a crash.

    start_room in particular is NOT quietly honoured as entry_room: it has been
    ignored for sixteen minor versions, and reviving a stale key as authoritative
    would be worse than failing loudly on a missing entry_room."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        "[storage]\n"
        'rooms_file = "rooms.toml"\n'
        'defaults_file = "defaults.txt"\n'
        'world_dir = "/somewhere/world"\n'
        "[world]\n"
        'start_room = "atrium"\n'
        'entry_room = "hall"\n'
        "wrap_columns = 78\n", encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.world_dir == "/somewhere/world"
    assert cfg.wrap_columns == 78
    assert cfg.entry_room == "hall"                 # the key that IS read
    assert not hasattr(cfg, "rooms_file")
    assert not hasattr(cfg, "defaults_file")
    assert not hasattr(cfg, "start_room")


def test_entry_and_jail_room_are_read_from_the_world_section(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[world]\nentry_room = "hall"\njail_room = "pit"\n',
                        encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.entry_room == "hall"
    assert cfg.jail_room == "pit"


def test_they_default_to_empty(tmp_path):
    cfg = load_config(tmp_path / "no-such-config.toml")
    assert cfg.entry_room == "" and cfg.jail_room == ""


def test_the_cert_section_is_read(tmp_path):
    """[tls] became [cert] — the section holds a certificate and a key, and
    nothing about the TLS door itself (that is [listeners])."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[cert]\ncert_file = "/x/full.pem"\nkey_file = "/x/priv.pem"\n',
                        encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.cert_file == "/x/full.pem"
    assert cfg.key_file == "/x/priv.pem"


def test_the_old_tls_section_is_no_longer_read(tmp_path):
    """A clean rename, with no silent fallback: a config has to be edited for
    entry_room anyway, so honouring [tls] would only let a half-finished edit
    look finished."""
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('[tls]\ncert_file = "/x/full.pem"\n', encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.cert_file == Config.cert_file          # the built-in default


def test_the_world_is_not_excluded_from_the_deploy_rsync():
    """world/ is authored content, like help/ — it must ship. (And rsync's
    --delete is what removes the retired rooms.toml from /opt/heresay.)"""
    script = (REPO / "deploy" / "deploy.sh").read_text()
    excludes = [ln for ln in script.split("\n") if "--exclude" in ln]
    assert excludes                                   # sanity: we found the block
    assert not any("world" in ln for ln in excludes)
    assert "--delete" in script


# ---- what the server says at startup --------------------------------------

def _server_cfg(tmp_path, world_dir, **over):
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
        world_dir=str(world_dir),
        entry_room="atrium",
        jail_room="holding_cell",
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


def test_startup_logs_the_world_as_facts(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="heresay"):
        srv = TalkerServer(_server_cfg(tmp_path, WORLD))
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("world:"))
    assert line == (f"world: 27 room(s) loaded from {WORLD}; "
                    f"entry atrium; jail holding_cell")
    # Room ids and counts — never a word of room text.
    assert "worn look" not in caplog.text
    assert not any(r.getMessage().startswith("one-way passages:") for r in caplog.records)


def test_startup_lists_one_way_passages_exactly(tmp_path, caplog):
    root = tmp_path / "w"
    (root / "rooms").mkdir(parents=True)
    (root / "map.txt").write_text("m\n", encoding="utf-8")
    (root / "rooms" / "atrium.txt").write_text(
        "name: atrium\nexits: orchard\n\nDown.\n", encoding="utf-8")
    (root / "rooms" / "orchard.txt").write_text("name: orchard\n\nRoses.\n", encoding="utf-8")
    (root / "rooms" / "cellar.txt").write_text(
        "name: cellar\nexits: stall\n\nDark.\n", encoding="utf-8")
    (root / "rooms" / "stall.txt").write_text("name: stall\n\nWool.\n", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="heresay"):
        TalkerServer(_server_cfg(tmp_path, root, jail_room=""))
    line = next(r.getMessage() for r in caplog.records
                if r.getMessage().startswith("one-way passages:"))
    assert line == "one-way passages: atrium -> orchard, cellar -> stall"


# ---- a world that cannot be built refuses to start ------------------------

def test_a_world_with_no_rooms_refuses_to_start(tmp_path):
    root = tmp_path / "w"
    (root / "rooms").mkdir(parents=True)
    with pytest.raises(WorldError) as exc:
        TalkerServer(_server_cfg(tmp_path, root))
    assert "no room files" in str(exc.value)


def test_an_unset_entry_room_refuses_to_start(tmp_path):
    with pytest.raises(WorldError) as exc:
        TalkerServer(_server_cfg(tmp_path, WORLD, entry_room=""))
    assert "entry_room is not set" in str(exc.value)


def test_an_entry_room_that_does_not_exist_refuses_to_start(tmp_path):
    with pytest.raises(WorldError) as exc:
        TalkerServer(_server_cfg(tmp_path, WORLD, entry_room="nowhere"))
    assert "entry_room 'nowhere' is not a room" in str(exc.value)


def test_a_jail_room_that_does_not_exist_refuses_to_start(tmp_path):
    with pytest.raises(WorldError) as exc:
        TalkerServer(_server_cfg(tmp_path, WORLD, jail_room="dungeon"))
    assert "jail_room 'dungeon' is not a room" in str(exc.value)


def test_the_entry_room_config_key_drives_the_entry_room(tmp_path):
    """Not the room file: point the same house/ at a different front door and it
    is a different house."""
    srv = TalkerServer(_server_cfg(tmp_path, WORLD, entry_room="oak"))
    assert srv.world.start_room == "oak"


# ---- a world with no jail -------------------------------------------------

@pytest.fixture
def jailless(tmp_path):
    root = tmp_path / "w"
    (root / "rooms").mkdir(parents=True)
    (root / "map.txt").write_text("m\n", encoding="utf-8")
    (root / "rooms" / "hall.txt").write_text(HALL, encoding="utf-8")
    (root / "rooms" / "cave.txt").write_text(CAVE, encoding="utf-8")
    return load_world(root, "hall")


def test_jail_and_free_refuse_gracefully_with_no_jail(jailless, clock):
    staff = FakeSession(jailless, name="Warden", clock=clock, level="wizard")
    resident = FakeSession(jailless, name="Corvid", clock=clock)
    for probe in (".jail Corvid", ".free Corvid"):
        staff.clear()
        dispatch(staff, probe)
        assert staff.out == [NO_JAIL], probe
    assert resident.resident.room_id == "hall"     # nobody was moved anywhere
    assert resident.out == []                      # and nobody was told anything


def test_a_one_way_passage_walks_one_way(tmp_path, clock):
    """The graph is what the files say, and `.go` is the graph. Walking into a
    sealed room works if something still reaches it; walking back out does not,
    and the refusal is the ordinary one — no special case, no explanation."""
    root = tmp_path / "w"
    (root / "rooms").mkdir(parents=True)
    (root / "map.txt").write_text("m\n", encoding="utf-8")
    (root / "rooms" / "hall.txt").write_text(HALL, encoding="utf-8")
    (root / "rooms" / "cave.txt").write_text("name: cave\n\nA cave.\n", encoding="utf-8")
    world = load_world(root, "hall")
    s = FakeSession(world, name="Corvid", clock=clock)

    dispatch(s, ".go cave")
    assert s.resident.room_id == "cave"              # the stated direction
    s.clear()
    dispatch(s, ".go hall")
    assert s.resident.room_id == "cave"              # sealed behind you
    assert s.out[-1] == "There's no way there from here."
    s.clear()
    dispatch(s, ".look")
    assert "Exits:" not in s.text                    # nothing to list


def test_the_shipped_void_is_reachable_only_by_staff_travel(clock):
    """void.txt is an ordinary sealed room: nobody walks in, a staff can `.go`
    there because staff travel ignores the graph."""
    world = load_world(WORLD, "atrium", "holding_cell")
    resident = FakeSession(world, name="Corvid", clock=clock)
    staff = FakeSession(world, name="Wizzy", clock=clock, level="wizard")
    dispatch(resident, ".go void")
    assert resident.resident.room_id == "atrium"          # refused
    dispatch(staff, ".go void")
    assert staff.resident.room_id == "void"
    assert "Nothing here yet." in staff.text
