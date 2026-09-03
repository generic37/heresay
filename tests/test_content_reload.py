# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Content on the reload spine (v1.39.0, Run 2) — the last step of the
live-configurable house.

⛔ **The rule that shapes all of it: never swap the `World`; graft into it.** A
`World` holds the authored rooms AND the live house — residents, locks, topics,
the entry room's fading memory. Replacing it to change a paragraph would empty
every room. So every test here that touches the world asserts the object is the
same one afterwards, and that the people in it are where they were.

⚠ **This is where the spine's `guard` step earns its keep.** Run 1's sources
needed none: a permissions value is valid or it is not. A room's change depends
on who is standing in it — prose applies under an occupant's feet, structure
waits for them to leave.
"""

import json
import shutil
from pathlib import Path

import pytest

from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def house(tmp_path):
    """A private copy of the shipped house/, safe to edit."""
    dst = tmp_path / "house"
    shutil.copytree(REPO / "tests" / "fixtures" / "house", dst)
    return dst


def _cfg(tmp_path, house, **over):
    base = dict(
        owner_name="Generic",
        house_dir=str(house),
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
        instance_name="Commons",
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000, reload_poll_seconds=0,
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def srv(tmp_path, house):
    return TalkerServer(_cfg(tmp_path, house))


def _tick(srv):
    return {r.source: r for r in srv.reloader.check_once()}


def _resident(srv, name="Corvid", room=None, **kw):
    s = FakeSession(srv.world, name=name, room=room, state=srv.state,
                    config=srv.config, help_topics=srv.help_topics,
                    theme=srv.theme, **kw)
    return s


def _room_file(house, rid):
    return house / "world" / "rooms" / f"{rid}.txt"


def _set_header(house, rid, key, value):
    """Rewrite one header line of a room file."""
    p = _room_file(house, rid)
    lines = p.read_text(encoding="utf-8").split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(f"{key}:"):
            lines[i] = f"{key}: {value}"
            break
    else:
        lines.insert(1, f"{key}: {value}")
    p.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# ⛔ The graft — the World object is never replaced
# ---------------------------------------------------------------------------

def test_a_prose_edit_grafts_without_touching_the_live_house(srv, house):
    """The heart of it: everyone's location, the locks, the topics and the room's
    fading memory all survive a content change, because the object holding them
    is never replaced."""
    world = srv.world
    res = _resident(srv)
    srv.world.lock("cabin")
    srv.world.topics["atrium"] = "mind the step"
    srv.world.record_review("atrium", "Corvid", "Corvid says: hello", 0.0)

    p = _room_file(house, "atrium")
    p.write_text(p.read_text().replace("name: atrium", "name: the atrium"),
                 encoding="utf-8")
    r = _tick(srv)["world"]

    assert r.ok and r.changes
    assert srv.world is world                        # ⛔ never swapped
    assert srv.world.rooms["atrium"].name == "the atrium"
    assert res.resident.room_id == "atrium"     # nobody moved
    assert "cabin" in srv.world.locked_rooms       # locks kept
    assert srv.world.topics["atrium"] == "mind the step"
    assert srv.world.review                          # the room still remembers


def test_a_new_room_appears_and_a_removed_one_goes(srv, house):
    (house / "world" / "rooms" / "annexe.txt").write_text(
        "name: annexe\n\nA room that was not there before.\n", encoding="utf-8")
    r = _tick(srv)["world"]
    assert r.ok and any("annexe" in c for c in r.changes)
    assert "annexe" in srv.world.rooms

    (house / "world" / "rooms" / "annexe.txt").unlink()
    r = _tick(srv)["world"]
    assert r.ok and any("removed" in c and "annexe" in c for c in r.changes)
    assert "annexe" not in srv.world.rooms


def test_the_map_and_the_echoes_reload(srv, house):
    (house / "world" / "map.txt").write_text("a different map\n", encoding="utf-8")
    (house / "world" / "echoes.txt").write_text("A door creaks.\n", encoding="utf-8")
    r = _tick(srv)["world"]
    assert r.ok
    assert srv.world.ascii_map == "a different map"
    assert srv.world.echoes == ["A door creaks."]
    assert any("map redrawn" in c for c in r.changes)
    assert any("echoes" in c for c in r.changes)


# ---------------------------------------------------------------------------
# validate — a bad file never closes the house
# ---------------------------------------------------------------------------

def test_a_bad_room_file_is_rejected_and_the_world_stands(srv, house):
    before = dict(srv.world.rooms)
    _room_file(house, "cabin").write_text(
        "name: cabin\nexits: nowhere_at_all\n\nA cabin.\n", encoding="utf-8")
    r = _tick(srv)["world"]
    assert not r.ok and "unknown room" in " ".join(r.errors)
    assert srv.world.rooms == before                 # untouched
    # ...and the house is still usable.
    res = _resident(srv)
    dispatch(res, ".look")
    assert "atrium" in res.text.lower()


def test_a_room_file_with_no_name_is_rejected(srv, house):
    _room_file(house, "cabin").write_text("exits: stall\n\nA cabin.\n",
                                            encoding="utf-8")
    r = _tick(srv)["world"]
    assert not r.ok
    assert srv.world.rooms["cabin"].name == "cabin"


def test_removing_the_entry_room_is_rejected(srv, house):
    """guard: the house has to keep its front door."""
    # Point everything that led to the atrium elsewhere, then delete it, so
    # the failure is the GUARD's and not just a dangling exit.
    for p in (house / "world" / "rooms").glob("*.txt"):
        t = p.read_text(encoding="utf-8")
        if "atrium" in t:
            p.write_text(t.replace("atrium,", "").replace(", atrium", "")
                          .replace("exits: atrium\n", "exits:\n"),
                         encoding="utf-8")
    _room_file(house, "atrium").unlink()
    r = _tick(srv)["world"]
    assert not r.ok
    assert "entry_room" in " ".join(r.errors) or "atrium" in " ".join(r.errors)
    assert "atrium" in srv.world.rooms


# ---------------------------------------------------------------------------
# guard — the occupied-room rule
# ---------------------------------------------------------------------------

def test_a_structural_change_to_an_occupied_room_waits(srv, house):
    """⚠ The guard step, doing the thing Run 1 never needed. Removing an exit
    from under somebody standing there is exactly the change that must wait."""
    _resident(srv, room="cabin")
    before = list(srv.world.rooms["cabin"].exits)
    _set_header(house, "cabin", "exits", "")

    r = _tick(srv)["world"]
    assert r.ok                                       # not a failure — a wait
    assert r.deferred
    assert srv.world.rooms["cabin"].exits == before  # kept
    assert "cabin" in " ".join(r.notes) and "occupied by 1" in " ".join(r.notes)


def test_the_deferred_change_lands_once_the_room_empties(srv, house):
    res = _resident(srv, room="cabin")
    _set_header(house, "cabin", "exits", "")
    assert _tick(srv)["world"].deferred

    # A tick with nothing changed on disk: still waiting, and reported quietly.
    srv.reloader.check_once()
    assert srv.world.rooms["cabin"].exits

    srv.world.remove_resident(res.resident)           # they leave
    srv.world.room_emptied("cabin")
    results = srv.reloader.check_once()               # the retry
    assert srv.world.rooms["cabin"].exits == []
    assert not srv.reloader._deferred


def test_prose_applies_to_an_occupied_room_but_structure_does_not(srv, house):
    """Both halves in one edit, which is the case that actually happens: an
    author fixes a typo and removes a door in the same sitting."""
    _resident(srv, room="cabin")
    p = _room_file(house, "cabin")
    body = p.read_text(encoding="utf-8")
    p.write_text(body.replace("name: cabin", "name: the cabin"), encoding="utf-8")
    assert _tick(srv)["world"].ok
    assert srv.world.rooms["cabin"].name == "the cabin"   # prose landed

    _set_header(house, "cabin", "staff_only", "yes")
    r = _tick(srv)["world"]
    assert r.deferred
    assert srv.world.rooms["cabin"].staff_only is False     # structure waited


def test_a_deferral_that_would_break_an_exit_defers_the_whole_pass(srv, house):
    """⚠ A kept room's exits were validated against the OLD set. Keeping the
    cabin is no good if the room its door leads to has just been deleted — a
    partly-applied world is the one outcome worse than a late one."""
    _resident(srv, room="cabin")
    _set_header(house, "cabin", "staff_only", "yes")     # makes it structural
    dest = srv.world.rooms["cabin"].exits[0]
    for p in (house / "world" / "rooms").glob("*.txt"):
        t = p.read_text(encoding="utf-8")
        if dest in t:
            p.write_text(t.replace(f"{dest},", "").replace(f", {dest}", "")
                          .replace(f"exits: {dest}\n", "exits:\n"), encoding="utf-8")
    _room_file(house, dest).unlink()

    r = _tick(srv)["world"]
    assert not r.ok
    assert "pointing nowhere" in " ".join(r.errors)
    assert dest in srv.world.rooms                          # nothing applied


# ---------------------------------------------------------------------------
# The authored files around the world
# ---------------------------------------------------------------------------

def test_a_help_page_reaches_an_already_connected_session(srv, house):
    """⚠ Mutated in place, not replaced: every open session holds THIS dict, and
    the person who most wants a corrected page is the one already asking."""
    res = _resident(srv)
    dispatch(res, ".help go")
    assert "leave" in res.text.lower() or "go" in res.text.lower()

    (house / "help" / "go.txt").write_text(
        "go — a corrected page\n\nWalk to an adjoining room.\n", encoding="utf-8")
    r = _tick(srv)["help"]
    assert r.ok and any("go" in c for c in r.changes)

    res.clear()
    dispatch(res, ".help go")
    assert "a corrected page" in res.text                 # the SAME session


def test_a_new_help_page_appears_and_a_deleted_one_goes(srv, house):
    (house / "help" / "custom.txt").write_text(
        "custom — a house page\n\nSomething local.\n", encoding="utf-8")
    r = _tick(srv)["help"]
    assert "custom" in srv.help_topics and any("added" in c for c in r.changes)
    (house / "help" / "custom.txt").unlink()
    r = _tick(srv)["help"]
    assert "custom" not in srv.help_topics
    assert any("removed" in c for c in r.changes)


def test_the_greeting_reloads(srv, house):
    (house / "greeting.txt").write_text("A NEW DOOR\n", encoding="utf-8")
    r = _tick(srv)["greeting"]
    assert r.ok and r.changes
    assert "A NEW DOOR" in srv.greeting


def test_the_reboot_script_reloads(srv, house):
    (house / "reboot.txt").write_text("A different machine.\n@pause 0.1\n",
                                      encoding="utf-8")
    r = _tick(srv)["reboot"]
    assert r.ok and r.changes
    assert any("A different machine." in str(step) for step in srv.reboot_steps)


# ---------------------------------------------------------------------------
# theme.toml — words live, ranks refused
# ---------------------------------------------------------------------------

def _theme(house, body):
    (house / "theme.toml").write_text(body, encoding="utf-8")


def test_the_house_words_reload_and_reach_connected_sessions(srv, house):
    """The WORDS — this file's job since v1.60.0 took the NAME out of it."""
    res = _resident(srv)
    assert res.theme.staff_word == "staff"
    _theme(house, '[ranks]\norder = ["resident", "staff", "owner"]\n'
                  'staff = "staff"\n[levels.staff]\nsingular = "warden"\n'
                  'plural = "wardens"\n')
    r = _tick(srv)["theme"]
    assert r.ok and r.changes
    assert srv.theme.staff_word == "warden"
    assert res.theme.staff_word == "warden"              # handed to the session
    # ...and the browser was told, since the tab's identity is the client's to
    # hold and it cannot discover a change.
    assert any(f.startswith("\x01title") for f in res.controls)


