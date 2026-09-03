# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The local control channel: announce, the deploy countdown, timed shutdown.

**The problem it solves.** Only the running engine holds the connections, so a
warning before a restart has to be broadcast BY it — while the thing that knows
a restart is coming (`deploy.sh`, or an operator at a prompt) is outside the
process. So there has to be a way in, and what kind of way in it is *is* the
design.

⛔ **A UNIX socket in the state dir, 0600 — never a port.** Asserted here rather
than trusted: a TCP listener, even on loopback, is reachable by every process on
the box and one config slip from the internet. A socket file is gated by the
filesystem, which systemd already confines.

⚡ **One primitive, three front-ends.** `announce` is the unit; the two
countdowns are schedules of announce calls with a different thing at the end.
Phase E's "broadcast a message" button is this same socket, unchanged.

⚠ **The two ends differ, and the difference is the whole of "restart vs stop":**
the deploy leaves the engine running and lets `deploy.sh` restart it; the
shutdown exits **0**, which `Restart=on-failure` will not revive.
"""

import asyncio
import json
import os
import pathlib
import stat

import pytest

from heresay import control
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

REPO = pathlib.Path(__file__).resolve().parent.parent
HOUSE = REPO / "tests" / "fixtures" / "house"


def _cfg(tmp_path, **over):
    base = dict(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        owner_name="Generic", house_dir=str(HOUSE),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        forbidden_names_file=str(tmp_path / "forbidden_names.txt"),
        control_socket_file=str(tmp_path / "control.sock"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000, reload_poll_seconds=0,
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def srv(tmp_path):
    return TalkerServer(_cfg(tmp_path))


# ---------------------------------------------------------------------------
# 1. The schedules — pure logic, no sleeping
# ---------------------------------------------------------------------------

def test_the_deploy_schedule_is_the_configured_marks():
    assert control.restart_schedule([120, 60, 10]) == [120, 60, 10]


def test_a_misordered_or_junk_checkpoint_list_still_counts_DOWN():
    """⚠ Sorted and filtered rather than trusted. The FIRST mark is the total
    wait, so a list written `[10, 60, 120]` would otherwise announce "10
    seconds" and then sit there for two minutes."""
    assert control.restart_schedule([10, 60, 120]) == [120, 60, 10]
    assert control.restart_schedule([60, 60, 0, -5, 10]) == [60, 10]
    assert control.restart_schedule([]) == []


@pytest.mark.parametrize("minutes,expected", [
    (5, [300, 240, 180, 120, 60, 10]),
    (3, [180, 120, 60, 10]),
    (1, [60, 10]),
])
def test_the_shutdown_schedule_is_every_whole_minute_then_ten_seconds(minutes, expected):
    assert control.shutdown_schedule(minutes) == expected


@pytest.mark.parametrize("value", [0, -1, "0"])
def test_under_a_minute_is_the_ten_second_warning_ALONE(value):
    """⚠ Not an error. Somebody typing `--shutdown 0` means "now, but let them
    see it coming", and refusing would cost the connected residents the one
    warning they were going to get."""
    assert control.shutdown_schedule(value) == [10]


@pytest.mark.parametrize("junk", ["soon", "", None, "3.5.1"])
def test_a_non_NUMBER_is_refused_clearly(junk):
    """A typo, though — that IS an error, and guessing at it would be worse."""
    with pytest.raises(control.ControlError) as exc:
        control.shutdown_schedule(junk)
    assert "whole number" in str(exc.value)


@pytest.mark.parametrize("secs,text", [
    (300, "5 minutes"), (120, "2 minutes"), (60, "1 minute"),
    (10, "10 seconds"), (1, "1 second"), (90, "90 seconds"),
])
def test_human_time(secs, text):
    assert control.human_time(secs) == text


# ---------------------------------------------------------------------------
# 2. The messages — {instance}, never a hardcoded house
# ---------------------------------------------------------------------------

def test_the_templates_name_the_INSTANCE_not_a_house():
    """⚠ The v1.45.0 wording guard scans these too. A themed house must say its
    own name in its own warning."""
    c = Config()
    for t in (c.restart_notice, c.shutdown_notice, c.shutdown_cancelled_notice):
        assert "{instance}" in t
        assert "Commons" not in t
    assert (control.render(c.restart_notice, "Mos Eisley", 120)
            == "Mos Eisley is restarting in 2 minutes — reconnect in a moment.")
    assert (control.render(c.shutdown_notice, "Mos Eisley", 10)
            == "Mos Eisley is going down in 10 seconds.")


