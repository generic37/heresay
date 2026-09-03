# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""WebSocket door: parity with the socket door, and password control frames.

Scrollback cap and disconnect-clear are client-side (browser); they are covered
by the manual-test checklist in the README, not here.
"""

import asyncio
import re
from pathlib import Path

import pytest
from websockets.asyncio.client import connect as ws_connect

from heresay.config import Config
from heresay.server import TalkerServer

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


def _config(tmp_path):
    return Config(
        instance_name="Commons",
        plain_host="127.0.0.1",
        plain_port=0,
        ws_host="127.0.0.1",
        ws_port=0,
        cert_file=str(tmp_path / "nope-cert.pem"),
        key_file=str(tmp_path / "nope-key.pem"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        # Every store gets a tmp path — a default here resolves against the CWD,
        # which is the repo root and the operator's real data. The autouse guard
        # in conftest catches it, but not writing it in the first place is better.
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        resume_file=str(tmp_path / "resume.json"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )


@pytest.fixture
async def server(tmp_path):
    srv = TalkerServer(_config(tmp_path))
    await srv.start()
    plain_port = srv._servers[0].sockets[0].getsockname()[1]
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        yield srv, plain_port, ws_port
    finally:
        await srv.aclose()


# ---- socket helpers ------------------------------------------------------

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
    await sock_collect(reader)          # agreement text + "Do you agree?"
    await sock_send(writer, "y")
    return await sock_collect(reader, timeout=1.5)


# ---- websocket helpers ---------------------------------------------------

async def ws_collect(ws, timeout=0.4, keep_control=False):
    """Collect frames until a quiet gap. Control frames (\\x01…) are dropped
    unless keep_control is set."""
    frames = []
    while True:
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout)
        except asyncio.TimeoutError:
            break
        except Exception:
            break
        if isinstance(frame, (bytes, bytearray)):
            frame = frame.decode(errors="replace")
        if frame.startswith("\x01") and not keep_control:
            continue
        frames.append(frame)
    return frames


async def ws_text(ws, timeout=0.4):
    return "\n".join(await ws_collect(ws, timeout))


async def ws_signin(ws, name, pw="swordfish-x9"):
    """Log in to an account that ALREADY exists (ws_login creates one)."""
    await ws_collect(ws)
    await ws.send(name)
    await ws_collect(ws)
    await ws.send(pw)
    return await ws_text(ws, timeout=1.5)


async def ws_login(ws, name, pw="swordfish-x9"):
    await ws_collect(ws)
    await ws.send(name)
    await ws_collect(ws)
    await ws.send("yes")
    await ws_collect(ws)
    await ws.send(pw)
    await ws_collect(ws)
    await ws.send(pw)
    await ws_collect(ws)                # agreement text + "Do you agree?"
    await ws.send("y")
    # The web door asks once, at signup, whether to stay logged in (v1.24.0).
    # Default is no; these tests are not about remember-me, so: no.
    await ws_collect(ws)
    await ws.send("n")
    return await ws_text(ws, timeout=1.5)


async def ws_login_existing(ws, name, pw):
    await ws_collect(ws)     # greeting + name prompt
    await ws.send(name)
    await ws_collect(ws)     # password prompt
    await ws.send(pw)
    return await ws_text(ws)


def _norm_idle(text):
    # idle time and the .who header clock are inherently temporal; normalise them
    # out for parity comparison (two .who calls a second apart still match).
    text = re.sub(r"idle \d+:\d+", "idle", text)
    return re.sub(r"\*\*\* Current users on .+ \*\*\*", "*** Current users ***", text)


@pytest.mark.asyncio
async def test_ws_and_socket_see_identical_text(server):
    srv, plain_port, ws_port = server

    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login(ws, "Corvid")
        assert "atrium" in intro.lower()

        # A socket session joins the same room.
        r, w = await sock_connect(plain_port)
        sock_intro = await sock_login(r, w, "Wren")
        assert "atrium" in sock_intro.lower()

        # The browser session sees the arrival, same wording as any door.
        assert "[Wren enters the world]" in await ws_text(ws)

        # Socket speaks -> browser hears the exact same line.
        await sock_send(w, "the rain finally stopped.")
        assert "Wren says: the rain finally stopped." in await ws_text(ws)

        # Browser speaks -> socket hears the exact same line.
        await ws.send("hello from the browser.")
        assert "Corvid says: hello from the browser." in await sock_collect(r)

        # .go announces to the room the mover left (socket sees it).
        await ws.send(".go gallery")
        moved = await ws_text(ws)
        assert "gallery" in moved.lower()
        assert "slips out toward the gallery" in await sock_collect(r)

        # .tell crosses rooms and is private.
        await ws.send(".tell Wren still here?")
        assert ">> Corvid says: still here?" in await sock_collect(r)
        # Drain Corvid's own copy of the same line off the WS side.
        assert ">> Corvid says: still here?" in await ws_text(ws)

        # .who parity: both doors render the same text (idle field normalised).
        await ws.send(".who")
        ws_who = _norm_idle(await ws_text(ws))
        await sock_send(w, ".who")
        sock_who = _norm_idle(await sock_collect(r)).replace("\r\n", "\n")
        # Strip the socket's trailing prompt lines and blank noise for compare.
        ws_lines = [ln for ln in ws_who.split("\n") if ln.strip() and not ln.strip().startswith(">")]
        sock_lines = [ln for ln in sock_who.split("\n") if ln.strip() and not ln.strip().startswith(">")]
        assert ws_lines == sock_lines

        w.close()


@pytest.mark.asyncio
async def test_ws_password_control_frames(server):
    srv, plain_port, ws_port = server

    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_collect(ws, keep_control=True)  # greeting + name prompt
        await ws.send("Newcomer")
        await ws_collect(ws, keep_control=True)  # "Create it?"
        await ws.send("yes")

        # Around the "Choose a password:" prompt we must see echo off, and the
        # control frames are never room text (they carry the \x01 prefix).
        frames = await ws_collect(ws, keep_control=True)
        assert "\x01echo off" in frames
        # The visible password prompt is a separate, non-control frame.
        assert any("password" in f.lower() and not f.startswith("\x01") for f in frames)

        await ws.send("hunter28")
        after = await ws_collect(ws, keep_control=True)
        # Echo is restored after the secret read (either right after this read,
        # or bundled with the confirm prompt's echo-off that follows).
        assert "\x01echo on" in after

        # No frame ever both starts with the control prefix and carries words
        # that would render as room text.
        for f in frames + after:
            if f.startswith("\x01"):
                assert f in ("\x01echo off", "\x01echo on")


@pytest.mark.asyncio
async def test_ws_control_frames_never_shown_as_text(server):
    """The two defined control frames are the only \\x01 frames the server emits."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_login(ws, "Talker")
        await ws.send("just a normal line")
        # Speaking never produces control frames back to the speaker's room feed.
        frames = await ws_collect(ws, keep_control=True)
        assert all(not f.startswith("\x01") for f in frames)