def test_a_theme_file_that_still_NAMES_the_house_cannot_rename_it(srv, house):
    """⛔ v1.60.0 moved `instance_name` to `config.toml` with NO fallback read.
    A `theme.toml` still carrying the key is the likeliest leftover of that
    move, and it must not be able to rename the house behind the config's back
    — nor to blank it to the engine's colourless default, which is what taking
    the fresh file's value would have done on every unrelated theme edit."""
    res = _resident(srv)
    assert res.theme.instance_name == "Commons"
    _theme(house, 'instance_name = "Elsewhere"\n[ranks]\n'
                  'order = ["resident", "staff", "owner"]\nstaff = "staff"\n'
                  '[levels.staff]\nsingular = "warden"\nplural = "wardens"\n')
    r = _tick(srv)["theme"]
    assert r.ok
    assert srv.theme.instance_name == "Commons"       # unmoved
    assert res.theme.instance_name == "Commons"
    assert not any("Elsewhere" in c for c in r.changes)
    assert srv.theme.staff_word == "warden"              # the rest still applied


def test_a_ranks_change_is_REFUSED_live(srv, house):
    """⛔ Rank changes are reboot-only. The ladder is what every stored rank is
    read against, and changing it re-places every account (v1.38.0) — that
    cannot happen under a live house."""
    res = _resident(srv)
    _theme(house, '[ranks]\n'
                  'order = ["novice", "resident", "staff", "owner"]\n'
                  'resident = "resident"\nstaff = "staff"\n'
                  '[levels.staff]\nsingular = "warden"\nplural = "wardens"\n')
    r = _tick(srv)["theme"]

    assert r.ok                                          # not a rejection
    assert srv.theme.order == ("resident", "staff", "owner")   # ladder KEPT
    assert srv.theme.newb_rank == "resident"
    assert "RESTART" in " ".join(r.notes)
    # ...and the rest of the file applied anyway.
    assert srv.theme.staff_word == "warden"


