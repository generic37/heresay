# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

""".rev — the entry room's fading public memory. In memory ONLY, never on disk.

The bound is the feature: every rendered say/emote/echo from the last window
(default 30 min), capped at the newest N (default 100), the atrium and
nowhere else, gone on restart, and never written down. An idle room's memory
genuinely empties over time.
"""

from pathlib import Path

import pytest

from heresay import commands
from heresay.commands import NOTHING_YET, REV_ELSEWHERE, REV_HEADER, dispatch
from heresay.world import load_world

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _rev(session):
    session.clear()
    dispatch(session, ".rev")
    return session.text


# ---- what is and isn't buffered -----------------------------------------

def test_says_and_emotes_are_buffered(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, "the rain finally stopped")
    dispatch(a, ".e stretches.")
    out = _rev(b)
    assert REV_HEADER in out
    assert "Corvid says: the rain finally stopped" in out
    assert "Corvid stretches." in out


def test_thinks_shouts_tells_and_announcements_are_not_buffered(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, ".think a private thought")
    dispatch(a, ".shout carried everywhere")
    dispatch(a, ".tell Wren a private word")
    dispatch(a, ".topic a topic line")
    dispatch(b, ".go oak")          # a departure announcement
    lines = world.review
    assert list(lines) == []
    assert NOTHING_YET in _rev(a)


def test_order_is_oldest_first(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    for n in ("one", "two", "three"):
        clock.t += 5
        dispatch(a, n)
    body = _rev(a).split("\n")
    said = [ln for ln in body if ln.startswith("Corvid says:")]
    assert said == ["Corvid says: one", "Corvid says: two", "Corvid says: three"]


def test_the_cap_keeps_only_the_newest_lines(world, clock):
    """Within the window, the buffer is capped at the newest `review_max`."""
    world.review_max = 100
    for i in range(120):
        clock.t += 1                          # all inside the 30-min window
        dispatch(a := FakeSession(world, name=f"U{i}", clock=clock), f"line {i}")
    assert len(world.review) == 100
    out = _rev(FakeSession(world, name="Reader", clock=clock))
    assert "line 0" not in out and "line 19" not in out   # the oldest 20 rolled off
    assert "line 20" in out and "line 119" in out


# ---- fading with time ----------------------------------------------------

def test_a_line_inside_the_window_shows(world, clock):
    world.review_window = 1800.0                # 30 minutes
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "still fresh")
    clock.t += 1799                             # just inside the window
    assert "Corvid says: still fresh" in _rev(FakeSession(world, name="Wren", clock=clock))


def test_a_line_past_the_window_has_faded(world, clock):
    world.review_window = 1800.0
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "long ago")
    clock.t += 1801                            # just past the window
    assert _rev(FakeSession(world, name="Wren", clock=clock)) == NOTHING_YET


def test_the_room_empties_over_time(world, clock):
    """The new behavior: an idle room's memory genuinely fades to nothing."""
    world.review_window = 600.0                # 10 minutes
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "one")
    clock.t += 300
    dispatch(a, "two")                         # 5 minutes later
    # At 11 minutes: the first has faded, the second is still inside its window.
    clock.t += 360                             # now t = 660; "one"@0 gone, "two"@300 kept
    out = _rev(a)
    assert "Corvid says: one" not in out
    assert "Corvid says: two" in out
    # At 21 minutes both are gone.
    clock.t += 600
    assert _rev(a) == NOTHING_YET


def test_only_the_faded_lines_go_the_survivors_stay_in_order(world, clock):
    world.review_window = 100.0
    a = FakeSession(world, name="Corvid", clock=clock)
    for n in ("one", "two", "three", "four"):
        clock.t += 40
        dispatch(a, n)                         # at t = 40, 80, 120, 160
    # now = 160, window 100 -> cutoff 60: "one"@40 gone, the rest kept in order.
    body = _rev(a).split("\n")
    said = [ln for ln in body if ln.startswith("Corvid says:")]
    assert said == ["Corvid says: two", "Corvid says: three", "Corvid says: four"]


# ---- where it can be read -----------------------------------------------

def test_rev_outside_the_atrium_is_refused(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "something in the hole")
    b = FakeSession(world, name="Wren", room="oak", clock=clock)
    assert _rev(b) == REV_ELSEWHERE
    assert "something in the hole" not in b.text


def test_speech_elsewhere_is_never_buffered(world, clock):
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    dispatch(a, "said under the oak")
    b = FakeSession(world, name="Wren", clock=clock)
    assert NOTHING_YET in _rev(b)