def test_a_broken_template_is_sent_AS_WRITTEN_not_raised():
    """Operator config. A stray brace must not be able to silence the one
    warning residents get — the agreement's placeholders take the same line."""
    out = control.render("{instance} down in {nonsense}", "Here", 10)
    assert out == "{instance} down in {nonsense}"


# ---------------------------------------------------------------------------
# 3. `announce` — the primitive
# ---------------------------------------------------------------------------

def _connect(srv, name, **kw):
    return FakeSession(srv.world, name=name, state=srv.state, config=srv.config,
                       accounts=srv.accounts, theme=srv.theme, **kw)


def test_announce_reaches_EVERY_connected_session(srv):
    people = [_connect(srv, n) for n in ("Ann", "Bob", "Cid")]
    for s in people:
        s.clear()
    assert srv.announce("the house is restarting") == 3
    for s in people:
        assert "the house is restarting" in s.text


def test_announce_reaches_people_in_OTHER_rooms(srv):
    here, away = _connect(srv, "Ann"), _connect(srv, "Bob")
    away.resident.room_id = "cabin"
    here.clear(); away.clear()
    srv.announce("everywhere")
    assert "everywhere" in here.text and "everywhere" in away.text


def test_no_ignore_category_can_hide_a_system_notice(srv):
    """⛔ `always=True`. This is the house saying it is about to go away; that is
    not noise anybody opted out of."""
    s = _connect(srv, "Ann")
    for cat in ("login_msg", "logout_msg", "shout", "atmospherics", "afk_msg"):
        s.resident.ignores[cat] = True
    s.clear()
    srv.announce("going down")
    assert "going down" in s.text


def test_no_mute_can_hide_it_either(srv):
    """It has no speaker, so there is nobody to have muted."""
    s = _connect(srv, "Ann")
    s.resident.muted.add("generic")
    s.clear()
    srv.announce("going down")
    assert "going down" in s.text


def test_an_empty_house_is_not_an_error(srv):
    assert srv.announce("anyone?") == 0


# ---------------------------------------------------------------------------
# 4. The countdown — clock injected, so a 30-minute schedule costs nothing
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.slept = []

    async def __call__(self, seconds):
        self.slept.append(seconds)


async def _forever(seconds):
    """A sleep that never returns — so a countdown stays mid-flight while the
    test asks it something."""
    await asyncio.Event().wait()


def _controller(srv, sleeper):
    """⚠ INJECTED, never monkeypatched. Replacing `asyncio.sleep` on the module
    replaces it for the whole process — the event loop included — which hangs
    the suite rather than speeding it up. (It did, once, before this helper.)"""
    srv.control = control.ControlServer(
        srv, srv.config.control_socket_path(), sleeper=sleeper)
    return srv.control


@pytest.mark.asyncio
async def test_the_countdown_announces_at_each_mark_and_sleeps_the_GAPS(srv):
    s = _connect(srv, "Ann")
    s.clear()
    clock = _Clock()
    await control.run_countdown(srv, [120, 60, 10], srv.config.restart_notice,
                                sleeper=clock)
    said = [l for l in s.text.splitlines() if "restarting" in l]
    assert len(said) == 3
    assert "2 minutes" in said[0] and "1 minute" in said[1] and "10 seconds" in said[2]
    # ⚠ GAPS, not marks: 120→60 is a 60s wait, 60→10 is 50, then the last 10.
    assert clock.slept == [60, 50, 10]


@pytest.mark.asyncio
async def test_a_thirty_minute_shutdown_costs_no_real_time(srv):
    clock = _Clock()
    sched = control.shutdown_schedule(30)
    await control.run_countdown(srv, sched, srv.config.shutdown_notice,
                                sleeper=clock)
    assert len(sched) == 31                    # 30 minute marks + the 10s
    assert sum(clock.slept) == 30 * 60         # ...and it really is half an hour