def test_a_broken_theme_is_rejected_and_the_words_stand(srv, house):
    # `floor`, not `newb`: newb is derived since v1.41.0 and cannot be wrong.
    _theme(house, '[ranks]\norder = ["a", "b"]\nresident = "nope"\n')
    r = _tick(srv)["theme"]
    assert not r.ok
    assert srv.theme.staff_word == "staff"              # the live words stand


def test_the_favicon_reloads_and_is_pushed(srv, house):
    res = _resident(srv)
    (house / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                                    encoding="utf-8")
    _theme(house, 'favicon = "icon.svg"\n[ranks]\n'
                  'order = ["resident", "staff", "owner"]\nstaff = "staff"\n')
    r = _tick(srv)["theme"]
    assert r.ok and any("favicon" in c for c in r.changes)
    assert srv.favicon_url.startswith("data:image/svg+xml;base64,")
    assert any(f.startswith("\x01favicon") and "base64" in f for f in res.controls)


# ---------------------------------------------------------------------------
# agreement.txt — live at the door, next login for everyone else
# ---------------------------------------------------------------------------

def test_an_agreement_edit_is_REBOOT_GATED(srv, house):
    """⛔ A DELIBERATE REVERSAL (v1.40.0). v1.39.0 applied this live, which meant
    that from that instant new logins consented to the NEW version while everyone
    already in-world stayed on the OLD one — a mixed-agreement state that drained
    only as each person happened to relog. **You cannot have different people
    under different agreements.**"""
    res = _resident(srv)
    was, was_text = srv.agreement_hash, srv.agreement_text
    p = house / "agreement.txt"
    p.write_text(p.read_text(encoding="utf-8") + "\n9. And be kind.\n",
                 encoding="utf-8")

    r = _tick(srv)["agreement"]
    assert r.ok                                  # seen and reported, not failed
    assert srv.agreement_hash == was             # ⛔ UNMOVED
    assert srv.agreement_text == was_text
    assert "RESTART TO APPLY" in " ".join(r.notes)
    # The person in the room was not asked anything, and did not move.
    assert res.out == [] or all("agree" not in line.lower() for line in res.out)
    assert res.resident.room_id == "atrium"