def test_empty_buffer_is_quiet(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    assert _rev(a) == NOTHING_YET


# ---- mute filtering happens at VIEW time --------------------------------

def test_muted_speakers_are_omitted_for_that_viewer_only(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    c = FakeSession(world, name="Bob", clock=clock)
    dispatch(a, "from Corvid")
    dispatch(b, "from Wren")
    dispatch(c, ".mute Corvid")
    seen_by_c = _rev(c)
    assert "from Corvid" not in seen_by_c
    assert "from Wren" in seen_by_c
    # One buffer, two viewers: the unmuted one still sees everything.
    seen_by_b = _rev(b)
    assert "from Corvid" in seen_by_b and "from Wren" in seen_by_b


def test_muting_after_the_fact_hides_older_lines_too(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, "said before the mute")
    dispatch(b, ".mute Corvid")
    assert "said before the mute" not in _rev(b)


# ---- .ignore filtering happens at VIEW time too (v1.29.4) ----------------
#
# The bug: `.ignore atmospherics` suppressed the room's murmurs at DELIVERY, but
# the buffer remembered what delivery had dropped, so they turned up again in
# `.rev`. Playing back a line someone asked not to be shown is the same failure
# whether it happens now or half an hour later — so `.rev` replays the room as
# THIS viewer would have experienced it live.

def _murmur(world, clock, line="the teacups rattle."):
    """Put one atmospheric in the buffer, the way the room really does."""
    world.echoes = [line]
    world.echo_min = world.echo_max = 0.0
    # Any earlier speech will have DRAWN an interval from the old bounds, so
    # clear it and let the seeding tick below redraw against the new ones.
    world._echo_interval = None
    world.maybe_echo(clock.t)              # establishes the quiet clock
    clock.t += 1
    assert world.maybe_echo(clock.t) == line
    return line


def test_atmospherics_are_replayed_by_default(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    line = _murmur(world, clock)
    assert line in _rev(a)


def test_ignoring_atmospherics_omits_them_from_rev(world, clock):
    """The fix. Live suppression and replay suppression are one decision."""
    a = FakeSession(world, name="Corvid", clock=clock)
    line = _murmur(world, clock)
    dispatch(a, ".ignore atmospherics yes")
    out = _rev(a)
    assert line not in out


def test_the_ignore_is_per_viewer_and_the_buffer_stays_whole(world, clock):
    """One buffer, two viewers, two readings — exactly like mutes."""
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, "an ordinary remark")
    line = _murmur(world, clock)
    dispatch(a, ".ignore atmospherics yes")

    seen_by_a = _rev(a)
    assert line not in seen_by_a
    assert "an ordinary remark" in seen_by_a      # speech is untouched by it
    assert line in _rev(b)                         # ...and b still sees the murmur
    assert len(world.review) == 2                  # the buffer kept both


def test_ignoring_after_the_fact_hides_older_murmurs_too(world, clock):
    """Applied at read time, not at record time — the same rule as mute."""
    a = FakeSession(world, name="Corvid", clock=clock)
    line = _murmur(world, clock)
    dispatch(a, ".ignore atmospherics yes")
    assert line not in _rev(a)
    dispatch(a, ".ignore atmospherics no")
    assert line in _rev(a)                         # ...and turning it back on returns it


def test_no_other_ignore_category_touches_rev(world, clock):
    """The audit, pinned. Only say/emote/echo are ever buffered, so the presence
    and movement categories have nothing in `.rev` to hide — setting them must
    not quietly eat room speech."""
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    dispatch(a, "an ordinary remark")
    dispatch(a, ".e stretches.")
    for cat in ("login_msg", "logout_msg", "enter_msg", "exit_msg",
                "afk_msg", "shout", "tab_alert"):
        dispatch(b, f".ignore {cat} yes")
    out = _rev(b)
    assert "Corvid says: an ordinary remark" in out
    assert "Corvid stretches." in out


# ---- lifetime ------------------------------------------------------------

def test_survives_the_room_emptying(world, clock):
    """The point of it: an arrival can see the room was alive a minute ago."""
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "still warm")
    world.remove_resident(a.resident)
    world.room_emptied("atrium")
    assert world.occupants("atrium") == []
    newcomer = FakeSession(world, name="Wren", clock=clock)
    assert "Corvid says: still warm" in _rev(newcomer)


def test_gone_after_a_restart(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    dispatch(a, "said before the restart")
    fresh = load_world(WORLD, "atrium", "holding_cell")   # a new process
    newcomer = FakeSession(fresh, name="Wren", clock=clock)
    assert _rev(newcomer) == NOTHING_YET


# ---- never on disk -------------------------------------------------------

@pytest.mark.asyncio
async def test_room_lines_never_reach_any_state_file(tmp_path):
    """Canary: say something distinctive over a real server, then grep every
    byte of every file it wrote. Room speech is still never written down."""
    import asyncio
    from heresay.config import Config
    from heresay.server import TalkerServer

    canary = "zzqqxx-rev-canary-42"
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
    )
    srv = TalkerServer(cfg)
    await srv.start()
    port = srv._servers[0].sockets[0].getsockname()[1]

    async def collect(r, t=0.4):
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(r.read(4096), t)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="replace")

    async def send(w, text):
        w.write((text + "\n").encode())
        await w.drain()

    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await collect(r)
        for line in ("Corvid", "yes", "swordfish-x9", "swordfish-x9", "y"):
            await send(w, line)
            await collect(r, 1.5)
        await send(w, canary)                 # a say, in the atrium
        await send(w, f".e {canary}")         # and an emote
        await collect(r)
        await send(w, ".rev")
        assert canary in await collect(r)     # it IS in the buffer
        w.close()
    finally:
        await srv.aclose()

    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert written                            # sanity: the server wrote something
    for p in written:
        assert canary.encode() not in p.read_bytes(), p.name


