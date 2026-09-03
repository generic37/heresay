# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Regression tests for the v1.52.3 input-robustness pass (audit PASS 1).

  C1 — non-finite ban hours are REFUSED. `float("nan")` slipped every guard
       (`nan <= 0` is False, `min(nan, ceiling)` is nan) and wrote a phantom
       ban: stored on disk, never enforced, never listed by `.bans`, never
       purged, and a bare `NaN` token in the JSON.
  C3 — the `isdigit()`/`int()` mismatch in three handlers, AND `dispatch`'s
       "never raises on user input" promise made structural rather than hoped
       for.
  W3 — `restart_schedule` raises `ControlError` instead of a bare ValueError /
       TypeError (no traceback for an ordinary bad request), and caps the
       countdown.
  W5 — `send_control` emits exactly ONE frame: the output-side counterpart of
       v1.51.0's one-frame-one-line input rule.

⚠ Every one of these was written and watched FAIL on the unfixed code before
the fix went in. Stores are on tmp paths throughout; the autouse
`no_repo_root_state` fixture in conftest is the backstop.
"""

import asyncio
import inspect
import json
import math
from pathlib import Path

import pytest

from heresay import control
from heresay.bans import BanStore
from heresay.commands import staff as S
from heresay.config import Config
from heresay.mail import MailStore
from heresay.server import TalkerServer
from heresay.session import Session
from heresay.theme import DEFAULT_THEME
import heresay.commands as C

from conftest import FakeSession

HOUSE = Path(__file__).resolve().parent / "fixtures" / "house"


# ===========================================================================
# C1 — non-finite ban hours
# ===========================================================================

NON_FINITE = ["nan", "NaN", "-nan", "inf", "-inf", "Infinity", "1e400"]


def _keeper(world, tmp_path, name="Staff"):
    """A staff session with a REAL BanStore behind it, on a tmp path."""
    bans = BanStore(str(tmp_path / "bans.json"))
    s = FakeSession(world, name=name, bans=bans, level="wizard")
    return s, bans


@pytest.mark.parametrize("bad", NON_FINITE)
def test_ban_hours_refuses_a_non_finite_value(world, tmp_path, bad):
    """The shared parser is where this has to be caught — one helper, both
    commands. Unfixed, `_ban_hours` returned `nan` (or `inf` clamped) instead
    of None."""
    s, _ = _keeper(world, tmp_path)
    assert S._ban_hours(s, ["Mallory", bad], 24.0) is None
    assert "positive number" in s.text


def test_ban_hours_still_takes_a_finite_value(world, tmp_path):
    """The guard must not over-reach: ordinary hours still parse and clamp."""
    s, _ = _keeper(world, tmp_path)
    assert S._ban_hours(s, ["Mallory", "6"], 24.0) == 6.0
    assert S._ban_hours(s, ["Mallory"], 24.0) == 24.0
    # still clamped at the configured ceiling
    s.config.ban_max_hours = 10.0
    assert S._ban_hours(s, ["Mallory", "99"], 24.0) == 10.0
    # and the old guards still hold
    assert S._ban_hours(s, ["Mallory", "0"], 24.0) is None
    assert S._ban_hours(s, ["Mallory", "-3"], 24.0) is None
    assert S._ban_hours(s, ["Mallory", "soon"], 24.0) is None


@pytest.mark.parametrize("bad", ["nan", "-nan", "inf"])
def test_ban_with_non_finite_hours_writes_NOTHING(world, tmp_path, bad):
    """⛔ The phantom. Unfixed, this stored a record whose `until` was NaN:
    never enforced (`nan > now` is False), never listed (same comparison),
    never purged (`nan <= now` is False) — and the staff was told
    "Banned Mallory for nanh." """
    s, bans = _keeper(world, tmp_path)
    S.cmd_ban(s, f"Mallory {bad}")

    assert "positive number" in s.text
    assert "Banned" not in s.text
    assert bans._bans == {}                       # nothing stored at all
    assert bans.list_active(now=0.0) == []
    assert not bans.is_banned_name("Mallory", now=0.0)
    assert not (tmp_path / "bans.json").exists()  # not even a file written


def test_ban_ip_refuses_non_finite_hours_before_touching_anything(world, tmp_path):
    """`.ban_ip` parses hours FIRST, so the refusal lands before any account or
    address lookup — and it is the same helper, which is the whole point."""
    s, bans = _keeper(world, tmp_path)
    assert S.cmd_ban_ip(s, "Mallory nan") is None
    assert "positive number" in s.text
    assert bans._bans == {}


def test_a_finite_ban_still_lands(world, tmp_path):
    """Guard against over-fixing: the ordinary path is untouched."""
    s, bans = _keeper(world, tmp_path)
    S.cmd_ban(s, "Mallory 6")
    assert "Banned Mallory for 6h." in s.text
    assert bans.is_banned_name("Mallory", now=s.now_wall())
    rec = bans._bans["name:mallory"]
    assert math.isfinite(rec["until"])
    # and it round-trips as STRICT json — the NaN token was not valid JSON
    json.loads((tmp_path / "bans.json").read_text())


def test_both_ban_commands_share_the_hours_parser():
    """The fix is in one helper because both callers use it. Pinned, so a future
    hand-rolled hours parse in either command is a failing test rather than a
    silent second code path."""
    for fn in (S.cmd_ban, S.cmd_ban_ip):
        assert "_ban_hours" in inspect.getsource(fn), fn.__name__


def test_the_hours_parser_checks_finiteness_explicitly():
    """`math.isfinite` rather than a comparison trick: `nan` fails every
    comparison silently, which is exactly how it got through."""
    assert "isfinite" in inspect.getsource(S._ban_hours)


# ===========================================================================
# C3 — isdigit()/int(), and dispatch never raises
# ===========================================================================

# `str.isdigit()` is True for these; `int()` raises on every one.
NON_ASCII_DIGITS = ["²", "³", "¹", "①"]   # ² ³ ¹ ①


def _mail_session(world, tmp_path, level="resident", name="Corvid"):
    mail = MailStore(str(tmp_path / "mailboxes.json"), str(tmp_path / "mail.key"))
    # level is a legacy INPUT token; FakeSession maps it to this house's rank
    # against its OWN (Commons) theme via _legacy_rank, so "arch" -> the
    # owner rank the session's gate actually recognises.
    return FakeSession(world, name=name, mail=mail, level=level)


@pytest.mark.parametrize("digit", NON_ASCII_DIGITS)
def test_dmail_with_a_non_ascii_digit_does_not_raise(world, tmp_path, digit):
    """⛔ Floor rank, one keystroke. Unfixed this raised ValueError out of the
    handler, out of `dispatch`, into `_serve`'s blanket handler — and the
    caller's connection was closed."""
    s = _mail_session(world, tmp_path)
    assert C.dispatch(s, f".dmail {digit}") is None
    assert "Usage: .dmail" in s.text


