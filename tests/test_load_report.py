# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.load_report` (v1.36.0) — what the engine saw when it read the house.

The load already said all of this; it said it to stdout, which in production is
journald, which means a builder who mistyped a filename had to SSH to the box to
find out. These tests protect the second copy: an in-memory buffer filled at
boot, shown in-client, and gone at restart.

Two things are worth stating about the shape, because they are the design:

- The WARNINGS arrive through a scoped log handler, so the buffer is complete by
  construction — a warning added to some loader years from now is captured with
  no edit here. The tests therefore drive REAL malformed houses through a REAL
  server rather than calling `report.warn()` by hand.
- journald still gets everything. The capture is additive, and a test pins that.
"""

import logging
from pathlib import Path

import pytest

import heresay.commands as C
from heresay.commands import dispatch
from heresay.commands.registry import STAFF, command_seed
from heresay.config import Config
from heresay.loadreport import WARNING_CAP, LoadReport
from heresay.server import TalkerServer
from heresay.theme import DEFAULT_THEME
from heresay.world import load_world

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _cfg(tmp_path, world_dir=None, entry="atrium", jail="holding_cell",
         **over):
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
        world_dir=str(world_dir or WORLD),
        entry_room=entry, jail_room=jail,
        wrap_columns=78,
    )
    base.update(over)
    return Config(**base)


def _messy_world(tmp_path):
    """A house with the three authoring mistakes that are NOT fatal:

    a one-way passage (orchard -> pond, declared from one side only), a retired
    header (`entry:`, ignored since v1.17.0), and an orphan narrow file whose
    room does not exist.
    """
    w = tmp_path / "world"
    (w / "rooms").mkdir(parents=True)
    (w / "rooms" / "orchard.txt").write_text(
        "name: orchard\nexits: pond\n\nA orchard gone to seed.\n", encoding="utf-8")
    (w / "rooms" / "pond.txt").write_text(
        "name: pond\nentry: yes\n\nStill water.\n", encoding="utf-8")
    (w / "rooms" / "hall.narrow.txt").write_text(
        "A hall, narrowly.\n", encoding="utf-8")
    return w


def _server(tmp_path, **over):
    return TalkerServer(_cfg(tmp_path, **over))


def _session(srv, width=78, level="wizard", name="Wizzy", **kw):
    s = FakeSession(srv.world, name=name, level=level, width=width,
                    config=srv.config, **kw)
    s.load_report = srv.load_report
    return s


def _screen(srv, width=78, **kw):
    s = _session(srv, width=width, **kw)
    s.clear()
    dispatch(s, ".load_report")
    return s.text


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def test_it_seeds_to_staff_and_is_assignable():
    assert command_seed()["load_report"] == STAFF
    assert C._visible("load_report", DEFAULT_THEME.staff_rank, DEFAULT_THEME)
    assert C._visible("load_report", DEFAULT_THEME.owner_rank, DEFAULT_THEME)
    assert not C._visible("load_report", DEFAULT_THEME.resident_rank, DEFAULT_THEME)


def test_a_resident_cannot_see_it(tmp_path):
    srv = _server(tmp_path)
    assert "Unknown command" in _screen(srv, level="resident", name="Corvid")


def test_a_keeper_and_the_arch_can(tmp_path):
    srv = _server(tmp_path)
    assert "Unknown command" not in _screen(srv, level="wizard")
    assert "Unknown command" not in _screen(srv, level="arch", name="Generic")


# ---------------------------------------------------------------------------
# A clean load
# ---------------------------------------------------------------------------

def test_a_clean_load_shows_the_counts_and_no_warnings(tmp_path):
    srv = _server(tmp_path)
    out = _screen(srv)
    assert "Last load — " in out
    assert f"{len(srv.world.rooms)} rooms" in out
    assert f"{len(srv.world.echoes)} echoes" in out
    assert f"{len(srv.help_topics)} help topics" in out
    assert "entry atrium, jail holding_cell" in out
    assert "no warnings." in out
    assert "no one-way passages, no warnings" in out       # the footer
    assert "warnings:" not in out                          # no empty block


def test_the_shipped_house_loads_clean(tmp_path):
    """Commons itself must produce no warnings. If it ever does, this is the
    test that says so — which is the whole point of the feature."""
    srv = _server(tmp_path)
    assert srv.load_report.warnings == []
    assert srv.load_report.one_way == []


def test_the_stamp_is_the_load_time(tmp_path):
    srv = _server(tmp_path)
    import time
    expected = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(srv.load_report.loaded_at))
    assert f"Last load — {expected}" in _screen(srv)


# ---------------------------------------------------------------------------
# A messy load: one-way passage, orphan narrow file, retired header
# ---------------------------------------------------------------------------

def test_the_three_authoring_mistakes_all_appear(tmp_path):
    srv = _server(tmp_path, world_dir=_messy_world(tmp_path),
                  entry="orchard", jail="")
    out = _screen(srv)

    # 1. the one-way passage, in its own block
    assert "one-way passages:" in out
    assert "orchard -> pond" in out

    # 2. the retired header, named with its file
    assert "'entry:' is retired and ignored" in out
    assert "pond.txt" in out

    # 3. the orphan narrow variant
    assert "narrow variant with no room file" in out
    assert "hall.narrow.txt" in out

    assert "warnings:" in out
    assert "1 one-way passage, 2 warnings" in out          # singular/plural both


def test_the_counts_follow_the_house_that_was_loaded(tmp_path):
    srv = _server(tmp_path, world_dir=_messy_world(tmp_path),
                  entry="orchard", jail="")
    out = _screen(srv)
    assert "2 rooms" in out
    assert "0 echoes" in out
    assert "entry orchard, jail none" in out


def test_a_malformed_theme_is_captured(tmp_path):
    """A warning from a module that has never heard of the load report — which
    is the point of capturing by handler rather than by explicit calls."""
    bad = tmp_path / "theme.toml"
    bad.write_text('[ranks]\norder = "not a list"\n', encoding="utf-8")
    srv = _server(tmp_path, theme_file=str(bad))       # absolute: wins outright
    assert any("[ranks] order must be a list" in w
               for w in srv.load_report.warnings)
    assert "[ranks] order must be a list" in _screen(srv)


def test_an_unreadable_favicon_is_captured(tmp_path):
    bad = tmp_path / "theme.toml"
    bad.write_text('favicon = "icon.txt"\n', encoding="utf-8")
    srv = _server(tmp_path, theme_file=str(bad))
    assert any("favicon: unsupported type" in w for w in srv.load_report.warnings)


def test_a_missing_arch_account_is_captured_at_start(tmp_path):
    """`start()` is inside the capture too: "did TLS come up?" is the same kind
    of question as "how many rooms?"."""
    import asyncio

    async def go():
        srv = _server(tmp_path, plain_port=0, ws_port=0,
                      cert_file=str(tmp_path / "nope.pem"),
                      key_file=str(tmp_path / "nope.key"))
        await srv.start()
        try:
            return srv.load_report, _screen(srv)
        finally:
            await srv.aclose()

    report, out = asyncio.run(go())
    assert any("owner account" in w for w in report.warnings)
    assert any("TLS listener NOT started" in w for w in report.warnings)
    assert "doors: plaintext, web" in out       # TLS absent, and the report says so
    assert "TLS listener NOT started" in out


# ---------------------------------------------------------------------------
# ⚠ The fatal cases are still fatal — a load report is not a softening
# ---------------------------------------------------------------------------

def test_an_exit_to_an_unknown_room_still_refuses_the_boot(tmp_path):
    """There is no "malformed exit — ignored" warning, and there must not be:
    an exit naming a room that does not exist is a startup ERROR, and a house
    that opens with a broken door is worse than one that will not open. Capturing
    the load must never turn a refusal into a line on a screen nobody reads."""
    from heresay.world import WorldError
    w = tmp_path / "world"
    (w / "rooms").mkdir(parents=True)
    (w / "rooms" / "orchard.txt").write_text(
        "name: orchard\nexits: nrth\n\nA orchard.\n", encoding="utf-8")
    with pytest.raises(WorldError) as exc:
        load_world(w, "orchard", "")
    assert "unknown room" in str(exc.value)


def test_a_failed_load_leaves_no_handler_behind(tmp_path):
    """The capture is removed in a `finally`. A load that raises must not leave a
    listener on the package logger, quietly filing every later warning in the
    process into a report nobody will ever read."""
    from heresay.world import WorldError
    logger = logging.getLogger("heresay")
    before = list(logger.handlers)
    with pytest.raises(WorldError):
        _server(tmp_path, world_dir=tmp_path / "no-such-world")
    assert logger.handlers == before


# ---------------------------------------------------------------------------
# journald still gets everything — the capture is additive
# ---------------------------------------------------------------------------

def test_the_log_output_is_unchanged(tmp_path, caplog):
    """The second copy is a second listener, not a redirection: every line the
    load used to emit still reaches the ordinary logger."""
    world = _messy_world(tmp_path)
    with caplog.at_level(logging.INFO, logger="heresay"):
        srv = _server(tmp_path, world_dir=world, entry="orchard", jail="")
    text = caplog.text
    assert "room(s) loaded from" in text                # the INFO counts
    assert "help topics:" in text
    assert "one-way passages: orchard -> pond" in text   # the INFO passage line
    assert "'entry:' is retired and ignored" in text    # and both warnings
    assert "narrow variant with no room file" in text
    # ...and the in-memory copy holds the same two warnings.
    assert len(srv.load_report.warnings) == 2


# ---------------------------------------------------------------------------
# Rendering: both widths, and the buffer's own edges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("width", [40, 78])
def test_it_fits_at_both_widths(tmp_path, width):
    srv = _server(tmp_path, world_dir=_messy_world(tmp_path),
                  entry="orchard", jail="")
    out = _screen(srv, width=width)
    for line in out.split("\n"):
        assert len(line) <= width, repr(line)
    # The content survives the squeeze at both widths.
    assert "orchard -> pond" in out
    assert "hall.narrow.txt" in out


def test_a_long_path_is_broken_rather_than_overhanging(tmp_path):
    """⚠ Warnings carry filesystem paths — single unbreakable tokens of any
    length. At 40 the ordinary wrap would punch one straight through the frame,
    so this screen (and only this screen) breaks long words."""
    report = LoadReport()
    report.warn("/" + "a" * 120 + "/rooms/x.txt: something is wrong")
    s = FakeSession(load_world(WORLD, "atrium", "holding_cell"),
                    name="Wizzy", level="wizard", width=40,
                    load_report=report)
    dispatch(s, ".load_report")
    for line in s.text.split("\n"):
        assert len(line) <= 40, repr(line)


def test_the_arrows_line_up_wide_and_do_not_force_a_wrap_narrow(tmp_path):
    report = LoadReport()
    report.note_one_way([("a", "b"), ("longer_room", "c")])
    world = load_world(WORLD, "atrium", "holding_cell")

    wide = FakeSession(world, name="Wizzy", level="wizard", width=78)
    wide.load_report = report
    dispatch(wide, ".load_report")
    rows = [l for l in wide.text.split("\n") if "->" in l]
    assert len({l.index("->") for l in rows}) == 1        # one column of arrows

    narrow = FakeSession(world, name="Wizzy", level="wizard", width=40)
    narrow.load_report = report
    dispatch(narrow, ".load_report")
    assert "a -> b" in narrow.text                        # unpadded at 40


def test_the_warning_buffer_is_bounded_and_says_so():
    report = LoadReport()
    for i in range(WARNING_CAP + 5):
        report.warn(f"warning {i}")
    assert len(report.warnings) == WARNING_CAP
    assert report.dropped == 5
    world = load_world(WORLD, "atrium", "holding_cell")
    s = FakeSession(world, name="Wizzy", level="wizard", width=78)
    s.load_report = report
    dispatch(s, ".load_report")
    assert "(5 more not kept — buffer full)" in s.text


def test_a_session_with_no_report_says_so(tmp_path):
    srv = _server(tmp_path)
    s = FakeSession(srv.world, name="Wizzy", level="wizard", config=srv.config)
    s.load_report = None
    s.clear()
    dispatch(s, ".load_report")
    assert "No load report available." in s.text


# ---------------------------------------------------------------------------
# Nothing is written down
# ---------------------------------------------------------------------------

def test_the_report_is_memory_only(tmp_path):
    """No file, no event-log entry: a load report describes the process that is
    running, and a stale one from the last restart would be worse than none."""
    srv = _server(tmp_path, world_dir=_messy_world(tmp_path),
                  entry="orchard", jail="")
    assert srv.load_report.warnings                       # there IS something
    for path in tmp_path.rglob("*"):
        if path.is_file():
            raw = path.read_text(encoding="utf-8", errors="replace")
            assert "narrow variant with no room file" not in raw, path
    assert srv.events.read(types=None) == [] or all(
        "narrow" not in str(e) for e in srv.events.read())