# ---- replay renders exactly as live delivery did -------------------------
#
# The buffers hold LOGICAL lines; the wrap belongs to the screen. Replaying
# through the raw path sent them out unwrapped and the client broke them at the
# window edge, so a replayed conversation was a different SHAPE than it had
# been. These pin the fix: identical physical lines, same wrap points.

from heresay.config import Config                      # noqa: E402
from heresay.session import Session                    # noqa: E402
from heresay.world import Resident                     # noqa: E402
from conftest import FakeClock, RecordingTransport    # noqa: E402

LONG_SAY = ("the rain finally stopped and the whole orchard smells of wet stone "
            "tonight, which is the sort of thing you only notice standing still")


def _real(world, name, wrap=78):
    """A real Session over a recording transport — the layer that does wrapping."""
    tr = RecordingTransport()
    s = Session(tr, world, None, Config(wrap_columns=wrap),
                clock=FakeClock(), wall_clock=lambda: 0.0)
    s.resident = Resident(name, "", world.start_room)
    s.resident.session = s
    world.add_resident(s.resident)
    return s, tr


def _lines(tr):
    return tr.text.strip("\n").split("\n")


def test_replayed_say_is_identical_to_what_the_room_saw_live(world):
    speaker, _ = _real(world, "Corvid")
    mate, mtr = _real(world, "Wren")

    dispatch(speaker, LONG_SAY)
    live = _lines(mtr)
    assert len(live) > 1, "fixture must be long enough to wrap"
    assert all(len(ln) <= 78 for ln in live)

    mtr.frames.clear()
    dispatch(mate, ".rev")
    replay = _lines(mtr)
    assert replay[0] == REV_HEADER
    assert replay[1] == ""                      # the header's own blank
    assert replay[2:] == live                   # same text, same wrap points


def test_replayed_emote_is_identical_too(world):
    speaker, _ = _real(world, "Corvid")
    mate, mtr = _real(world, "Wren")
    dispatch(speaker, ".e " + LONG_SAY)
    live = _lines(mtr)
    assert len(live) > 1
    mtr.frames.clear()
    dispatch(mate, ".rev")
    assert _lines(mtr)[2:] == live


def test_no_replayed_line_exceeds_the_wrap_width(world):
    speaker, _ = _real(world, "Corvid")
    for i in range(3):
        dispatch(speaker, f"{i} " + LONG_SAY)
    mate, mtr = _real(world, "Wren")
    dispatch(mate, ".rev")
    for ln in _lines(mtr):
        assert len(ln) <= 78, ln


def test_short_lines_are_not_reflowed(world):
    speaker, _ = _real(world, "Corvid")
    dispatch(speaker, "short one")
    dispatch(speaker, "short two")
    mate, mtr = _real(world, "Wren")
    dispatch(mate, ".rev")
    assert _lines(mtr) == [REV_HEADER, "", "Corvid says: short one",
                           "Corvid says: short two"]


def test_the_whole_block_is_one_unit(world):
    """One blank before the block, and none between the physical lines of a
    single wrapped message — a wrapped sentence is still one thing said."""
    speaker, _ = _real(world, "Corvid")
    dispatch(speaker, LONG_SAY)
    mate, mtr = _real(world, "Wren")
    dispatch(mate, ".rev")
    assert len(mtr.frames) == 1                 # one unit
    raw = mtr.text
    assert raw.startswith("\n")                 # exactly one leading blank
    assert "\n\n\n" not in raw
    body = raw.strip("\n").split("\n")[2:]      # past header + its blank
    assert "" not in body                       # no blanks inside the message