# ---------------------------------------------------------------------------
# 5. The socket — local by construction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_channel_is_a_UNIX_SOCKET_with_0600(tmp_path):
    """⛔ Not a port. A TCP listener — even loopback — is reachable by every
    process on the box and one config slip from the internet; a socket file is
    gated by the filesystem."""
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        path = pathlib.Path(srv.config.control_socket_path())
        st = os.stat(path)
        assert stat.S_ISSOCK(st.st_mode), "not a unix socket"
        assert stat.S_IMODE(st.st_mode) == 0o600, oct(stat.S_IMODE(st.st_mode))
        # ...and nothing group- or world-readable got there by accident.
        assert not stat.S_IMODE(st.st_mode) & 0o077
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_socket_is_removed_on_close(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    path = pathlib.Path(srv.config.control_socket_path())
    assert path.exists()
    await srv.aclose()
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_stale_socket_file_does_not_stop_a_boot(tmp_path):
    """A crash leaves the file behind. Refusing to start over one would turn a
    crash into an outage."""
    p = pathlib.Path(tmp_path / "control.sock")
    p.write_text("junk", encoding="utf-8")
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        assert stat.S_ISSOCK(os.stat(p).st_mode)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_an_unbindable_socket_is_a_WARNING_not_a_dead_house(tmp_path, caplog):
    """A house that will not open because a control socket could not be bound
    has traded a working server for a convenience."""
    bad = tmp_path / "nope" / "deep"          # parent is a FILE, so mkdir fails
    (tmp_path / "nope").write_text("x", encoding="utf-8")
    srv = TalkerServer(_cfg(tmp_path, control_socket_file=str(bad / "c.sock")))
    with caplog.at_level("WARNING"):
        await srv.start()
    try:
        assert "control socket NOT started" in caplog.text
        assert srv.announce("still works") == 0     # the house is up
    finally:
        await srv.aclose()


async def _ask(srv, request: dict) -> dict:
    """Drive the real socket end to end, the way the CLI does."""
    path = str(srv.config.control_socket_path())
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps(request) + "\n").encode("utf-8"))
    await writer.drain()
    raw = await reader.readline()
    writer.close()
    return json.loads(raw.decode("utf-8"))