@pytest.mark.asyncio
async def test_passwd_echo_off_control_frames(server):
    """.passwd prompts old + new + new, each with echo off/on around it."""
    srv, plain_port, ws_port = server

    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_login(ws, "Changer", pw="oldpass1")
        await ws.send(".passwd")

        f1 = await ws_collect(ws, keep_control=True)   # echo off + "Old password:"
        assert "\x01echo off" in f1
        await ws.send("oldpass1")
        f2 = await ws_collect(ws, keep_control=True)   # around "New password:"
        await ws.send("newpass1")
        f3 = await ws_collect(ws, keep_control=True)   # around "New password again:"
        await ws.send("newpass1")
        f4 = await ws_collect(ws, keep_control=True)

        allframes = f1 + f2 + f3 + f4
        assert allframes.count("\x01echo off") >= 3    # one per prompt
        assert allframes.count("\x01echo on") >= 3
        assert "Password changed." in "\n".join(allframes)

    # New password is accepted.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login_existing(ws, "Changer", "newpass1")
        assert "atrium" in intro.lower()

    # Old password is rejected.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login_existing(ws, "Changer", "oldpass1")
        assert "Wrong password." in intro


@pytest.mark.asyncio
async def test_login_mail_notice_only_at_login(server):
    srv, plain_port, ws_port = server

    # First login: no mail yet, so no notice; then mail yourself and quit.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login(ws, "Mailer", pw="mailpass")
        assert "You have mail." not in intro
        await ws.send(".mail Mailer a note left for later")
        assert "Mail sent to Mailer." in await ws_text(ws)
        # No unsolicited push arrives mid-session.
        assert "You have mail." not in await ws_text(ws, timeout=0.5)
        await ws.send(".quit")
        await ws_collect(ws)

    # Reconnect: the notice appears exactly at login.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login_existing(ws, "Mailer", "mailpass")
        assert "You have mail." in intro
        # Reading it works; no second notice pushed afterward.
        await ws.send(".mail")
        body = await ws_text(ws)
        assert "a note left for later" in body
        assert "You have mail." not in body


@pytest.mark.asyncio
async def test_login_notice_fires_on_unread_only(server):
    """v1.34.0: the notice used to fire on a non-empty box, so it kept saying
    'You have mail.' at every login until you deleted everything. It now fires
    only when something is unread — and reading is what clears it."""
    srv, plain_port, ws_port = server

    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_login(ws, "Unread", pw="unreadpass")
        await ws.send(".mail Unread first note")
        await ws_text(ws)
        await ws.send(".quit")
        await ws_collect(ws)

    # Unread -> the notice fires. Read it, and quit.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login_existing(ws, "Unread", "unreadpass")
        assert "You have mail." in intro
        await ws.send(".mail")
        assert "first note" in await ws_text(ws)
        await ws.send(".quit")
        await ws_collect(ws)

    # Read, and NOT deleted -> no notice. This is the bug this release fixes.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login_existing(ws, "Unread", "unreadpass")
        assert "You have mail." not in intro
        assert srv.mail.count("Unread") == 1        # still in the box, unshouted
        # New mail lands: unread again, so the notice comes back next login.
        await ws.send(".mail Unread second note")
        await ws_text(ws)
        await ws.send(".quit")
        await ws_collect(ws)

    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        intro = await ws_login_existing(ws, "Unread", "unreadpass")
        assert "You have mail." in intro


