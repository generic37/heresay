# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Blank-line separation: every output unit is preceded by exactly one blank line.

A UNIT is one logical event — a say (however many physical lines it wraps to),
an emote, a tell, an arrival, the whole `.who` block, the whole `.help` screen,
one error line. The session does the spacing (``Session._emit_unit``); handlers
just hand over their text. Two blank lines in a row must never appear.
"""

import asyncio
from pathlib import Path

import pytest

from heresay import commands
from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.session import Session
from heresay.world import Resident, load_world

from conftest import FakeClock, RecordingTransport

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _real_session(world=None, name="Corvid", wrap=40):
    """A real Session (the layer that does the spacing) over a recording transport."""
    world = world or load_world(WORLD, "atrium", "holding_cell")
    tr = RecordingTransport()
    sess = Session(tr, world, None, Config(wrap_columns=wrap),
                   clock=FakeClock(), wall_clock=lambda: 0.0)
    sess.resident = Resident(name, "", world.start_room)
    sess.resident.session = sess
    world.add_resident(sess.resident)
    return sess, tr


def assert_no_double_blanks(stream: str):
    """The invariant, stated once: never two blank lines in a row, any door."""
    normalized = stream.replace("\r\n", "\n")
    assert "\n\n\n" not in normalized, repr(normalized)


# ---- one unit = one blank + its lines ------------------------------------

def test_wrapped_say_is_one_unit_with_one_leading_blank():
    sess, tr = _real_session(wrap=40)
    dispatch(sess, "the rain finally stopped and the whole orchard "
                   "smells of wet stone tonight")
    lines = tr.text.split("\n")
    assert lines[0] == ""                       # exactly one leading blank
    assert lines[1].startswith("Corvid says: ")
    body = [ln for ln in lines[1:] if ln]
    assert len(body) == 3, body                 # wrapped to 3 physical lines
    assert "" not in lines[1:-1]                # no blanks inside the unit
    assert_no_double_blanks(tr.text)


def test_each_consecutive_event_gets_exactly_one_blank():
    sess, tr = _real_session()
    dispatch(sess, "hello there")                # a say
    dispatch(sess, ".e stretches.")              # an emote
    sess.send("Wren arrives.")                   # an arrival announcement
    assert tr.text.split("\n") == [
        "", "Corvid says: hello there",
        "", "Corvid stretches.",
        "", "Wren arrives.",
        "",
    ]
    assert_no_double_blanks(tr.text)


def test_error_and_usage_lines_are_units_too():
    sess, tr = _real_session()
    dispatch(sess, ".frobnicate")
    dispatch(sess, ".go")
    assert tr.text.startswith("\n")
    assert tr.text.count("\n\n") == 1            # the separator before ".go"'s reply
    assert_no_double_blanks(tr.text)


# ---- block units: one blank before, interior untouched -------------------

def test_who_block_gets_one_blank_and_keeps_its_interior():
    # Wide on purpose: this asserts the WIDE wholist's header, and a session at
    # 40 columns is a narrow one (which has its own header, and its own tests).
    sess, tr = _real_session(wrap=78)
    dispatch(sess, ".who")
    lines = tr.text.rstrip("\n").split("\n")
    assert lines[0] == ""                        # one blank, then the header
    assert lines[1].startswith("*** Current users on ")
    assert lines[2] == ""                        # the block's OWN interior blank
    assert lines[-1].startswith("*** Total of ")
    assert_no_double_blanks(tr.text)


def test_help_screen_gets_one_blank_and_keeps_its_interior():
    sess, tr = _real_session(wrap=1000)
    dispatch(sess, ".help")
    lines = tr.text.rstrip("\n").split("\n")
    assert lines[0] == ""
    assert lines[1].startswith("*** Talker commands *")   # the top rule
    assert lines[2] == ""                        # help's own blank under the header
    assert lines[3].startswith("<text>")
    assert_no_double_blanks(tr.text)


def test_look_block_keeps_its_interior_blanks():
    sess, tr = _real_session(wrap=1000)
    dispatch(sess, ".look")
    lines = tr.text.rstrip("\n").split("\n")
    assert lines[0] == ""
    assert lines[1] == "[ atrium ]"
    assert lines[2] == ""                        # authored gap under the title
    assert lines[3]                              # description starts
    assert_no_double_blanks(tr.text)


# ---- the never-two-blanks invariant --------------------------------------

def test_unit_starting_with_a_blank_line_does_not_double_up():
    sess, tr = _real_session()
    sess.send("\n\nAlready spaced.")
    assert tr.text == "\nAlready spaced.\n"
    assert_no_double_blanks(tr.text)


def test_unit_ending_with_blank_lines_does_not_double_up():
    sess, tr = _real_session()
    sess.send("A notice.\n\n")
    sess.send("Another notice.")
    assert tr.text == "\nA notice.\n\nAnother notice.\n"
    assert_no_double_blanks(tr.text)


def test_all_blank_unit_emits_nothing():
    # An empty unit is no event at all — emitting it would strand a blank line.
    sess, tr = _real_session()
    sess.send("")
    sess.send_raw("")
    sess.send("\n\n\n")
    assert tr.frames == []


def test_separator_is_a_true_blank_line_under_set_time():
    # The time prefix decorates the unit's lines, never the separator itself.
    sess, tr = _real_session()
    sess.show_time = True
    sess.send("hello")
    lines = tr.text.split("\n")
    assert lines[0] == ""                        # no "HH:MM " on the blank
    assert lines[1].endswith("hello") and lines[1] != "hello"
    assert_no_double_blanks(tr.text)


def test_long_multi_event_transcript_has_no_double_blanks():
    sess, tr = _real_session(wrap=40)
    for line in ("hello", ".e waves", ".look", ".who", ".help", ".think hmm",
                 ".frobnicate", "a much longer line that will certainly need "
                 "wrapping across several physical lines to fit", ".map"):
        dispatch(sess, line)
        sess.send("Wren arrives.")
        sess.send("Wren departs.")
    assert_no_double_blanks(tr.text)
    # Every unit boundary is exactly one blank: count them.
    assert tr.text.startswith("\n")
    assert not tr.text.endswith("\n\n")


# ---- .reboot is exempt (it writes through its own sink) ------------------

def test_reboot_player_sink_bypasses_the_separator():
    """The player writes formed frames straight to the transport, so `@clear`'s
    24 blanks and back-to-back `@progress` stages play exactly as authored."""
    from heresay.server import _SessionSink

    sess, tr = _real_session()
    sink = _SessionSink(sess)
    sink.write("\n" * 24)                        # @clear
    sink.write("REINIT [    ]\n")
    sink.write("REINIT [####]\n")
    assert tr.frames == ["\n" * 24, "REINIT [    ]\n", "REINIT [####]\n"]
    # And Session.send is gated off entirely while a sequence plays.
    sess._reboot_active = True
    sess.send("something else")
    sess.send_raw("a block")
    assert len(tr.frames) == 3


# ---- cross-door: the real byte stream over a socket ----------------------

@pytest.fixture
async def server(tmp_path):
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
        wrap_columns=40,
    )
    srv = TalkerServer(cfg)
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


async def _collect(reader, timeout=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _new(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name); await _collect(r)
    await _send(w, "yes"); await _collect(r); await _send(w, pw); await _collect(r)
    await _send(w, pw); await _collect(r)                 # agreement text
    await _send(w, "y"); await _collect(r, timeout=1.5)   # + recovery hashing
    return r, w


@pytest.mark.asyncio
async def test_wrapped_say_crosses_the_door_as_one_blank_plus_three_lines(server):
    srv, port = server
    ra, wa = await _new(port, "Corvid", "corvidpw")
    rb, wb = await _new(port, "Wren")
    await _collect(ra)

    await _send(wa, "the rain finally stopped and the whole orchard "
                    "smells of wet stone tonight")
    heard = (await _collect(rb)).replace("\r\n", "\n")
    assert heard.startswith("\n")
    body = [ln for ln in heard.split("\n") if ln.strip() and not ln.startswith("> ")]
    assert len(body) == 3, body
    assert body[0].startswith("Corvid says: ")
    assert_no_double_blanks(heard)

    for w in (wa, wb):
        w.close()


@pytest.mark.asyncio
async def test_multi_event_socket_transcript_never_doubles_a_blank(server):
    srv, port = server
    ra, wa = await _new(port, "Corvid", "corvidpw")
    rb, wb = await _new(port, "Wren")
    await _collect(ra); await _collect(rb)

    transcript = ""
    for line in ("hello there", ".e waves", ".look", ".who", "another remark"):
        await _send(wa, line)
        transcript += await _collect(ra)
    # The listener's stream too: says, emotes and the arrival/departure notices.
    await _send(wa, ".go oak")
    transcript_b = await _collect(rb)

    for stream in (transcript, transcript_b):
        # The socket prompt ("> ") is written without a newline; drop it before
        # judging blank lines, or a bare prompt reads as a non-blank line.
        cleaned = stream.replace("\r\n", "\n").replace("> ", "")
        assert_no_double_blanks(cleaned)

    for w in (wa, wb):
        w.close()