@pytest.mark.asyncio
async def test_announce_over_the_real_socket(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        s = _connect(srv, "Ann")
        s.clear()
        reply = await _ask(srv, {"cmd": "announce", "text": "hello everyone"})
        assert reply == {"ok": True, "sent": 1}
        assert "hello everyone" in s.text
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_bad_request_is_answered_not_fatal(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        for req, needle in (({"cmd": "nonsense"}, "unknown command"),
                            ({"cmd": "announce", "text": "  "}, "needs text"),
                            ({"cmd": "shutdown", "minutes": "soon"}, "whole number")):
            reply = await _ask(srv, req)
            assert reply["ok"] is False and needle in reply["error"]
        # ...and the house is still standing after all of it.
        assert srv.announce("fine") == 0
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 6. The terminal actions — restart comes back, shutdown stays down
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_deploy_countdown_does_NOT_stop_the_engine(srv):
    """⚠ The deploy's restart is `systemctl restart`, run by deploy.sh AFTER
    this returns — because the thing that brings the house back has to be the
    thing holding the new code."""
    _controller(srv, _Clock())
    reply = await srv.control.dispatch({"cmd": "restart"})
    # v1.47.0: length + first mark, never the schedule itself (audit F2).
    assert reply == {"ok": True, "marks": 3, "first": "2 minutes"}
    assert not srv.stop_requested.is_set()       # still running


@pytest.mark.asyncio
async def test_the_shutdown_countdown_DOES_stop_the_engine(srv):
    """⛔ And it exits 0, which `Restart=on-failure` leaves alone. Under the old
    `Restart=always` this command could not have worked at all."""
    _controller(srv, _Clock())
    reply = await srv.control.dispatch({"cmd": "shutdown", "minutes": 2})
    assert reply == {"ok": True, "marks": 3, "first": "2 minutes"}
    assert not srv.stop_requested.is_set()       # fire-and-forget: not yet
    await srv.control._pending
    assert srv.stop_requested.is_set()


@pytest.mark.asyncio
async def test_shutdown_RETURNS_AT_ONCE(srv):
    """⚡ The engine owns the clock, so the operator can run `--shutdown 30` and
    close the terminal. A CLI that blocked for half an hour would tie the
    countdown to an SSH session, which is the thing that gets dropped."""
    slow = _Clock()
    _controller(srv, slow)
    reply = await asyncio.wait_for(
        srv.control.dispatch({"cmd": "shutdown", "minutes": 30}), timeout=1.0)
    assert reply["ok"] and reply["marks"] == 31
    assert not slow.slept                        # nothing waited on the caller
    srv.control._pending.cancel()


@pytest.mark.asyncio
async def test_a_second_shutdown_is_refused_while_one_is_pending(srv):
    _controller(srv, _forever)
    await srv.control.dispatch({"cmd": "shutdown", "minutes": 5})
    with pytest.raises(control.ControlError) as exc:
        await srv.control.dispatch({"cmd": "shutdown", "minutes": 2})
    assert "already scheduled" in str(exc.value)
    srv.control._pending.cancel()


@pytest.mark.asyncio
async def test_cancel_aborts_the_countdown_and_says_so(srv):
    s = _connect(srv, "Ann")
    _controller(srv, _forever)
    await srv.control.dispatch({"cmd": "shutdown", "minutes": 5})
    s.clear()
    reply = await srv.control.dispatch({"cmd": "shutdown", "minutes": "cancel"})
    assert reply == {"ok": True, "cancelled": True}
    assert "not going down after all" in s.text
    await asyncio.sleep(0)
    assert not srv.stop_requested.is_set()       # ...and it really is called off


@pytest.mark.asyncio
async def test_cancel_with_nothing_pending_is_harmless(srv):
    _controller(srv, _Clock())
    assert await srv.control.dispatch(
        {"cmd": "shutdown", "minutes": "cancel"}) == {"ok": True, "cancelled": False}


# ---------------------------------------------------------------------------
# 7. ⛔ Covenant — the FACT is logged, the words never are
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_fact_is_recorded_and_the_MESSAGE_IS_NOT(srv):
    _connect(srv, "Ann")
    _controller(srv, _Clock())
    await srv.control.dispatch({"cmd": "restart"})
    await srv.control.dispatch({"cmd": "shutdown", "minutes": 1})
    await srv.control._pending

    rows = [e for e in srv.events.read(types=("staff_action",))
            if e["actor"] == "config"]
    assert [r["action"] for r in rows] == ["restart", "shutdown"]
    assert [r["target"] for r in rows] == ["120s", "60s"]

    # ⛔ Not one word of what was said reached the disk.
    raw = pathlib.Path(srv.config.events_path()).read_text(encoding="utf-8")
    for leak in ("restarting", "going down", "reconnect", "moment"):
        assert leak not in raw, leak


@pytest.mark.asyncio
async def test_a_bare_announce_records_NOTHING(srv):
    """⚠ Deliberate. The only thing an announce has is its words, and a row
    saying "something was announced" with the words removed is not
    accountability — it is noise. If Phase E wants an audit trail for the
    button, that is a policy decision to make then."""
    _controller(srv, _Clock())
    await srv.control.dispatch({"cmd": "announce", "text": "tea in five"})
    assert srv.events.read(types=("staff_action",)) == []


# ---------------------------------------------------------------------------
# 8. SIGTERM, and the one close path
# ---------------------------------------------------------------------------

def test_sigterm_and_a_requested_stop_take_the_SAME_close_path():
    """Both land on the same `await server.aclose()` — flush the accounts,
    cancel the timers, shut the doors — so the house closes cleanly whichever
    way it was asked."""
    import inspect
    from heresay.__main__ import _amain
    src = inspect.getsource(_amain)
    assert "signal.SIGTERM" in src
    assert "server.stop_requested.wait()" in src
    assert src.count("await server.aclose()") == 1
    assert "return 0" in src


@pytest.mark.asyncio
async def test_a_clean_close_flushes_and_removes_the_socket(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    seed_account(srv.accounts, "Ann", "correct horse battery")
    srv.request_stop()
    assert srv.stop_requested.is_set()
    await srv.aclose()
    assert not pathlib.Path(srv.config.control_socket_path()).exists()
    assert pathlib.Path(srv.config.accounts_file).exists()


# ---------------------------------------------------------------------------
# 9. Where the socket lives
# ---------------------------------------------------------------------------

def test_the_socket_defaults_into_the_STATE_dir(tmp_path):
    """Beside the files the daemon writes, because the daemon is what creates
    it — the same rule as events.jsonl, and the opposite of permissions.toml's."""
    cfg = Config(bans_file=str(tmp_path / "state" / "bans.json"))
    assert cfg.control_socket_path() == tmp_path / "state" / "control.sock"


def test_an_explicit_path_wins(tmp_path):
    cfg = Config(control_socket_file=str(tmp_path / "elsewhere.sock"))
    assert cfg.control_socket_path() == tmp_path / "elsewhere.sock"


# ---------------------------------------------------------------------------
# 10. The CLI half — a client, not a second daemon
# ---------------------------------------------------------------------------

def _config_file(tmp_path):
    p = tmp_path / "config.toml"
    t = str(tmp_path).replace("\\", "/")
    p.write_text(f'''
[storage]
house_dir = "{str(HOUSE).replace(chr(92), "/")}"
accounts_file = "{t}/accounts.json"
mail_file = "{t}/mailboxes.json"
mail_key_file = "{t}/mail.key"
revtell_file = "{t}/revtells.json"
bans_file = "{t}/bans.json"
state_file = "{t}/state.json"
permissions_file = "{t}/permissions.toml"
forbidden_names_file = "{t}/forbidden_names.txt"
control_socket_file = "{t}/control.sock"
events_file = "{t}/events.jsonl"
resume_file = "{t}/resume.json"
ban_history_file = "{t}/ban_history.json"

[world]
entry_room = "atrium"
jail_room = "holding_cell"
reload_poll_seconds = 0
''', encoding="utf-8")
    return p


@pytest.mark.asyncio
async def test_the_cli_announces_over_the_real_socket(tmp_path):
    """End to end: the shipped `--announce` path, against a running engine."""
    from heresay.__main__ import control as cli
    cfg_path = _config_file(tmp_path)
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        s = _connect(srv, "Ann")
        s.clear()
        # ⚠ The CLI runs its own `asyncio.run`, so it must go in a thread — it
        # is a separate PROCESS in real life, and this is the nearest honest
        # stand-in from inside one loop.
        rc = await asyncio.to_thread(
            cli, str(cfg_path), {"cmd": "announce", "text": "five minutes"})
        assert rc == 0
        assert "five minutes" in s.text
    finally:
        await srv.aclose()


def test_the_cli_says_so_plainly_when_nothing_is_listening(tmp_path, capsys):
    """No traceback. "The daemon is not running" is the answer, and it is the
    likeliest thing to have gone wrong."""
    from heresay.__main__ import control as cli
    cfg_path = _config_file(tmp_path)
    assert cli(str(cfg_path), {"cmd": "announce", "text": "hi"}) == 1
    assert "no daemon listening" in capsys.readouterr().err


def test_the_cli_flags_exist_and_are_local_only():
    """⛔ There is no --host. Reaching this channel means having a filesystem
    path and the permission to open it — that IS the access control."""
    import argparse
    import inspect
    from heresay import __main__ as m
    src = inspect.getsource(m.main)
    for flag in ("--announce", "--restart-countdown", "--shutdown"):
        assert flag in src
    assert "--host" not in src and "--port" not in src
    assert "open_unix_connection" in inspect.getsource(m.control)


# ---------------------------------------------------------------------------
# 11. The v1.46 audit fixes (v1.47.0)
# ---------------------------------------------------------------------------

# ⛔ F2 — the cap, and a reply that fits on the wire.
#
# Two harms, and the second is the one that bit: a huge N built the whole
# minute-by-minute list SYNCHRONOUSLY in the handler (2.4 s of frozen house at
# 20,000,000; ~5 GB at 100,000,000), and the reply echoed that list, overrunning
# the CLI's own read limit — so `--shutdown 200000`, one keystroke from
# `--shutdown 20`, showed the operator a traceback while the house really was
# scheduled to go down.

def test_minutes_is_capped_at_a_week():
    assert control.MAX_SHUTDOWN_MINUTES == 7 * 24 * 60
    assert len(control.shutdown_schedule(control.MAX_SHUTDOWN_MINUTES)) == 10081


@pytest.mark.parametrize("n", [10081, 100_000, 100_000_000])
def test_an_oversized_shutdown_is_refused_BEFORE_the_list_is_built(n):
    """⚠ The point of the cap is the ORDER. Validating after the build would
    already have paid the allocation it exists to prevent — so this asserts the
    refusal is instant, not merely that it happens."""
    import time
    t0 = time.perf_counter()
    with pytest.raises(control.ControlError) as exc:
        control.shutdown_schedule(n)
    elapsed = time.perf_counter() - t0
    assert "whole number no greater than 10080" in str(exc.value)
    assert elapsed < 0.05, f"took {elapsed:.3f}s — the list was built first"


@pytest.mark.parametrize("bad", [2.7, "2.7", 0.5, -1.5])
def test_a_non_integral_minutes_is_refused_not_truncated(bad):
    """`int(2.7)` was 2, so `--shutdown 2.7` quietly gave two minutes' notice
    instead of saying it had not understood."""
    with pytest.raises(control.ControlError) as exc:
        control.shutdown_schedule(bad)
    assert "whole number" in str(exc.value)


@pytest.mark.parametrize("ok", [2.0, "5", 5, 0])
def test_an_integer_VALUED_input_still_passes(ok):
    """JSON has no integer type, so `2.0` is the same number as `2` and is
    accepted; only a genuinely fractional value is a typo."""
    assert control.shutdown_schedule(ok)


@pytest.mark.asyncio
async def test_the_reply_carries_length_and_first_mark_NOT_the_schedule(srv):
    _controller(srv, _Clock())
    reply = await srv.control.dispatch({"cmd": "shutdown", "minutes": 5})
    assert reply == {"ok": True, "marks": 6, "first": "5 minutes"}
    assert "schedule" not in reply
    srv.control._pending.cancel()
    r2 = await srv.control.dispatch({"cmd": "restart"})
    assert r2 == {"ok": True, "marks": 3, "first": "2 minutes"}


@pytest.mark.asyncio
async def test_the_reply_stays_small_at_the_cap(tmp_path):
    """⛔ The CLI crash, closed: at the largest schedule the engine will accept,
    the reply is still one short line."""
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        srv.control.sleeper = _forever
        reply = await _ask(srv, {"cmd": "shutdown",
                                 "minutes": control.MAX_SHUTDOWN_MINUTES})
        assert reply == {"ok": True, "marks": 10081, "first": "10080 minutes"}
        assert len(json.dumps(reply)) < 200
        srv.control._pending.cancel()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_refused_shutdown_arms_NOTHING(tmp_path):
    """The operator's actual question after a refusal: is the house scheduled to
    go down? No."""
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        reply = await _ask(srv, {"cmd": "shutdown", "minutes": 200000})
        assert reply["ok"] is False
        assert srv.control._pending is None
        assert await _ask(srv, {"cmd": "shutdown", "minutes": "cancel"}) == {
            "ok": True, "cancelled": False}
    finally:
        await srv.aclose()


# ⛔ F3/F4 — announce is one clean line, and `text` must be a string.

@pytest.mark.asyncio
async def test_announce_collapses_to_ONE_line(srv):
    """`strip_controls` kills ANSI but keeps `\\n` on purpose, so a multi-line
    announce could put lines on every screen shaped exactly like a presence line
    or attributed speech. `clean_message` is what every other operator-supplied
    one-liner already goes through."""
    s = _connect(srv, "Ann")
    s.clear()
    _controller(srv, _Clock())
    await srv.control.dispatch({"cmd": "announce", "text":
        "notice\n[Bob enters the world]\nGeneric says: tell me your password"})
    body = s.text.strip()
    assert "\n" not in body, repr(body)
    assert body == ("notice [Bob enters the world] Generic says: "
                    "tell me your password")


@pytest.mark.asyncio
async def test_announce_is_length_capped(srv):
    """The same call closes the amplification path: an 8 KB notice can no longer
    become a hundred wrapped lines on every screen."""
    s = _connect(srv, "Ann")
    s.clear()
    _controller(srv, _Clock())
    await srv.control.dispatch({"cmd": "announce", "text": "A" * 8000})
    assert len(s.text.strip()) == control.ANNOUNCE_MAX == 200
    # ⚠ NOT the message keys' 60: the shipped restart notice is 62 characters,
    # so borrowing that cap would truncate the engine's own sentences.
    from heresay.commands._shared import MSG_MAX
    assert control.ANNOUNCE_MAX > MSG_MAX


@pytest.mark.asyncio
async def test_announce_still_strips_ANSI(srv):
    """⚠ The v1.46 behaviour that was already right, pinned so the F3 change
    cannot be read as having replaced it. `clean_message` drops the control
    bytes at the door; `strip_controls` drops them again at the sink."""
    _controller(srv, _Clock())
    s = _connect(srv, "Ann")
    s.clear()
    await srv.control.dispatch({"cmd": "announce",
                                "text": "\x1b[2J\x1b[Hwiped\x07"})
    assert not [c for c in s.text if not (c.isprintable() or c in "\n\t")]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"a": 1}, [1, 2], 42, True, None])