@pytest.mark.asyncio
async def test_pre_v2_mail_is_announced_after_the_upgrade(tmp_path):
    """v1.34.1, end to end and across a real restart: mail written before the
    read flag existed is migrated UNREAD, so the upgrade announces it once —
    and reading it is the end of it. Two servers on one state directory, with
    the file downgraded between them, is what an upgrade actually looks like."""
    import json

    from heresay.mail import MailStore

    # ---- the old world: a server that has never heard of the read flag.
    srv = TalkerServer(_config(tmp_path))
    await srv.start()
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
            await ws_login(ws, "Legacy", pw="legacypass")
            await ws.send(".mail Legacy a letter from before the flag")
            await ws_text(ws)
            await ws.send(".quit")
            await ws_collect(ws)
    finally:
        await srv.aclose()

    # ⚠ Since v1.52.5 the live store is one file per box, so "a genuine pre-v2
    # mailbox" means writing the OLD single-file shape back out and clearing the
    # split ones — which is exactly the state an upgrading house is in, and the
    # only state in which the pre-v2 reader still runs. This now exercises BOTH
    # migrations at once: the read flag and the split.
    import shutil

    from heresay import boxfiles

    box_dir = boxfiles.box_dir(tmp_path / "mailboxes.json")
    boxes = {key: [{"ts": m["ts"], "blob": m["blob"]}      # no `read` field
                   for m in data["messages"]]
             for key, data in boxfiles.read_boxes(box_dir)}
    shutil.rmtree(box_dir)
    (tmp_path / "mailboxes.json").write_text(
        json.dumps({"version": 1, "mailboxes": boxes}), encoding="utf-8")

    # ---- the upgrade: a new server loads it, migrating on the way in.
    srv = TalkerServer(_config(tmp_path))
    await srv.start()
    ws_port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
            intro = await ws_login_existing(ws, "Legacy", "legacypass")
            assert "You have mail." in intro          # announced once more
            await ws.send(".mail")
            assert "a letter from before the flag" in await ws_text(ws)
            await ws.send(".quit")
            await ws_collect(ws)

        # Reading it was the end of it — the mail is still in the box.
        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
            intro = await ws_login_existing(ws, "Legacy", "legacypass")
            assert "You have mail." not in intro
        assert srv.mail.count("Legacy") == 1
    finally:
        await srv.aclose()

    # And it stays clear over the NEXT restart: the flag is on disk, and NEITHER
    # migration can run twice — the record has a `read` field now, and the
    # legacy file has been renamed aside.
    assert MailStore(tmp_path / "mailboxes.json",
                     tmp_path / "mail.key").has_unread("Legacy") is False
    assert (tmp_path / "mailboxes.json.pre-split").exists()


# ---- the web client's command echo ---------------------------------------
#
# The echo itself is client-side JavaScript and is verified by hand (there is no
# JS harness here). What CAN be checked from Python is the thing that will
# actually rot: the client's NO_ECHO list drifting away from the server's
# conversation verbs, which would make a say or an emote print twice.

def test_no_echo_list_matches_the_servers_conversation_verbs():
    import re
    from pathlib import Path
    from heresay import commands

    conversation = {commands.cmd_emote, commands.cmd_think, commands.cmd_tell,
                    commands.cmd_remote, commands.cmd_shout}
    # The single-character prefix aliases (`;` `>` `<`) are registry aliases as
    # of v1.28.0, so they are in _COMMANDS — but they have no DOTTED form anyone
    # types, and a line beginning with `;` never reaches the client's echo path
    # (which only echoes lines starting with `.`). They are conversation either
    # way; excluding them keeps this comparing the two lists that actually face
    # each other.
    expected = {"." + verb for verb, fn in commands._COMMANDS.items()
                if fn in conversation and verb not in commands._PREFIX_ALIASES}

    page = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text()
    block = re.search(r"const NO_ECHO = \[(.*?)\];", page, re.S)
    assert block, "NO_ECHO list not found in web/index.html"
    listed = set(re.findall(r'"([^"]+)"', block.group(1)))
    assert listed == expected, f"client {listed} vs server {expected}"


# ---- the tab_alert control frame (1.19.0) --------------------------------
#
# The web client's background-tab mark is a browser thing, so the resident's
# `.ignore tab_alert` setting has to be PUSHED to it: once at login, so a fresh
# page starts correct, and again whenever they change it. The socket doors never
# see it — there is no tab there to mark, and a terminal would print the frame.

from heresay.transport import CTRL_TAB_ALERT


async def ws_controls(ws, timeout=0.6):
    """Just the control frames, in order."""
    frames = await ws_collect(ws, timeout, keep_control=True)
    return [f for f in frames if f.startswith("\x01")]