def test_a_reboot_applies_the_edited_agreement(srv, house, tmp_path):
    """The other half: the next boot re-resolves and re-hashes, and because the
    restart drops everyone, they all reconnect onto the same version."""
    was = srv.agreement_hash
    p = house / "agreement.txt"
    p.write_text(p.read_text(encoding="utf-8") + "\n9. And be kind.\n",
                 encoding="utf-8")
    _tick(srv)                                   # live: refused
    assert srv.agreement_hash == was

    rebooted = TalkerServer(_cfg(tmp_path, house))
    assert rebooted.agreement_hash != was
    assert "And be kind" in rebooted.agreement_text


def test_nothing_recomputes_the_agreement_at_runtime():
    """⛔ Structural: `refresh_agreement` is GONE, and the whole reload package
    is swept for any other writer. The hash is set once, in `_build`, and never
    again — that absence is the guarantee, not a rule somebody must remember."""
    import ast
    import inspect

    import heresay.reload as RL
    import heresay.server as S

    assert not hasattr(S.TalkerServer, "refresh_agreement")
    assert "refresh_agreement" not in inspect.getsource(RL)

    # The only assignments to agreement_text/agreement_hash in the server are
    # inside _build (the boot).
    tree = ast.parse(inspect.getsource(S))
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        for node in ast.walk(fn):
            for tgt in (node.targets if isinstance(node, ast.Assign) else []):
                if isinstance(tgt, ast.Attribute) and tgt.attr in (
                        "agreement_text", "agreement_hash"):
                    assert fn.name == "_build", fn.name