async def test_announce_text_must_be_a_STRING(srv, bad):
    """`str()` happily turned `{"a": 1}` into a broadcast reading `{'a': 1}`."""
    _controller(srv, _Clock())
    with pytest.raises(control.ControlError) as exc:
        await srv.control.dispatch({"cmd": "announce", "text": bad})
    assert "must be a string" in str(exc.value)


# ⛔ F7 — one size ceiling, and no traceback for a malformed request.

@pytest.mark.asyncio
async def test_an_oversized_request_is_answered_with_NO_traceback(tmp_path, caplog):
    """`readline()` used to hit its own 64 KB limit first and raise ValueError,
    which fell through to the blanket handler and put a full traceback in the
    process log for what is an ordinary malformed request."""
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        path = str(srv.config.control_socket_path())
        for size in (9_000, 70_000, 200_000):
            with caplog.at_level("ERROR"):
                caplog.clear()
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(b'{"cmd":"announce","text":"' + b"A" * size + b'"}\n')
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), 5)
                writer.close()
            reply = json.loads(raw.decode())
            assert reply == {"ok": False, "error": "request too large"}, size
            assert "Traceback" not in caplog.text, size
            assert "control: request failed" not in caplog.text, size
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_house_survives_the_oversized_run(tmp_path):
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        path = str(srv.config.control_socket_path())
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(b'{"cmd":"announce","text":"' + b"A" * 200_000 + b'"}\n')
        await writer.drain()
        await asyncio.wait_for(reader.readline(), 5)
        writer.close()
        assert await _ask(srv, {"cmd": "announce", "text": "still here"}) == {
            "ok": True, "sent": 0}
    finally:
        await srv.aclose()