@pytest.mark.asyncio
async def test_web_login_is_told_the_tab_alert_setting(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        # The login flow's own frames include the echo on/off pair; the tab_alert
        # frame rides with them and says "on" for a resident who has set nothing.
        await ws_login(ws, "Corvid")
        acct = srv.accounts.get("Corvid")
        assert acct is not None
        await ws.send(".ignore tab_alert yes")
        assert CTRL_TAB_ALERT + "off" in await ws_controls(ws)
        await ws.send(".ignore tab_alert no")
        assert CTRL_TAB_ALERT + "on" in await ws_controls(ws)


@pytest.mark.asyncio
async def test_the_login_frame_matches_the_stored_setting(server):
    """The point of sending it at login: a page that opens after the resident
    opted out must not start by marking the tab."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_login(ws, "Corvid")
        await ws.send(".ignore tab_alert yes")
        await ws_collect(ws)
        await ws.send(".quit")
        await ws_collect(ws)

    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws2:
        await ws2.send("Corvid")
        await ws_collect(ws2)
        await ws2.send("swordfish-x9")
        controls = await ws_controls(ws2, timeout=1.5)
        assert CTRL_TAB_ALERT + "off" in controls, controls


@pytest.mark.asyncio
async def test_a_fresh_resident_is_told_on(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_collect(ws)
        await ws.send("Newbie")
        await ws_collect(ws)
        await ws.send("yes")
        await ws_collect(ws)
        await ws.send("swordfish-x9")
        await ws_collect(ws)
        await ws.send("swordfish-x9")
        await ws_collect(ws)
        await ws.send("y")
        await ws_collect(ws)            # the remember-me question (web door)
        await ws.send("n")
        controls = await ws_controls(ws, timeout=1.5)
        assert CTRL_TAB_ALERT + "on" in controls, controls


@pytest.mark.asyncio
async def test_a_socket_login_never_sees_the_frame(server):
    """Not merely filtered at the client — never sent. A terminal would print it."""
    srv, plain_port, ws_port = server
    r, w = await sock_connect(plain_port)
    try:
        out = await sock_login(r, w, "Corvid")
        await sock_send(w, ".ignore tab_alert yes")
        out += await sock_collect(r)
        assert "tab_alert: hidden." in out          # the command worked...
        assert "\x01" not in out                    # ...and no frame was sent
        assert "tab_alert on" not in out and "tab_alert off" not in out
    finally:
        w.close()


# ---- the house frames: raise bounds, title, favicon (1.22.0) --------------
#
# Three more things the WEB client cannot work out for itself, pushed on connect
# through the same \x01 path as tab_alert. A socket door gets none of them.

from heresay.transport import (CTRL_FAVICON, CTRL_INWORLD, CTRL_RAISE_BOUNDS,
                               CTRL_TITLE)


@pytest.mark.asyncio
async def test_a_web_connect_is_told_the_house_bounds_title_and_favicon(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_login(ws, "Corvid")
        await ws.send(".ignore tab_alert no")          # any push re-sends the set
        ctl = await ws_controls(ws)
        assert CTRL_RAISE_BOUNDS + "0 10" in ctl, ctl


@pytest.mark.asyncio
async def test_the_login_push_carries_all_five_frames(server):
    """One connect, one full picture: the resident's own setting, the three
    house-level ones, and the in-world signal that gates the tab mark."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        # ws_login creates the account; the four frames ride the arrival.
        await ws_collect(ws)
        await ws.send("Corvid"); await ws_collect(ws)
        await ws.send("yes"); await ws_collect(ws)
        await ws.send("swordfish-x9"); await ws_collect(ws)
        await ws.send("swordfish-x9"); await ws_collect(ws)
        await ws.send("y"); await ws_collect(ws)
        await ws.send("n")              # the remember-me question (web door)
        ctl = await ws_controls(ws, timeout=1.5)
        joined = "\n".join(ctl)
        assert CTRL_TAB_ALERT in joined, ctl
        assert CTRL_RAISE_BOUNDS in joined, ctl
        assert CTRL_TITLE + srv.theme.instance_name in ctl, ctl
        assert any(f.startswith(CTRL_FAVICON) for f in ctl), ctl
        assert CTRL_INWORLD + "on" in ctl, ctl


@pytest.mark.asyncio
async def test_the_inworld_frame_arrives_only_after_login(server):
    """The tab mark may blink only for a connected, in-world session, so the
    signal that unlocks it must not be sent at the greeting or the prompts."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws)
        # Through the greeting, the name prompt and the create question.
        await ws_until(ws, "Name (or 'quit')")
        await ws.send("Corvid")
        await ws_until(ws, "Create it?")
        early = await ws_controls(ws, timeout=0.4)
        assert not any(f.startswith(CTRL_INWORLD) for f in early), early
        # Now finish logging in.
        await ws.send("yes")
        await ws_until(ws, "Choose a password")
        await ws.send("swordfish-x9")
        await ws_until(ws, "Confirm password")
        await ws.send("swordfish-x9")
        await ws_until(ws, "Do you agree?")
        await ws.send("y")
        await ws_until(ws, "Stay logged in")
        await ws.send("n")
        ctl = await ws_controls(ws, timeout=1.5)
        assert CTRL_INWORLD + "on" in ctl, ctl


@pytest.mark.asyncio
async def test_a_resumed_session_is_told_it_is_in_world(server):
    """Straight in on a token still unlocks the mark — the resume path goes
    through the same _load_prefs seam as every other way into the world."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        ctl = await ws_controls(ws, timeout=1.5)
        assert CTRL_INWORLD + "on" in ctl, ctl


@pytest.mark.asyncio
async def test_a_socket_login_is_never_told_it_is_in_world(server):
    """Web-only, like every control frame: a terminal has no tab to mark."""
    srv, plain_port, ws_port = server
    r, w = await sock_connect(plain_port)
    out = await sock_login(r, w, "Corvid")
    assert CTRL_INWORLD not in out
    w.close()


@pytest.mark.asyncio
async def test_the_favicon_frame_is_a_data_url_not_a_path(server):
    """Zero external requests: the browser is handed the bytes, never a URL it
    would have to fetch."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_collect(ws)
        await ws.send("Corvid"); await ws_collect(ws)
        await ws.send("yes"); await ws_collect(ws)
        await ws.send("swordfish-x9"); await ws_collect(ws)
        await ws.send("swordfish-x9"); await ws_collect(ws)
        await ws.send("y"); await ws_collect(ws)
        await ws.send("n")              # the remember-me question (web door)
        ctl = await ws_controls(ws, timeout=1.5)
        frame = next(f for f in ctl if f.startswith(CTRL_FAVICON))
        payload = frame[len(CTRL_FAVICON):]
        assert payload.startswith("data:"), payload[:40]
        assert "base64," in payload
        assert "http" not in payload


@pytest.mark.asyncio
async def test_a_socket_login_gets_none_of_the_house_frames(server):
    srv, plain_port, ws_port = server
    r, w = await sock_connect(plain_port)
    try:
        out = await sock_login(r, w, "Corvid")
        assert "\x01" not in out
        for token in ("raise_bounds", "favicon", "title Commons"):
            assert token not in out, token
    finally:
        w.close()


@pytest.mark.asyncio
async def test_changing_the_bounds_pushes_to_every_web_session(tmp_path):
    """House-wide, so it reaches everyone — unlike .ignore, which is one
    person's own setting and reaches only them.

    Its own server, not the shared fixture: this one WRITES state (the setting
    persists), so state_file has to be a tmp path.
    """
    cfg = Config(
        instance_name="Commons",
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        owner_name="Boss", max_conns_per_ip=20, churn_max_in_window=50,
    )
    # The arch NAME may only be registered from the local console, so seed it.
    from heresay.accounts import AccountStore
    from conftest import seed_account
    store = AccountStore(tmp_path / "accounts.json")
    seed_account(store, "Boss", "swordfish-x9")
    store.flush()

    srv = TalkerServer(cfg)
    await srv.start()
    port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        async with ws_connect(f"ws://127.0.0.1:{port}") as boss, \
                   ws_connect(f"ws://127.0.0.1:{port}") as other:
            await ws_signin(boss, "Boss")
            await ws_login(other, "Corvid")
            await ws_collect(boss, timeout=1.0)      # drain the arrival frames
            await ws_collect(other, timeout=1.0)
            await boss.send(".ownerset raise_prompt_max 4")
            assert CTRL_RAISE_BOUNDS + "0 4" in await ws_controls(boss, timeout=1.2)
            assert CTRL_RAISE_BOUNDS + "0 4" in await ws_controls(other, timeout=1.2)
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_bounds_are_validated(tmp_path):
    """min <= max, non-negative, whole numbers — a house whose floor is above
    its ceiling has no legal setting at all."""
    cfg = Config(
        instance_name="Commons",
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        owner_name="Boss", max_conns_per_ip=20, churn_max_in_window=50,
    )
    from heresay.accounts import AccountStore
    from conftest import seed_account
    store = AccountStore(tmp_path / "accounts.json")
    seed_account(store, "Boss", "swordfish-x9")
    store.flush()

    srv = TalkerServer(cfg)
    await srv.start()
    port = srv._ws_server.sockets[0].getsockname()[1]
    try:
        async with ws_connect(f"ws://127.0.0.1:{port}") as ws:
            await ws_signin(ws, "Boss")
            await ws_collect(ws, timeout=1.0)
            for bad, needle in ((".ownerset raise_prompt_max lots",
                                 "whole number"),
                                (".ownerset raise_prompt_min -2", "negative"),
                                (".ownerset raise_prompt_min 99", "cannot exceed")):
                await ws.send(bad)
                out = "\n".join(await ws_collect(ws, timeout=1.0))
                assert needle in out, (bad, out)
            # ...and nothing was stored by any of them.
            assert srv.state.house_setting("raise_prompt_min") == "0"
            assert srv.state.house_setting("raise_prompt_max") == "10"
            # A legal pair IS stored, and persists.
            await ws.send(".ownerset raise_prompt_min 2")
            await ws_collect(ws, timeout=1.0)
            assert srv.state.house_setting("raise_prompt_min") == "2"
            from heresay.state import StateStore
            assert StateStore(tmp_path / "state.json").house_setting(
                "raise_prompt_min") == "2"
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# Remember-me: the resume handshake, end to end over a real WebSocket
# ---------------------------------------------------------------------------

CTRL_RESUME = "\x01resume"


async def ws_until(ws, needle, timeout=3.0):
    """Collect until ``needle`` appears, rather than until a quiet gap.

    The signup flow hashes a password with argon2 between two of its prompts, so
    a fixed quiet-gap collect can return before the next question has even been
    asked — which under load makes an answer land on the wrong prompt. Waiting
    for the text we are answering is deterministic.
    """
    seen = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            frame = await asyncio.wait_for(ws.recv(), deadline - loop.time())
        except (asyncio.TimeoutError, Exception):
            break
        if isinstance(frame, (bytes, bytearray)):
            frame = frame.decode(errors="replace")
        seen.append(frame)
        if needle in frame:
            return seen
    raise AssertionError(f"never saw {needle!r}; got {seen!r}")


async def ws_hello(ws, token=""):
    """What the real client sends the instant the socket opens: width, then the
    resume frame — ALWAYS, empty when it has no token, because that frame is how
    the server knows the hello is over and it can stop waiting."""
    await ws.send("\x01width 100")
    await ws.send(CTRL_RESUME + (f" {token}" if token else ""))


def resume_token(controls):
    """The token out of a `\\x01resume <token>` push, or None. An EMPTY push (a
    revoke) deliberately returns None: there is no token in it."""
    for f in controls:
        if f.startswith(CTRL_RESUME + " "):
            return f[len(CTRL_RESUME) + 1:].strip()
    return None


async def ws_signup(ws, name, remember, pw="swordfish-x9"):
    """Create an account, answering the remember-me question with ``remember``.

    Each answer waits for the question it answers (see ws_until) rather than for
    a quiet gap, so the sequence cannot drift under load.
    """
    await ws_hello(ws)
    await ws_until(ws, "Name (or 'quit')")
    await ws.send(name)
    await ws_until(ws, "Create it?")
    await ws.send("yes")
    await ws_until(ws, "Choose a password")
    await ws.send(pw)
    await ws_until(ws, "Confirm password")
    await ws.send(pw)
    await ws_until(ws, "Do you agree?")
    await ws.send("y")
    await ws_until(ws, "Stay logged in")
    await ws.send(remember)
    return await ws_controls(ws, timeout=1.5)


async def ws_create_remembering(ws, name, pw="swordfish-x9"):
    """Create an account and answer YES to the remember-me question."""
    return await ws_signup(ws, name, "y", pw)


@pytest.mark.asyncio
async def test_a_remembered_signup_is_pushed_a_token(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        ctl = await ws_create_remembering(ws, "Corvid")
        assert resume_token(ctl), ctl
        assert srv.resume.count_for("Corvid") == 1


@pytest.mark.asyncio
async def test_declining_at_signup_gets_no_token(server):
    """Off by default, and "default" means the answer you get by saying nothing."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        ctl = await ws_signup(ws, "Corvid", "")    # just press enter
        assert resume_token(ctl) is None, ctl
        assert srv.resume.count_for("Corvid") == 0


@pytest.mark.asyncio
async def test_a_token_logs_you_straight_in_with_no_prompts(server):
    """The whole feature: no banner, no name, no password."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        text = await ws_text(ws, timeout=1.5)
        assert "Password" not in text, text
        assert "Name (or 'quit')" not in text, text
        assert "atrium" in text.lower(), text     # straight into the world


@pytest.mark.asyncio
async def test_a_used_token_is_rotated_and_the_old_one_dies(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        first = resume_token(await ws_create_remembering(ws, "Corvid"))
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, first)
        second = resume_token(await ws_controls(ws, timeout=1.5))
    assert second and second != first
    assert srv.resume.resolve(first) is None, "a leaked token dies at next use"
    assert srv.resume.resolve(second) == "corvid"


@pytest.mark.asyncio
async def test_a_rejected_token_falls_back_to_the_login_and_is_cleared(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, "a-token-that-was-never-issued")
        frames = await ws_collect(ws, timeout=1.5, keep_control=True)
        text = "\n".join(f for f in frames if not f.startswith("\x01"))
        assert "Name (or 'quit')" in text, text        # the ordinary door
        assert CTRL_RESUME in frames, frames           # empty push: forget it


@pytest.mark.asyncio
async def test_a_revoked_token_no_longer_works(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    srv.resume.revoke_account("Corvid")               # as .quit would
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        text = await ws_text(ws, timeout=1.5)
        assert "Name (or 'quit')" in text, text


@pytest.mark.asyncio
async def test_a_password_change_logs_every_device_out(server):
    """The revocation hook, proved through the real server rather than the store."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    await srv.accounts.achange_password("Corvid", "a-different-passphrase")
    assert srv.resume.resolve(token) is None
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        assert "Name (or 'quit')" in await ws_text(ws, timeout=1.5)


@pytest.mark.asyncio
async def test_a_socket_login_is_never_issued_a_token(server):
    """Web-only. The telnet door cannot present one and is never given one."""
    srv, plain_port, ws_port = server
    r, w = await sock_connect(plain_port)
    await sock_login(r, w, "Corvid")
    srv.accounts.set_setting("Corvid", "remember", True)   # even if it is ON
    w.close()
    r2, w2 = await sock_connect(plain_port)
    out = await sock_login_existing(r2, w2, "Corvid", "swordfish-x9")
    # NB: a bare \x01 does appear on this door — it is the ECHO option byte of
    # the telnet IAC negotiation around the password prompt. What must never
    # appear is a control FRAME.
    assert CTRL_RESUME not in out, "a terminal must never be sent a resume frame"
    assert srv.resume.count_for("Corvid") == 0
    w2.close()


async def sock_login_existing(reader, writer, name, pw):
    await sock_collect(reader)
    await sock_send(writer, name)
    await sock_collect(reader)
    await sock_send(writer, pw)
    return await sock_collect(reader, timeout=1.5)


@pytest.mark.asyncio
async def test_private_mode_closes_the_resume_door_too(server):
    """A 30-day token resume is a LOGIN, not a grace reattach — so `.logins off`
    has to close it, or the switch does not actually close the house."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    srv.state.set_logins(False)
    try:
        async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
            await ws_hello(ws, token)
            text = await ws_text(ws, timeout=1.5)
            assert "closed just now" in text, text
    finally:
        srv.state.set_logins(True)


@pytest.mark.asyncio
async def test_a_resumed_session_still_needs_the_password_to_change_it(server):
    """A found phone with a live token must not lock the owner out."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        await ws_collect(ws, timeout=1.5)
        await ws.send(".passwd")
        assert "Old password" in await ws_text(ws)     # still asked, always
        await ws.send("not-the-password")
        assert "Wrong password" in await ws_text(ws)


@pytest.mark.asyncio
async def test_quit_signs_out_every_device(server):
    """`.quit` is a full ACCOUNT logout, not a disconnect: the device that typed
    it AND every other remembered one."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        first = resume_token(await ws_create_remembering(ws, "Corvid"))
    # A second remembered device (a laptop, say), signed in with the password.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws2:
        await ws_hello(ws2)
        await ws_until(ws2, "Name (or 'quit')")
        await ws2.send("Corvid")
        await ws_until(ws2, "Password")
        await ws2.send("swordfish-x9")
        second = resume_token(await ws_controls(ws2, timeout=1.5))
    assert second and second != first

    # Now resume on the first device and quit from it.
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, first)
        rotated = resume_token(await ws_controls(ws, timeout=1.5))
        assert rotated, "the resume should have rotated the token"
        await ws.send(".quit")
        await ws_collect(ws, timeout=1.0)

    assert srv.resume.count_for("Corvid") == 0, \
        "a quit signs out EVERY device, not just the one that typed it"
    assert srv.resume.resolve(rotated) is None
    assert srv.resume.resolve(second) is None


@pytest.mark.asyncio
async def test_a_quit_cannot_be_resumed_afterward(server):
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
        await ws.send(".quit")
        await ws_collect(ws, timeout=1.0)
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        assert "Name (or 'quit')" in await ws_text(ws, timeout=1.5)


@pytest.mark.asyncio
async def test_the_remember_setting_survives_a_quit(server):
    """Quitting logs you out; it does not stop remembering you. So the next
    password login issues a fresh token and you are remembered again — only
    `.remember off` ends that."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_create_remembering(ws, "Corvid")
        await ws.send(".quit")
        await ws_collect(ws, timeout=1.0)
    assert srv.accounts.get("Corvid").settings.get("remember") is True
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws)
        await ws_until(ws, "Name (or 'quit')")
        await ws.send("Corvid")
        await ws_until(ws, "Password")
        await ws.send("swordfish-x9")
        assert resume_token(await ws_controls(ws, timeout=1.5)), \
            "still remembered: the login should issue a fresh token"