@pytest.mark.parametrize("digit", NON_ASCII_DIGITS)
def test_dstaffmail_with_a_non_ascii_digit_does_not_raise(world, tmp_path, digit):
    s = _mail_session(world, tmp_path, level="wizard", name="Staff")
    assert C.dispatch(s, f".dstaffmail {digit}") is None
    assert "Usage: .dstaffmail" in s.text


@pytest.mark.parametrize("digit", NON_ASCII_DIGITS)
def test_staff_actions_with_a_non_ascii_digit_does_not_raise(world, tmp_path,
                                                             digit):
    s = _mail_session(world, tmp_path, level="arch", name="Generic")
    assert C.dispatch(s, f".staff_actions {digit}") is None
    assert "Usage: .staff_actions" in s.text


def test_staff_actions_bounds_the_digit_run(world, tmp_path):
    """`.dmail`/`.dstaffmail` had `len(arg) > 9`; `.staff_actions` did not, so a
    >4300-digit run hit CPython's int-conversion limit and raised. Latent
    (MAX_LINE_LENGTH caps the wire at ~1009 digits) — fixed anyway, because the
    reason it is unreachable is a constant somewhere else."""
    s = _mail_session(world, tmp_path, level="arch", name="Generic")
    S.cmd_staff_actions(s, "9" * 5000)
    assert "Usage: .staff_actions" in s.text


def test_ascii_digits_still_delete_a_message(world, tmp_path):
    """Guard against over-fixing: the ordinary numeric path still works."""
    mail = MailStore(str(tmp_path / "mailboxes.json"), str(tmp_path / "mail.key"))
    mail.deliver("Corvid", "Ann", "hello")
    s = FakeSession(world, name="Corvid", mail=mail)
    C.dispatch(s, ".dmail 1")
    assert "Message 1 deleted." in s.text
    assert mail.count("Corvid") == 0