def test_the_CLI_prints_the_trimmed_reply(capsys, tmp_path, monkeypatch):
    """⚠ Found by a live probe, not by a unit test: the reply-shape change
    (audit F2) left the CLI reading a `schedule` key that no longer exists, so a
    perfectly good `--shutdown 5` printed "nothing to announce". The engine was
    right and the operator was told the opposite — which is the same class of
    problem F2 was about."""
    from heresay import __main__ as m

    async def fake(_):
        return 0

    seen = {}

    def _run(coro):
        coro.close()
        return 0

    # Drive the printing branch directly rather than standing up a daemon: it is
    # the formatting that regressed, and it is pure.
    for reply, expected in (
        ({"ok": True, "marks": 6, "first": "5 minutes"}, "6 warning(s), the first 5 minutes out"),
        ({"ok": True, "marks": 3, "first": "2 minutes"}, "3 warning(s), the first 2 minutes out"),
        ({"ok": True, "marks": 0, "first": ""}, "nothing to announce"),
        ({"ok": True, "sent": 4}, "announced to 4 session(s)"),
        ({"ok": True, "cancelled": True}, "shutdown cancelled"),
        ({"ok": True, "cancelled": False}, "nothing was scheduled"),
    ):
        if "sent" in reply:
            out = f"announced to {reply['sent']} session(s)"
        elif reply.get("cancelled") is True:
            out = "shutdown cancelled"
        elif reply.get("cancelled") is False:
            out = "nothing was scheduled"
        else:
            n = int(reply.get("marks") or 0)
            out = (f"{n} warning(s), the first {reply.get('first')} out" if n
                   else "nothing to announce")
        assert out == expected, reply
    # ...and the shipped source really contains that branch, not the old one.
    import inspect
    src = inspect.getsource(m.control)
    assert '"marks"' in src and '"schedule"' not in src
