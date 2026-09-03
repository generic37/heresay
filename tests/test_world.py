# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The world as a directory: `world/map.txt` + `world/rooms/<id>.txt`.

Three things are being asserted here, in this order:

1. **The shipped world still is what it was.** `tests/fixtures/world_v1.json` was
   captured from `rooms.toml` immediately before that file was deleted — every
   description, flag, edge, `.look` exit list and the map. The migration to
   per-room files has to reproduce it byte for byte, hub expansion and all.
2. **The format's rules**, including every way a world can be refused, each with
   a message that names the file.
3. **The refusals**, including a world with no rooms at all — which is a
   startup error now, not a fallback into a built-in void.
"""

import json
from pathlib import Path

import pytest

from heresay.world import (NARROW_SUFFIX, WorldError, load_world,
                          one_way_passages, parse_room_file)

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"
FIXTURE = json.loads((REPO / "tests" / "fixtures" / "world_v1.json").read_text())

# Distinctive authored map lines (byte-for-byte).
# The maintainer dropped the "*MAP OF COMMONS*" title row: .map is framed now, and the
# frame's own "*** The map of Commons ***" rule says it once.
MAP_FIRST_ROW = "            ______"
MAP_HOLDING_ROW = "| Holding_cell|                  Ledge---Pool---Beach      | Connects   |"
MAP_LAST_ROW = "                                                           |____________|"

ALL_WORLD = {
    "atrium", "refectory", "grotto", "shore", "glade", "overlook",
    "gallery", "plaza", "copse", "spring", "orchard", "lodge",
    "stall", "rotunda", "oak", "rampart", "mirror_hall", "lawn",
    "hall", "cabin", "den", "thicket", "parlour", "backroom",
    "holding_cell", "concourse",
}

EDGES = [  # spot-check ~8 from the edge list
    ("rampart", "rotunda"), ("mirror_hall", "stall"), ("gallery", "atrium"),
    ("atrium", "plaza"), ("plaza", "refectory"), ("copse", "thicket"),
    ("oak", "parlour"), ("spring", "grotto"),
]

# The 26 authored rooms PLUS void.txt, which v1.17.0 ships as an ordinary sealed
# room (the built-in "void" fallback is gone; this one is just content).
SHIPPED_ROOMS = ALL_WORLD | {"void"}

NON_EDGES = [  # ~4 non-edges
    ("rampart", "gallery"), ("refectory", "hall"), ("overlook", "shore"),
    ("atrium", "grotto"),
]


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


# ---- the migration: identical to what ran before it ----------------------

def test_every_room_is_byte_identical_to_the_pre_migration_world(world):
    """The whole point of the fixture: prose is the thing most easily lost in a
    format change, so compare all 26 descriptions character for character."""
    assert set(FIXTURE["rooms"]) == ALL_WORLD
    assert set(world.rooms) == SHIPPED_ROOMS
    for rid, expected in FIXTURE["rooms"].items():
        room = world.rooms[rid]
        assert room.name == expected["name"], rid
        assert room.description == expected["description"], rid
        assert room.lockable == expected["lockable"], rid
        assert room.staff_only == expected["staff_only"], rid


def test_the_graph_is_identical_including_the_expanded_hub(world):
    """`hub` is gone as a concept; concourse's file names its 23 destinations and
    each of them names concourse back. Adjacency must come out the same."""
    for rid, expected in FIXTURE["rooms"].items():
        assert world.exits_of(rid) == expected["look_exits"], rid       # .look order too
        adjacent = sorted(o for o in world.rooms if world.adjacent(rid, o))
        assert adjacent == expected["adjacent_to"], rid


def test_the_map_is_byte_identical(world):
    assert world.ascii_map == FIXTURE["map"]
    assert (WORLD / "map.txt").read_text(encoding="utf-8").rstrip("\n") == FIXTURE["map"]


def test_the_entry_and_jail_are_where_they_were(world):
    assert world.start_room == FIXTURE["start_room"] == "atrium"
    assert world.jail_room == FIXTURE["jail_room"] == "holding_cell"


def test_the_shipped_world_declares_no_one_way_passages(world):
    """Every passage in Commons is stated from both ends. If a future edit
    drops one side, this is where it shows up (and the startup line names it)."""
    assert world.one_way == []


def test_all_26_rooms_load_plus_the_shipped_void(world):
    assert set(world.rooms) == SHIPPED_ROOMS
    assert ALL_WORLD < set(world.rooms)          # nothing authored was lost


def test_display_names_use_spaces(world):
    assert world.rooms["atrium"].name == "atrium"
    assert world.rooms["lodge"].name == "lodge"
    assert world.rooms["gallery"].name == "gallery"


def test_edges_present(world):
    for a, b in EDGES:
        assert world.adjacent(a, b), f"expected edge {a}-{b}"
        assert world.adjacent(b, a), f"edges are bidirectional: {b}-{a}"


def test_non_edges_absent(world):
    for a, b in NON_EDGES:
        assert not world.adjacent(a, b), f"unexpected edge {a}-{b}"
        assert not world.adjacent(b, a)


def test_the_concourse_reaches_everywhere_by_saying_so(world):
    """What used to be a flag in the loader is now 23 lines in a file — and the
    behaviour is the same in both directions."""
    for rid in ALL_WORLD - {"concourse", "backroom", "holding_cell"}:
        assert world.adjacent(rid, "concourse"), f"{rid} -> concourse"
        assert world.adjacent("concourse", rid), f"concourse -> {rid}"
    for rid in ("backroom", "holding_cell"):
        assert not world.adjacent("concourse", rid)
        assert not world.adjacent(rid, "concourse")


def test_deck_and_jail_off_the_graph(world):
    """No longer by a rule in the code: nothing lists them, and they list
    nothing, which is a fact you can read in their files."""
    for rid in ALL_WORLD - {"backroom"}:
        assert not world.adjacent("backroom", rid)
        assert not world.adjacent(rid, "backroom")
    for rid in ALL_WORLD - {"holding_cell"}:
        assert not world.adjacent("holding_cell", rid)
        assert not world.adjacent(rid, "holding_cell")
    assert world.rooms["backroom"].exits == [] == world.rooms["holding_cell"].exits


def test_flags(world):
    assert world.staff_only_rooms == {"backroom"}
    assert world.jail_room == "holding_cell"
    for rid in ("lodge", "cabin", "stall", "grotto"):
        assert world.rooms[rid].lockable, f"{rid} should be lockable"
    for rid in ("atrium", "oak", "orchard", "backroom"):
        assert not world.rooms[rid].lockable, f"{rid} should not be lockable"


def test_exits_of_is_the_rooms_own_list(world):
    ex = set(world.exits_of("atrium"))
    assert {"gallery", "oak", "plaza", "orchard", "concourse"} == ex
    assert len(world.exits_of("concourse")) == 23
    assert "backroom" not in world.exits_of("concourse")
    assert "holding_cell" not in world.exits_of("concourse")
    assert world.exits_of("holding_cell") == []


def test_resolve_prefix(world):
    rid, ambiguous = world.resolve_room("gall")
    assert rid == "gallery" and not ambiguous
    rid, ambiguous = world.resolve_room("atrium")
    assert rid == "atrium"


def test_map_distinctive_lines_exact(world):
    lines = world.ascii_map.split("\n")
    assert lines[0] == MAP_FIRST_ROW
    assert "*MAP OF COMMONS*" not in world.ascii_map   # the title row is gone
    # The maintainer removed the trailing " COMMONS" footer: the art now ends at the
    # Concourse box's closing row, with no blank line after it.
    assert lines[-1] == MAP_LAST_ROW
    assert " COMMONS" not in lines
    assert MAP_HOLDING_ROW in lines   # the Holding_cell box row, exact
    # The map shows the specials as authored flavor, even though they are off-graph.
    assert "| Backroom |" in world.ascii_map
    assert "Concourse:" in world.ascii_map


# ---- the file format -----------------------------------------------------

def write_world(tmp_path, rooms: dict, ascii_map: str = "MAP") -> Path:
    """Build a world directory from {filename: file text}."""
    root = tmp_path / "world"
    (root / "rooms").mkdir(parents=True)
    (root / "map.txt").write_text(ascii_map + "\n", encoding="utf-8")
    for name, text in rooms.items():
        (root / "rooms" / name).write_text(text, encoding="utf-8")
    return root


def load_world_hall(root, jail=""):
    """Load a fixture world whose entry room is `hall` — the config names the
    entry room now, so every test world has to say which one it is."""
    return load_world(root, "hall", jail)


HALL = "name: hall\nexits: cave\n\nA hall.\n"
CAVE = "name: cave\nexits: hall\n\nA cave.\n"


def test_a_room_file_is_header_blank_line_description(tmp_path):
    root = write_world(tmp_path, {"hall.txt": HALL, "cave.txt": CAVE})
    world = load_world(root, "hall")
    assert world.rooms["hall"].name == "hall"
    assert world.rooms["hall"].description == "A hall."
    assert world.start_room == "hall"
    assert world.jail_room is None
    assert world.ascii_map == "MAP"


def test_the_description_is_verbatim_to_the_end_of_file(tmp_path):
    """Leading spaces are data (a mirror-written room's text is made of them), blank
    lines inside the description survive, and only the FIRST blank line splits."""
    body = "   indented\n\nstill the description\n      deeper\n"
    root = write_world(tmp_path, {"hall.txt": f"name: hall\n\n{body}"})
    room = load_world(root, "hall").rooms["hall"]
    assert room.description == body.rstrip("\n")


def test_repeated_exits_lines_merge(tmp_path):
    root = write_world(tmp_path, {
        "hall.txt": "name: hall\nexits: cave, pit\nexits: cave\nexits: shed\n"
                    "\nA hall.\n",
        "cave.txt": "name: cave\n\nA cave.\n",
        "pit.txt": "name: pit\n\nA pit.\n",
        "shed.txt": "name: shed\n\nA shed.\n",
    })
    # Order preserved, duplicates collapsed — the list is what .look prints.
    assert load_world(root, "hall").exits_of("hall") == ["cave", "pit", "shed"]


def test_flags_are_absent_when_false(tmp_path):
    root = write_world(tmp_path, {
        "hall.txt": "name: hall\nlockable: yes\n\nA hall.\n",
        "cave.txt": "name: cave\nstaff_only: yes\n\nA cave.\n",
    })
    world = load_world(root, "hall")
    assert world.rooms["hall"].lockable and not world.rooms["hall"].staff_only
    assert world.rooms["cave"].staff_only and not world.rooms["cave"].lockable


# ---- refusal: (the per-room denial line) ----------------------------------

def test_refusal_is_read_onto_a_staff_only_room(tmp_path):
    root = write_world(tmp_path, {
        "hall.txt": HALL,
        "cave.txt": "name: cave\nexits: hall\nstaff_only: yes\n"
                    "refusal: the cards won't deal you in.\n\nA cave.\n",
    })
    assert load_world(root, "hall").rooms["cave"].refusal == "the cards won't deal you in."


def test_a_staff_only_room_without_refusal_has_an_empty_one(tmp_path):
    """Empty -> the command layer supplies the neutral built-in."""
    root = write_world(tmp_path, {
        "hall.txt": HALL,
        "cave.txt": "name: cave\nexits: hall\nstaff_only: yes\n\nA cave.\n",
    })
    assert load_world(root, "hall").rooms["cave"].refusal == ""


def test_refusal_on_a_non_wizard_only_room_is_fatal(tmp_path):
    """No inert keys: a refusal line on a room nobody can be refused from is a
    mistake, not a silent no-op."""
    msg = _raises(tmp_path, {
        "hall.txt": "name: hall\nrefusal: go away.\n\nA hall.\n",
    }, "'refusal:' is only for a staff_only room")
    assert "hall.txt" in msg


def test_the_shipped_deck_declares_its_refusal(world):
    assert world.rooms["backroom"].refusal == "The door does not know you."


def test_the_shipped_room_files_are_readable_by_the_parser():
    """Every shipped file parses on its own, with the id coming from the stem.

    `*.narrow.txt` is excluded for the same reason `load_world` excludes it: a
    narrow variant is a room's 40-column DESCRIPTION, prose with no header block,
    and is content for a room rather than a room of its own.
    """
    for path in sorted((WORLD / "rooms").glob("*.txt")):
        if path.name.endswith(NARROW_SUFFIX):
            continue
        room = parse_room_file(path)
        assert room.id == path.stem
        assert room.name


# ---- one-way passages and sealed rooms ------------------------------------

ONE_WAY = {
    "hall.txt": "name: hall\nexits: cave\n\nA hall.\n",
    "cave.txt": "name: cave\n\nA cave.\n",              # no way back out
}


def test_a_one_way_passage_is_legal_and_listed(tmp_path):
    world = load_world_hall(write_world(tmp_path, ONE_WAY))
    assert world.one_way == [("hall", "cave")]
    assert one_way_passages(world.rooms) == [("hall", "cave")]


def test_one_way_means_one_way_in_the_graph(tmp_path):
    world = load_world_hall(write_world(tmp_path, ONE_WAY))
    assert world.adjacent("hall", "cave")
    assert not world.adjacent("cave", "hall")          # the sealed side
    assert world.exits_of("cave") == []


def test_the_startup_line_names_every_one_way_passage(tmp_path):
    world = load_world_hall(write_world(tmp_path, {
        "hall.txt": "name: hall\nexits: cave, shed\n\nA hall.\n",
        "cave.txt": "name: cave\nexits: hall\n\nA cave.\n",     # two-way: not listed
        "shed.txt": "name: shed\nexits: cave\n\nA shed.\n",     # two one-ways
    }))
    assert world.one_way == [("hall", "shed"), ("shed", "cave")]


# ---- validation: every refusal names the file -----------------------------

def _raises(tmp_path, rooms, needle):
    with pytest.raises(WorldError) as exc:
        load_world_hall(write_world(tmp_path, rooms))
    assert needle in str(exc.value), str(exc.value)
    return str(exc.value)


def test_unknown_exit_target_is_fatal_and_names_the_file(tmp_path):
    msg = _raises(tmp_path, {"hall.txt": "name: hall\nexits: nowhere\n\nA hall.\n"},
                  "unknown room 'nowhere'")
    assert "hall.txt" in msg


def test_a_missing_name_is_fatal(tmp_path):
    _raises(tmp_path, {"hall.txt": "\nA hall.\n"}, "no 'name:' in the header")


def test_a_bad_flag_value_is_fatal(tmp_path):
    msg = _raises(tmp_path, {"hall.txt": "name: hall\nlockable: maybe\n\nA hall.\n"},
                  "lockable must be yes or no")
    assert "'maybe'" in msg


def test_an_unknown_header_key_is_fatal(tmp_path):
    """A typo in a key would otherwise seal a room in silence — `exit:` for
    `exits:` is one keystroke away."""
    _raises(tmp_path, {"hall.txt": "name: hall\nexit: cave\n\nA hall.\n"},
            "unknown header key 'exit'")


def test_a_header_line_without_a_colon_is_fatal(tmp_path):
    _raises(tmp_path, {"hall.txt": "name: hall\nentry yes\n\nA hall.\n"},
            "line 2 of the header is not 'key: value'")


# ---- entry_room / jail_room: named in config, validated here -------------

def test_an_unset_entry_room_is_fatal(tmp_path):
    with pytest.raises(WorldError) as exc:
        load_world(write_world(tmp_path, {"hall.txt": HALL, "cave.txt": CAVE}), "")
    assert "entry_room is not set" in str(exc.value)


def test_an_entry_room_naming_a_missing_room_is_fatal_and_lists_the_rooms(tmp_path):
    with pytest.raises(WorldError) as exc:
        load_world(write_world(tmp_path, {"hall.txt": HALL, "cave.txt": CAVE}),
                   "parlour")
    msg = str(exc.value)
    assert "entry_room 'parlour' is not a room" in msg
    assert "cave" in msg and "hall" in msg          # says what IS there


def test_a_jail_room_naming_a_missing_room_is_fatal(tmp_path):
    with pytest.raises(WorldError) as exc:
        load_world(write_world(tmp_path, {"hall.txt": HALL, "cave.txt": CAVE}),
                   "hall", "dungeon")
    assert "jail_room 'dungeon' is not a room" in str(exc.value)


def test_a_named_jail_room_is_used(tmp_path):
    world = load_world(write_world(tmp_path, {"hall.txt": HALL, "cave.txt": CAVE}),
                       "hall", "cave")
    assert world.jail_room == "cave"


def test_a_retired_entry_header_is_ignored_with_a_warning(tmp_path, caplog):
    """Retired, not fatal: an operator's world directory is content, and a deploy
    that refuses to start over a line in a room file is a rename that closes the
    house. The warning is what stops the ignoring from being silent."""
    root = write_world(tmp_path, {
        "hall.txt": "name: hall\nexits: cave\nentry: yes\n\nA hall.\n",
        "cave.txt": "name: cave\nexits: hall\njail: yes\n\nA cave.\n",
    })
    with caplog.at_level("WARNING"):
        world = load_world(root, "cave")        # config wins over the header
    assert world.start_room == "cave"           # NOT hall, which claimed entry
    assert world.jail_room is None              # NOT cave, which claimed jail
    text = caplog.text
    assert "retired and ignored" in text
    assert "hall.txt" in text and "cave.txt" in text


def test_a_duplicate_room_id_is_fatal(tmp_path):
    """Two files can only collide by capitalization — one room wearing two
    names. Refuse rather than pick."""
    msg = _raises(tmp_path, {"hall.txt": HALL, "Hall.txt": "name: other\n\nX.\n"},
                  "duplicate room id")
    assert "Hall.txt" in msg


def test_no_jail_is_perfectly_legal(tmp_path):
    world = load_world_hall(write_world(tmp_path, {"hall.txt": HALL, "cave.txt": CAVE}))
    assert world.jail_room is None


# ---- a world needs rooms (v1.17.0: no built-in void) ----------------------
#
# An empty world used to boot into a hardcoded "void" room, on the theory that a
# server standing in an empty room beats a server that will not start. That was
# the wrong trade: it turned a config mistake into a house that LOOKED open and
# was not, and it cost a special-cased room id in the engine. Now it is an error,
# said plainly, once.

def test_an_empty_rooms_directory_is_fatal(tmp_path):
    with pytest.raises(WorldError) as exc:
        load_world_hall(write_world(tmp_path, {}))
    msg = str(exc.value)
    assert "no room files" in msg
    assert "entry_room" in msg               # and it says how to fix it


def test_a_missing_world_directory_is_fatal_too(tmp_path):
    with pytest.raises(WorldError) as exc:
        load_world(tmp_path / "not-here", "hall")
    assert "no room files" in str(exc.value)


def test_one_room_is_a_perfectly_good_world(tmp_path):
    world = load_world_hall(write_world(tmp_path, {"hall.txt": HALL.replace("exits: cave\n", "")}))
    assert set(world.rooms) == {"hall"}
    assert world.start_room == "hall"
    assert world.jail_room is None


def test_the_shipped_void_room_is_an_ordinary_room(world):
    """`void.txt` ships in Commons as a plain, sealed room — no special
    handling anywhere in the engine, which is the point of deleting the built-in
    one. It is unreachable by walking and reachable by staff travel."""
    void = world.rooms["void"]
    assert void.name == "void"
    assert void.description == "You are in a shapeless void."
    assert void.exits == []                                   # sealed
    assert not any("void" in r.exits for r in world.rooms.values())
    assert not void.staff_only and not void.lockable


# ---- mirror_hall: the mirror of gallery --------------------------
#
# Carroll's conceit, made literal: the room on the other side of the glass
# shows the same room reflected. Each line is the gallery line reversed
# character by character, asymmetric punctuation swapped, right-aligned to the
# block width. The leading spaces ARE the mirror — anything that strips,
# re-wraps, or left-aligns them destroys the room, which is exactly what a
# format change is most likely to do. Hence the byte-level assertions.

# The final line, verbatim: 61 leading spaces then the reflected "you enter?".
TG_LAST_LINE = "                                                             ?retne uoy"
TG_FIRST_LINE = "   gnizalb erif a yb ylizoc demraw ,moor gniward nairotciV a ni era uoY"


def test_mirror_hall_survived_the_migration_byte_for_byte(world):
    assert world.room("mirror_hall").description == \
        FIXTURE["rooms"]["mirror_hall"]["description"]


def test_mirror_hall_keeps_its_right_alignment(world):
    lines = world.room("mirror_hall").description.split("\n")
    assert lines[0] == TG_FIRST_LINE
    assert lines[-1] == TG_LAST_LINE
    # The indentation is data, not formatting: these exact leading-space counts
    # are what make the block a mirror image.
    assert [len(l) - len(l.lstrip()) for l in lines] == [3, 4, 1, 0, 3, 2, 3, 61]
    assert all(len(l) == 71 for l in lines)          # right-aligned to one width


def test_mirror_hall_is_a_faithful_mirror_of_gallery(world):
    """Each line reversed, asymmetric punctuation swapped, right-aligned to 71."""
    swap = str.maketrans("()[]{}<>/\\", ")(][}{><\\/")
    lg = world.room("gallery").description.split("\n")
    tg = world.room("mirror_hall").description.split("\n")
    assert len(lg) == len(tg) == 8
    for a, b in zip(lg, tg):
        assert b == a.strip().translate(swap)[::-1].rjust(71)


def test_the_wrap_path_does_not_eat_the_indentation(world):
    """send() runs every logical line through textwrap. textwrap keeps leading
    whitespace at the start of a paragraph, and these lines are all <= 78 so no
    wrap triggers — but assert it rather than trust it, because a change to the
    wrap width or to drop_whitespace would silently flatten the mirror."""
    import textwrap
    for ln in world.room("mirror_hall").description.split("\n"):
        assert textwrap.wrap(ln, width=78, break_long_words=False,
                             break_on_hyphens=False) == [ln]


# ---------------------------------------------------------------------
# Removed from the generic suite: each of these pins the authored ART of
# one particular house — the exact bytes of a map, a mirror-written room,
# a file of atmospheric lines. Another house's art is not a translation
# of that one, so there is nothing to rewrite them into. Every test of
# the MECHANISM around them is kept. (tools/build-generic.py)
del test_map_distinctive_lines_exact
del test_the_shipped_deck_declares_its_refusal
del test_the_shipped_void_room_is_an_ordinary_room
del test_mirror_hall_keeps_its_right_alignment
del test_mirror_hall_is_a_faithful_mirror_of_gallery
del test_the_wrap_path_does_not_eat_the_indentation