# ---- the structural half: dispatch swallows a throwing handler -------------

def test_dispatch_does_not_propagate_a_throwing_handler(world, monkeypatch):
    """⛔ The promise made structural. `dispatch`'s docstring has always said
    "Never raises on user input"; until now that was true only for as long as
    every handler happened to be careful."""
    def boom(session, args):
        raise RuntimeError("the canary should not escape")

    monkeypatch.setitem(C._COMMANDS, "look", boom)
    s = FakeSession(world, name="Corvid")
    assert C.dispatch(s, ".look") is None
    assert "Something went wrong" in s.text


def test_bare_speech_is_covered_by_the_same_guard(world, monkeypatch):
    """Bare speech is the one route that does not go through the command table,
    so it needs the same guard or the promise has a hole exactly the width of
    the thing people do most."""
    def boom(session, line):
        raise RuntimeError("the canary should not escape")

    monkeypatch.setattr(C, "cmd_say", boom)
    s = FakeSession(world, name="Corvid")
    assert C.dispatch(s, "hello everyone") is None
    assert "Something went wrong" in s.text


def test_a_handler_error_puts_NO_user_text_in_the_log(world, monkeypatch, caplog):
    """The covenant half. The exception's own message carries the user's input
    (`invalid literal for int() with base 10: '²'`), so the guard may log the
    TYPE and nothing else — never `str(exc)`, never the args."""
    canary = "hunting-of-the-snark-9931"

    def boom(session, args):
        raise ValueError(f"invalid literal: {canary!r}")

    monkeypatch.setitem(C._COMMANDS, "look", boom)
    s = FakeSession(world, name="Corvid")
    with caplog.at_level("DEBUG", logger="heresay"):
        assert C.dispatch(s, f".look {canary}") is None
    assert canary not in caplog.text
    assert "ValueError" in caplog.text          # the fact IS recorded
    assert "Traceback" not in caplog.text


def test_the_session_is_not_closed_by_a_handler_error(world, monkeypatch):
    """The whole point: the connection survives."""
    def boom(session, args):
        raise RuntimeError("nope")

    monkeypatch.setitem(C._COMMANDS, "look", boom)
    s = FakeSession(world, name="Corvid")
    C.dispatch(s, ".look")
    assert not s._closing


def test_quit_still_returns_its_sentinel(world):
    """The guard must not eat a handler's RETURN value — QUIT, PASSWD and the
    rest are how the session loop is driven."""
    s = FakeSession(world, name="Corvid")
    assert C.dispatch(s, ".quit") == C.QUIT


# ===========================================================================
# W3 — restart_schedule: ControlError, and a cap
# ===========================================================================

@pytest.mark.parametrize("bad", [["x"], [None], [{}], ["1.5.2"], [[1]], "abc"])
def test_restart_schedule_refuses_malformed_checkpoints(bad):
    """Unfixed this raised a bare ValueError/TypeError, which fell past the
    ControlError branch into the blanket handler and put a full traceback in
    the process log for an ordinary bad request — the shape audit F7 fixed for
    `readline`."""
    with pytest.raises(control.ControlError):
        control.restart_schedule(bad)


def test_restart_schedule_caps_the_countdown():
    """`deploy.sh` BLOCKS on this synchronously, so an uncapped mark ties up a
    deploy for as long as it says. `[10**9]` armed a 31-year countdown."""
    with pytest.raises(control.ControlError) as exc:
        control.restart_schedule([10 ** 9])
    assert str(control.MAX_RESTART_SECONDS) in str(exc.value)


def test_restart_schedule_accepts_the_cap_itself():
    assert control.restart_schedule([control.MAX_RESTART_SECONDS]) == \
        [control.MAX_RESTART_SECONDS]


def test_restart_schedule_still_sorts_filters_and_dedupes():
    """The v1.46.0 behaviour, pinned so the new guard cannot be read as having
    replaced it."""
    assert control.restart_schedule([120, 60, 10]) == [120, 60, 10]
    assert control.restart_schedule([10, 60, 120]) == [120, 60, 10]
    assert control.restart_schedule([0, -5, 60]) == [60]
    assert control.restart_schedule([60, 60, 10]) == [60, 10]
    assert control.restart_schedule([]) == []
    assert control.restart_schedule([1.0, 2.0]) == [2, 1]