@pytest.mark.asyncio
async def test_a_boot_does_not_sign_out_other_devices(server):
    """Only a DELIBERATE .quit revokes. Being disconnected by someone else — or
    by a dropped socket — is not you asking to be logged out everywhere."""
    srv, plain_port, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Corvid"))
    # The socket simply goes away (a closed tab, a dead connection).
    assert srv.resume.resolve(token) == "corvid", \
        "a dropped connection must not revoke — that is what the tab-close case IS"


# ---- a DEAD resume token: rate-limited, and the browser told to drop it ----
# §6c's bullet reads "a dead/rotated resume token → refused, rate-limited like
# any bad credential, and the browser told to drop it". The REFUSAL half was
# already covered (test_a_quit_cannot_be_resumed_afterward here, and the store
# tests in test_resume_binding.py). ⛔ The other two halves were implemented on
# nine call sites in server.py and asserted NOWHERE until v1.62.36.
#
# ⚠ Every case below drives the DOOR. A test that called the function would not
# prove the wiring, which is the part that was untested.

async def _dead_token(ws_port, name="Corvid"):
    """Create a remembering account, take its token, quit — the token is dead."""
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, name))
        await ws.send(".quit")
        await ws_collect(ws, timeout=1.0)
    return token


async def _resume_with(ws_port, token):
    """Present a token at the door; return the control frames it pushed back."""
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        await ws_hello(ws, token)
        return await ws_collect(ws, timeout=1.5, keep_control=True)


