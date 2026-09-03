# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.reboot` — the personal 486 boot theater (v2: fake confirmation + progress).

Three layers:
- the parser and player are pure (fake sink + injected sleep): directives,
  verbatim order, wrapping, the pause/@progress budget clamp, the socket BEL
  fallbacks and the staged (tt++-safe) socket progress bar, and the web frames;
- the command layer (FakeSession): the rate bucket (checked BEFORE the fake
  prompt) and the "Nothing happens." no-op — the emote now fires server-side,
  after the confirmation answer, so it is asserted there;
- the live server (real socket + WebSocket doors): the fake "Reboot server?
  (y/n)" prompt whose answer is ignored (y/n/empty/garbage all run), sound +
  progress frames reaching ONLY the invoker, the emote firing after the answer,
  speech during a boot, input swallowing, and the banner + look tail.

WebAudio synthesis and the animated web bar are browser-side — README checklist.
"""

import asyncio
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as ws_connect

from heresay import commands, reboot
from heresay.commands import REBOOT, REBOOT_REFUSED, REBOOT_PROMPT, dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession, FakeClock

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


# =========================================================================
# Parser
# =========================================================================

def test_parse_pause_is_float():
    assert reboot.parse("@pause 1.2") == [("pause", 1.2)]
    assert reboot.parse("@pause 3") == [("pause", 3.0)]


def test_parse_directives_and_verbatim_in_order():
    script = "@clear\n@sound power\nhello world\n@beep\n@pause 0.5\n@sound hdd"
    assert reboot.parse(script) == [
        ("clear",),
        ("sound", "power"),
        ("line", "hello world"),
        ("beep",),
        ("pause", 0.5),
        ("sound", "hdd"),
    ]


def test_parse_progress_directive():
    assert reboot.parse("@progress 12 REINITIALIZING WORLD STATE") == [
        ("progress", 12.0, "REINITIALIZING WORLD STATE")
    ]
    assert reboot.parse("@progress 3.5 GO") == [("progress", 3.5, "GO")]


def test_parse_progress_malformed_is_verbatim():
    assert reboot.parse("@progress") == [("line", "@progress")]
    assert reboot.parse("@progress 5") == [("line", "@progress 5")]        # no label
    assert reboot.parse("@progress fast LABEL") == [("line", "@progress fast LABEL")]


def test_parse_verbatim_preserves_indentation_and_inner_spacing():
    line = "  Primary Master   : QUANTUM LP240A 540MB"
    assert reboot.parse(line) == [("line", line)]


def test_parse_blank_line_is_a_blank_line():
    assert reboot.parse("a\n\nb") == [("line", "a"), ("line", ""), ("line", "b")]


def test_parse_wraps_only_overlong_lines():
    long = " ".join(["alpha"] * 30)          # ~180 cols of real words
    steps = reboot.parse(long, width=78)
    assert all(s[0] == "line" for s in steps)
    assert len(steps) > 1 and all(len(s[1]) <= 78 for s in steps)


def test_parse_unknown_directive_is_shown_verbatim():
    assert reboot.parse("@wobble the drive") == [("line", "@wobble the drive")]
    assert reboot.parse("@sound") == [("line", "@sound")]
    assert reboot.parse("@pause fast") == [("line", "@pause fast")]


def test_load_script_missing_returns_none(tmp_path):
    assert reboot.load_script(tmp_path / "nope.txt") is None


def test_load_script_reads_text(tmp_path):
    p = tmp_path / "reboot.txt"
    p.write_text("@beep\nhi", encoding="utf-8")
    assert reboot.load_script(p) == "@beep\nhi"


def test_shipped_default_script_parses_with_progress():
    steps = reboot.parse((WORLD.parent / "reboot.txt").read_text(encoding="utf-8"))
    kinds = {s[0] for s in steps}
    assert {"clear", "sound", "beep", "pause", "line", "progress"} <= kinds


def test_shipped_default_stays_under_two_and_a_half_minutes():
    steps = reboot.parse((WORLD.parent / "reboot.txt").read_text(encoding="utf-8"))
    total = sum(s[1] for s in steps if s[0] in ("pause", "progress"))
    assert total < 150.0


# =========================================================================
# Player — a fake sink records raw frames; sleep is injected
# =========================================================================

class FakeSink:
    def __init__(self):
        self.writes = []

    def write(self, text):
        self.writes.append(text)

    async def flush(self):
        pass


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, secs):
        self.calls.append(secs)


async def _play(steps, *, sound_frames, budget=reboot.PAUSE_BUDGET_SECONDS):
    sink, sleep = FakeSink(), FakeSleep()
    await reboot.play(steps, sink, sleep=sleep, sound_frames=sound_frames,
                      pause_budget=budget)
    return sink, sleep


async def test_play_line_gets_a_trailing_newline():
    sink, _ = await _play([("line", "CPU: 80486DX2")], sound_frames=True)
    assert sink.writes == ["CPU: 80486DX2\n"]


async def test_play_clear_pushes_24_blank_lines():
    sink, _ = await _play([("clear",)], sound_frames=True)
    assert sink.writes == ["\n" * reboot.CLEAR_LINES]


async def test_play_beep_is_bel_on_every_door():
    web, _ = await _play([("beep",)], sound_frames=True)
    sock, _ = await _play([("beep",)], sound_frames=False)
    assert web.writes == [reboot.BEL] and sock.writes == [reboot.BEL]


async def test_play_web_sound_is_control_frame():
    sink, _ = await _play([("sound", "power")], sound_frames=True)
    assert sink.writes == ["\x01sound power"]
    assert all(not w.startswith("\x07") for w in sink.writes)


async def test_play_socket_sound_bel_patterns():
    sink, sleep = await _play([("sound", "power")], sound_frames=False)
    assert sink.writes == [reboot.BEL] and sleep.calls == []
    sink, _ = await _play([("sound", "fan")], sound_frames=False)
    assert sink.writes == []
    sink, sleep = await _play([("sound", "hdd")], sound_frames=False)
    assert sink.writes == [reboot.BEL] * 3
    assert sleep.calls == [0.3, 0.3]
    sink, sleep = await _play([("sound", "floppy")], sound_frames=False)
    assert sink.writes == [reboot.BEL] * 2 and sleep.calls == [0.5]


async def test_play_socket_never_emits_control_frames():
    steps = [("sound", "power"), ("sound", "hdd"), ("beep",),
             ("progress", 1.0, "X")]
    sink, _ = await _play(steps, sound_frames=False)
    assert all(not w.startswith("\x01") for w in sink.writes)


async def test_play_pause_budget_is_clamped():
    steps = [("pause", 10.0)] * 4
    _, sleep = await _play(steps, sound_frames=True, budget=30.0)
    assert sum(sleep.calls) == pytest.approx(30.0)
    assert sleep.calls == [10.0, 10.0, 10.0, 0.0]


async def test_play_pause_passes_small_values_through():
    _, sleep = await _play([("pause", 1.2), ("pause", 0.8)], sound_frames=True)
    assert sleep.calls == [1.2, 0.8]


# ---- @progress ----------------------------------------------------------

async def test_play_progress_socket_stages_full_lines():
    dur = 2.0
    sink, sleep = await _play([("progress", dur, "REINIT")], sound_frames=False)
    # Every write is a complete, newline-terminated line — never a same-line
    # fragment (tt++ split mode re-renders unterminated lines and would mangle it).
    assert all(w.endswith("\n") for w in sink.writes)
    assert len(sink.writes) == reboot.PROGRESS_STAGES
    W, N = reboot.PROGRESS_CELLS, reboot.PROGRESS_STAGES

    def expected(i):
        f = min(round(i * W / (N - 1)), W)
        if f >= W:
            return f"REINIT [{'#' * W}] done\n"
        return f"REINIT [{'#' * f}{' ' * (W - f)}]\n"

    assert sink.writes == [expected(i) for i in range(N)]
    # Evenly spread across the duration: N-1 gaps summing to the full duration.
    assert len(sleep.calls) == reboot.PROGRESS_STAGES - 1
    assert sum(sleep.calls) == pytest.approx(dur)
    assert all(d == pytest.approx(dur / (N - 1)) for d in sleep.calls)


async def test_play_progress_socket_matches_documented_sample():
    # The exact rendering from the task spec (defaults: 5 stages, 20 cells).
    assert (reboot.PROGRESS_STAGES, reboot.PROGRESS_CELLS) == (5, 20)
    sink, _ = await _play([("progress", 4.0, "REINITIALIZING WORLD STATE")],
                          sound_frames=False)
    assert sink.writes == [
        "REINITIALIZING WORLD STATE [                    ]\n",
        "REINITIALIZING WORLD STATE [#####               ]\n",
        "REINITIALIZING WORLD STATE [##########          ]\n",
        "REINITIALIZING WORLD STATE [###############     ]\n",
        "REINITIALIZING WORLD STATE [####################] done\n",
    ]


async def test_play_progress_socket_no_unterminated_writes():
    sink, _ = await _play([("progress", 1.0, "REINITIALIZING WORLD STATE")],
                          sound_frames=False)
    assert sink.writes
    assert all(w.endswith("\n") for w in sink.writes)      # never a bare fragment
    assert all(not w.startswith("\x01") for w in sink.writes)  # no control frames


async def test_play_progress_web_is_one_control_frame():
    sink, sleep = await _play(
        [("progress", 12.0, "REINITIALIZING WORLD STATE")], sound_frames=True
    )
    assert sink.writes == ["\x01progress 12 REINITIALIZING WORLD STATE"]
    assert sleep.calls == [12.0]        # server waits in step with the animation


async def test_play_progress_counts_against_budget():
    # 8s progress + a 5s pause in a 10s budget: pause is clamped to the last 2s.
    steps = [("progress", 8.0, "X"), ("pause", 5.0)]
    _, sleep = await _play(steps, sound_frames=True, budget=10.0)
    assert sleep.calls == [8.0, 2.0]


async def test_play_progress_socket_frozen_when_budget_spent():
    # No budget left: all stage lines still print (full, terminated), instantly.
    steps = [("pause", 10.0), ("progress", 5.0, "LATE")]
    sink, sleep = await _play(steps, sound_frames=False, budget=10.0)
    prog = [w for w in sink.writes if w.startswith("LATE")]
    assert len(prog) == reboot.PROGRESS_STAGES
    assert prog[-1].endswith("] done\n")
    assert all(w.endswith("\n") for w in sink.writes)
    assert sum(sleep.calls) == pytest.approx(10.0)   # only the pause consumed time


# =========================================================================
# Command layer (FakeSession)
# =========================================================================

_TINY = [("line", "POST OK"), ("sound", "power")]


def test_reboot_no_script_says_nothing_happens(session):
    session.reboot_steps = None
    assert dispatch(session, ".reboot") is None
    assert "Nothing happens." in session.text


def test_reboot_no_script_spends_no_token(session):
    session.reboot_steps = None
    dispatch(session, ".reboot")
    dispatch(session, ".reboot")
    session.reboot_steps = _TINY
    assert dispatch(session, ".reboot") == REBOOT


def test_reboot_returns_sentinel_without_emoting_yet(world, clock):
    a = FakeSession(world, name="Corvid", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)   # same room
    a.reboot_steps = _TINY
    assert dispatch(a, ".reboot") == REBOOT
    # The emote now fires server-side, AFTER the confirmation answer — so at the
    # command layer nothing has reached the room yet.
    assert "terminal reboots" not in b.text


def test_reboot_rate_bucket_one_per_window(world):
    clock = FakeClock()
    a = FakeSession(world, name="Corvid", clock=clock)
    a.reboot_steps = _TINY
    assert dispatch(a, ".reboot") == REBOOT
    assert dispatch(a, ".reboot") is None            # too soon
    assert REBOOT_REFUSED in a.text
    clock.t = a.config.reboot_rate_seconds + 1       # window elapsed
    assert dispatch(a, ".reboot") == REBOOT


def test_reboot_refused_never_sees_the_prompt(world):
    clock = FakeClock()
    a = FakeSession(world, name="Corvid", clock=clock)
    a.reboot_steps = _TINY
    assert dispatch(a, ".reboot") == REBOOT
    a.clear()
    assert dispatch(a, ".reboot") is None            # bucket refuses
    assert REBOOT_REFUSED in a.text
    assert "Reboot server?" not in a.text            # the question never appears


def test_reboot_appears_in_help_all_levels(session):
    dispatch(session, ".help")
    assert ".reboot" in session.text and "initialize server" in session.text


# =========================================================================
# Live server — socket + WebSocket doors
# =========================================================================

@pytest.fixture
async def server(tmp_path):
    # A tiny, fast boot script (with a @progress) so integration timing stays
    # snappy but real.
    script = tmp_path / "reboot.txt"
    script.write_text(
        "@sound power\n@pause 0.15\nfirst boot line\n@beep\n@sound hdd\n"
        "@progress 0.3 REINIT\n@pause 0.15\nsecond boot line\n",
        encoding="utf-8",
    )
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0,
        ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "nope-cert.pem"),
        key_file=str(tmp_path / "nope-key.pem"),
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
        reboot_file=str(script),
    )
    srv = TalkerServer(cfg)
    await srv.start()
    plain_port = srv._servers[0].sockets[0].getsockname()[1]
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        yield srv, plain_port, ws_port
    finally:
        await srv.aclose()


# ---- helpers -------------------------------------------------------------

async def sock_connect(port):
    return await asyncio.open_connection("127.0.0.1", port)


async def sock_collect(reader, timeout=0.4):
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


async def sock_send(writer, text):
    writer.write((text + "\n").encode())
    await writer.drain()


async def sock_login(reader, writer, name, pw="swordfish-x9"):
    await sock_collect(reader)
    await sock_send(writer, name)
    await sock_collect(reader)
    await sock_send(writer, "yes")
    await sock_collect(reader)
    await sock_send(writer, pw)
    await sock_collect(reader)
    await sock_send(writer, pw)
    await sock_collect(reader)
    await sock_send(writer, "y")
    return await sock_collect(reader, timeout=1.0)


async def ws_collect(ws, timeout=0.5, keep_control=False):
    frames = []
    while True:
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout)
        except (asyncio.TimeoutError, Exception):
            break
        if isinstance(frame, (bytes, bytearray)):
            frame = frame.decode(errors="replace")
        if frame.startswith("\x01") and not keep_control:
            continue
        frames.append(frame)
    return frames


async def ws_login(ws, name, pw="swordfish-x9"):
    await ws_collect(ws)
    await ws.send(name)
    await ws_collect(ws)
    await ws.send("yes")
    await ws_collect(ws)
    await ws.send(pw)
    await ws_collect(ws)
    await ws.send(pw)
    await ws_collect(ws)
    await ws.send("y")
    await ws_collect(ws)                # the remember-me question (web door)
    await ws.send("n")
    return "\n".join(await ws_collect(ws, timeout=1.0))


# ---- the fake confirmation ----------------------------------------------

@pytest.mark.parametrize("answer", ["y", "n", "", "wibble"])
async def test_confirmation_any_answer_runs_the_sequence(server, answer):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as inv, \
               ws_connect(f"ws://127.0.0.1:{ws_port}") as room:
        await ws_login(inv, "Corvid")
        await ws_login(room, "Wren")
        await ws_collect(inv)
        await ws_collect(room)

        await inv.send(".reboot")
        prompt = "\n".join(await ws_collect(inv, 0.6))
        assert REBOOT_PROMPT.strip() in prompt          # exact question text
        # The room has heard nothing yet — the emote waits for the answer.
        assert "terminal reboots" not in "\n".join(await ws_collect(room, 0.3))

        await inv.send(answer)                           # ignored; runs regardless
        inv_frames = await ws_collect(inv, 1.5, keep_control=True)
        room_frames = await ws_collect(room, 1.5, keep_control=True)
        assert any("first boot line" in f for f in inv_frames)     # it ran
        # No acknowledgement of the answer is ever shown.
        assert all("Rebooting" not in f and "Cancelled" not in f for f in inv_frames)
        # The emote fires now, after the answer.
        assert any("Corvid's terminal reboots." in f for f in room_frames)


async def test_confirmation_prompt_precedes_any_output(server):
    srv, plain_port, ws_port = server
    r, w = await sock_connect(plain_port)
    await sock_login(r, w, "Corvid")
    await sock_send(w, ".reboot")
    first = await sock_collect(r, timeout=0.6)
    # The very first thing back is the question — no boot lines, no BEL yet.
    assert "Reboot server? (y/n)" in first
    assert "first boot line" not in first and "\x07" not in first
    await sock_send(w, "n")
    out = await sock_collect(r, timeout=1.5)
    assert "first boot line" in out                      # "n" ran it anyway


# ---- sound + progress reach only the invoker ----------------------------

async def test_web_sound_and_progress_frames_reach_only_the_invoker(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as inv, \
               ws_connect(f"ws://127.0.0.1:{ws_port}") as room:
        await ws_login(inv, "Corvid")
        await ws_login(room, "Wren")
        await ws_collect(inv)
        await ws_collect(room)

        await inv.send(".reboot")
        await ws_collect(inv, 0.6)                        # prompt
        await inv.send("n")
        inv_frames = await ws_collect(inv, timeout=1.5, keep_control=True)
        room_frames = await ws_collect(room, timeout=1.5, keep_control=True)

        assert "\x01sound power" in inv_frames
        assert "\x01sound hdd" in inv_frames
        assert "\x01progress 0.3 REINIT" in inv_frames
        assert any("first boot line" in f for f in inv_frames)

        # The roommate gets exactly one dry emote — no sound, no progress, no lines.
        assert any("Corvid's terminal reboots." in f for f in room_frames)
        assert all(not f.startswith("\x01") for f in room_frames)
        assert all("boot line" not in f and "REINIT" not in f for f in room_frames)


async def test_socket_gets_bel_and_staged_progress_lines(server):
    srv, plain_port, ws_port = server
    r, w = await sock_connect(plain_port)
    await sock_login(r, w, "Corvid")
    await sock_send(w, ".reboot")
    await sock_collect(r, timeout=0.6)                    # prompt
    await sock_send(w, "n")
    out = await sock_collect(r, timeout=2.0)
    assert "\x07" in out                                 # BEL patterns
    assert "\x01" not in out                             # never a web control frame
    # Staged full lines (tt++-safe): the empty first stage and the full "done".
    assert "REINIT [                    ]" in out
    assert "REINIT [####################] done" in out
    assert "first boot line" in out


async def test_reboot_ends_with_banner_and_look(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as inv:
        await ws_login(inv, "Corvid")
        await inv.send(".reboot")
        await ws_collect(inv, 0.6)                        # prompt
        await inv.send("n")
        tail = "\n".join(await ws_collect(inv, timeout=1.8))
        # The banner is now greeting.txt (the repo file, picked up like
        # agreement.txt); its koan line is the stable thing to pin.
        assert "You had to be there." in tail
        assert "What's said here stays here." in tail
        assert "atrium" in tail.lower()


async def test_input_during_boot_is_swallowed(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as inv, \
               ws_connect(f"ws://127.0.0.1:{ws_port}") as room:
        await ws_login(inv, "Corvid")
        await ws_login(room, "Wren")
        await ws_collect(inv)
        await ws_collect(room)

        await inv.send(".reboot")
        await ws_collect(inv, 0.6)                        # prompt
        await inv.send("n")                               # the (ignored) answer
        await inv.send("hello during the boot")           # must be swallowed
        await ws_collect(inv, timeout=1.6)
        room_after = "\n".join(await ws_collect(room, timeout=0.6))
        assert "hello during the boot" not in room_after
        assert "Corvid says" not in room_after


async def test_speech_during_boot_unseen_by_rebooter_but_seen_by_others(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as inv, \
               ws_connect(f"ws://127.0.0.1:{ws_port}") as speaker, \
               ws_connect(f"ws://127.0.0.1:{ws_port}") as bystander:
        await ws_login(inv, "Corvid")
        await ws_login(speaker, "Wren")
        await ws_login(bystander, "Finch")
        for s in (inv, speaker, bystander):
            await ws_collect(s)

        await inv.send(".reboot")
        await ws_collect(inv, 0.6)                        # prompt
        await inv.send("n")
        await asyncio.sleep(0.05)                         # mid-boot
        await speaker.send("the tide is turning")
        inv_tail = "\n".join(await ws_collect(inv, timeout=1.6))
        bystander_tail = "\n".join(await ws_collect(bystander, timeout=0.6))

        assert "Wren says: the tide is turning" in bystander_tail
        assert "the tide is turning" not in inv_tail


async def test_missing_script_says_nothing_happens_no_prompt(tmp_path):
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0,
        ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "no-cert.pem"), key_file=str(tmp_path / "no-key.pem"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"), mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"), state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell", owner_name="Generic", reboot_file=str(tmp_path / "absent.txt"),
    )
    srv = TalkerServer(cfg)
    assert srv.reboot_steps is None
    await srv.start()
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as inv:
            await ws_login(inv, "Corvid")
            await inv.send(".reboot")
            tail = "\n".join(await ws_collect(inv, timeout=0.8))
            assert "Nothing happens." in tail
            assert "Reboot server?" not in tail          # no script -> no prompt
    finally:
        await srv.aclose()