def test_the_shipped_agreement_is_unchanged_by_this_build():
    """⛔ No agreement change from v1.39.0 itself — the file is byte-identical,
    so nobody re-consents on deploy."""
    import hashlib
    raw = (REPO / "tests" / "fixtures" / "house" / "agreement.txt").read_bytes()
    assert b"{rev_window_minutes}" in raw          # still a token, never written
    assert hashlib.sha256(raw).hexdigest()         # (the pin is the git diff)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _report(srv, width=78):
    s = FakeSession(srv.world, name="Wizzy", level="wizard", width=width,
                    config=srv.config, load_report=srv.load_report)
    s.clear()
    dispatch(s, ".load_report")
    return s.text


def test_load_report_shows_a_content_reload(srv, house):
    (house / "greeting.txt").write_text("A NEW DOOR\n", encoding="utf-8")
    _tick(srv)
    out = _report(srv)
    assert "reloaded" in out and "greeting" in out


def test_load_report_shows_a_deferred_room(srv, house):
    _resident(srv, room="cabin")
    _set_header(house, "cabin", "exits", "")
    _tick(srv)
    out = _report(srv)
    assert "cabin" in out and "occupied" in out


def test_a_content_reload_is_recorded_as_a_fact(srv, house):
    (house / "greeting.txt").write_text("A NEW DOOR\n", encoding="utf-8")
    _tick(srv)
    acts = srv.events.read(types=("staff_action",))
    assert any(e["target"] == "greeting" and e["action"] == "reload"
               for e in acts)


@pytest.mark.parametrize("width", [40, 78])
def test_the_content_report_fits_both_widths(srv, house, width):
    _resident(srv, room="cabin")
    _set_header(house, "cabin", "exits", "")
    (house / "greeting.txt").write_text("A NEW DOOR\n", encoding="utf-8")
    _tick(srv)
    for line in _report(srv, width=width).split("\n"):
        assert len(line) <= width, repr(line)


# ---------------------------------------------------------------------------
# The watcher itself
# ---------------------------------------------------------------------------

def test_nothing_changed_means_nothing_runs(srv):
    assert srv.reloader.check_once() == []


def test_every_content_source_is_watched(srv):
    names = {s.name for s in srv.reloader.sources}
    assert {"world", "greeting", "help", "reboot", "theme", "agreement"} <= names
    watched = " ".join(str(p) for s in srv.reloader.sources
                       for p in s.paths(srv))
    for f in ("map.txt", "echoes.txt", "greeting.txt", "reboot.txt",
              "theme.toml", "agreement.txt"):
        assert f in watched, f
    assert "state.json" not in watched          # still true: the daemon writes it


def test_the_content_push_script_does_not_restart():
    """`make deploy-content` exists so a content change needs no restart; a
    restart in it would defeat the entire run."""
    script = (REPO / "deploy" / "deploy-content.sh").read_text(encoding="utf-8")
    assert "systemctl restart" not in script
    assert "rsync" in script and "--delete" in script
    assert "house/" in script
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert "deploy-content:" in makefile