@pytest.mark.asyncio
async def test_a_dead_resume_token_is_RATE_LIMITED_like_any_bad_credential(server):
    """⛔ The same auth-failure funnel a wrong password goes through.

    ⚠ Asserted as the OBSERVABLE CONSEQUENCE — a backoff delay now stands for
    that address — not as "a method was called". A mock-call assertion passes
    when the behaviour is broken, which is the failure mode this test exists to
    rule out. ⚡ It is deliberately the same assertion
    `test_takeover.py::test_wrong_password_refused_and_counts_backoff` makes for
    a wrong password: same funnel, same evidence.
    """
    srv, _, ws_port = server
    token = await _dead_token(ws_port)
    assert srv.auth_backoff.delay("127.0.0.1") == 0.0     # nothing owed yet
    await _resume_with(ws_port, token)
    assert srv.auth_backoff.delay("127.0.0.1") > 0, \
        "a dead resume token did not feed the auth-failure funnel"


@pytest.mark.asyncio
async def test_a_dead_resume_token_tells_the_BROWSER_TO_DROP_it(server):
    """⛔ The empty `\\x01resume` push — what arrives on the wire, not a call.

    ⚠ The bare frame with no token IS the instruction to forget: `resume_token`
    deliberately returns None for it, so the assertion is on the frame itself.
    Without it the browser keeps presenting a dead credential every reconnect.
    """
    srv, _, ws_port = server
    token = await _dead_token(ws_port)
    controls = await _resume_with(ws_port, token)
    assert CTRL_RESUME in controls, controls
    assert resume_token(controls) is None                 # nothing to store