def _cfg(tmp_path):
    return Config(
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


@pytest.mark.asyncio
async def test_a_malformed_restart_request_gets_no_traceback(tmp_path, caplog):
    """End to end over the real socket, mirroring the F7 test: a bad request is
    ANSWERED, not logged as a crash."""
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        path = str(srv.config.control_socket_path())
        for bad in (["x"], [None], [10 ** 9]):
            with caplog.at_level("ERROR"):
                caplog.clear()
                reader, writer = await asyncio.open_unix_connection(path)
                writer.write(json.dumps(
                    {"cmd": "restart", "checkpoints": bad}).encode() + b"\n")
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), 5)
                writer.close()
            reply = json.loads(raw.decode())
            assert reply["ok"] is False, bad
            assert "Traceback" not in caplog.text, bad
            assert "control: request failed" not in caplog.text, bad
    finally:
        await srv.aclose()


# ===========================================================================
# W5 — send_control emits exactly one frame
# ===========================================================================

class _WebLikeTransport:
    """A door with the control convention, whose `send_text` splits on newlines
    exactly as `WebSocketTransport.send_text` does — which is the behaviour the
    bug rode on."""

    wants_prompt = False
    supports_ansi = False
    supports_control_frames = True
    supports_sound_frames = True
    is_local_console = False
    reported_width = None
    pending_resume = None
    rejected = None

    def __init__(self):
        self.peer_ip = "127.0.0.1"
        self.frames = []     # what the browser would receive
        self.raw = []        # what send_control handed to the transport

    def send_text(self, text):
        self.raw.append(text)
        if text == "":
            return
        body = text[:-1] if text.endswith("\n") else text
        self.frames.extend(body.split("\n"))

    def prompt(self):
        pass

    def set_echo(self, on):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    def is_closing(self):
        return False


def _real_session(world, transport, tmp_path):
    """A REAL `Session` — not the conftest fake, whose `send_control` is a stub
    that appends to a list and so cannot see this bug at all."""
    from heresay.accounts import AccountStore
    accounts = AccountStore(str(tmp_path / "accounts.json"), ladder=DEFAULT_THEME)
    return Session(transport, world, accounts, Config())


@pytest.mark.parametrize("payload", [
    "\x01title Wonder\nland",
    "\x01title Wonder\r\nland",
    "\x01title Wonder\rland",
    "\x01title a\nb\nc\nd",
])
def test_send_control_emits_exactly_one_frame(world, tmp_path, payload):
    """⛔ Unfixed, `\\x01title Wonder\\nland` produced TWO frames —
    `['\\x01title Wonder', 'land']` — a control frame plus a line displayed in
    the terminal. The output-side counterpart of v1.51.0's input rule."""
    t = _WebLikeTransport()
    s = _real_session(world, t, tmp_path)
    s.send_control(payload)
    assert len(t.frames) == 1, t.frames
    assert t.frames[0].startswith("\x01title ")


def test_send_control_hands_the_transport_a_single_line(world, tmp_path):
    """Asserted at the choke point itself, so the guarantee does not depend on
    any one door's `send_text`."""
    t = _WebLikeTransport()
    s = _real_session(world, t, tmp_path)
    s.send_control("\x01title Wonder\nland")
    assert len(t.raw) == 1
    assert t.raw[0].count("\n") == 1 and t.raw[0].endswith("\n")
    assert "\r" not in t.raw[0]


def test_an_ordinary_control_frame_is_unchanged(world, tmp_path):
    """Guard against over-fixing: the frames the house actually sends are
    byte-identical to what they were."""
    t = _WebLikeTransport()
    s = _real_session(world, t, tmp_path)
    for payload in ("\x01echo off", "\x01tab_alert on", "\x01raise_bounds 0 10",
                    "\x01title Commons", "\x01inworld on", "\x01favicon "):
        t.frames.clear()
        s.send_control(payload)
        assert t.frames == [payload], payload


def test_a_socket_door_still_receives_no_control_frame(world, tmp_path):
    """The web-only rule is unchanged: a terminal would PRINT the frame."""
    class _Socketish(_WebLikeTransport):
        supports_control_frames = False

    t = _Socketish()
    s = _real_session(world, t, tmp_path)
    s.send_control("\x01title Wonder\nland")
    assert t.frames == [] and t.raw == []