@pytest.mark.asyncio
async def test_an_account_deleted_under_its_token_drops_AND_rate_limits(server):
    """A structurally different failure from a dead token: the token resolves,
    the account behind it does not. ⛔ Both halves must still happen — it is a
    credential that did not work, like any other."""
    srv, _, ws_port = server
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        token = resume_token(await ws_create_remembering(ws, "Wren"))
        await ws.send(".quit")
        await ws_collect(ws, timeout=1.0)
    srv.accounts.delete("Wren")                # the account goes; the token lives
    controls = await _resume_with(ws_port, token)
    assert CTRL_RESUME in controls, controls
    assert srv.auth_backoff.delay("127.0.0.1") > 0


async def _live_token(ws_port, name):
    """A token that is still VALID: connect, take it, and close the socket.

    ⛔ NOT `.quit` — a deliberate logout revokes every token the account has
    (`server.py`, "the one place a deliberate logout revokes"), which would send
    the next presentation down the unknown-token path instead of the one under
    test. ⚠ That mistake made an earlier draft of these cases pass for the wrong
    reason; the arrangement is the assertion here.
    """
    async with ws_connect(f"ws://127.0.0.1:{ws_port}") as ws:
        return resume_token(await ws_create_remembering(ws, name))


@pytest.mark.asyncio
async def test_private_mode_drops_the_token_but_is_NOT_an_auth_failure(server):
    """⚠ THE PATHS ARE NOT CO-EXTENSIVE, and this is the case that proves it.

    Three of the six drop sites also record an auth failure; three do not. This
    is one of the three that must NOT: the credential is GOOD and the house is
    simply closed. ⛔ Rate-limiting it would punish a resident for the operator
    flipping a switch — and `.logins off` closes the resume door precisely
    because a 30-day token is a LOGIN, not a grace reattach.

    ⚠ `allow_logins` is set on the server directly: that ARRANGES the scenario;
    the behaviour under test is still driven at the door.
    """
    srv, _, ws_port = server
    token = await _live_token(ws_port, "Finch")
    srv.state.allow_logins = False
    controls = await _resume_with(ws_port, token)
    assert CTRL_RESUME in controls, controls
    assert resume_token(controls) is None
    assert srv.auth_backoff.delay("127.0.0.1") == 0.0, \
        "a closed house was treated as a bad credential"


@pytest.mark.asyncio
async def test_a_ban_after_the_token_was_issued_drops_it_without_rate_limiting(server):
    """Refused AFTER the token authenticated — the ban stops `_enter`, and the
    freshly rotated token is revoked rather than left live for an account that
    cannot get in. ⛔ Still not an auth failure: the credential worked."""
    srv, _, ws_port = server
    token = await _live_token(ws_port, "Rook")
    srv.bans.ban("Rook", "name", 24.0, reason="test")
    controls = await _resume_with(ws_port, token)
    assert CTRL_RESUME in controls, controls
    assert srv.auth_backoff.delay("127.0.0.1") == 0.0


def test_EVERY_resume_failure_path_drops_the_token_and_the_right_ones_rate_limit():
    """⛔ THE SWEEP, because five door cases do not prove a sixth path exists.

    ⚠ A test that drives one path and reads as covering the behaviour is the
    shape that let a dead CVE scan sit "confirmed" for weeks. This reads the
    source and pins the counts, so a path added later that forgets the empty
    push — or one that quietly starts rate-limiting a good credential — fails
    here even though every case above still passes.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "heresay" / "server.py"
           ).read_text(encoding="utf-8")
    body = src.split("async def _try_resume", 1)[1].split("\n    async def ", 1)[0]
    assert body.count('send_control_resume("")') == 6, \
        "the resume path's drop sites moved — recount and update the door cases"
    assert body.count('_record_auth_failure(ip, "resume")') == 3, \
        "the resume path's auth-failure sites moved — recount"
    # ⛔ Every failure site drops the token too: a recorded failure that left the
    # browser holding the dead credential would re-fail on every reconnect.
    assert body.count('_record_auth_failure(ip, "resume")\n            session.send_control_resume("")') == 3
