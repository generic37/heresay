# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""greeting.txt — the operator-editable login banner.

Printed verbatim to every new connection before the name prompt. A missing file
falls back to the built-in two-line banner (never fatal, unlike agreement.txt).
The koan line ("You had to be there.") now lives inside the shipped file.
"""

import asyncio
from pathlib import Path

import pytest

import heresay
from heresay.config import Config
from heresay.server import BUILTIN_GREETING, TalkerServer, _load_greeting

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"
GREETING = REPO / "tests" / "fixtures" / "house" / "greeting.txt"
NARROW_GREETING = REPO / "tests" / "fixtures" / "house" / "greeting.narrow.txt"

# The login screen's version line — read from the constant, never from a file.
VERSION_LINE = f"Heresay version {heresay.__version__}"

# Distinctive lines, with their exact indentation — the whole point is that the
# art survives byte for byte, leading spaces included.
WORDMARK_TOP = "      _   _  _____  ____   _____  ____    _    __   __"
KOAN_LINE = "          What's said here stays here."
TAGLINE = "You had to be there."
KOAN = "You had to be there."


# ---- the loader -----------------------------------------------------------

def test_shipped_greeting_is_read_verbatim():
    text = _load_greeting(GREETING)
    lines = text.split("\n")
    assert lines[0] == WORDMARK_TOP          # the leading space is preserved
    assert KOAN_LINE in lines
    assert lines[-1] == KOAN                  # the koan is the last line of the file
    assert text == GREETING.read_text(encoding="utf-8").rstrip("\n")


def test_missing_file_falls_back_to_the_builtin(tmp_path):
    assert _load_greeting(tmp_path / "no-such-greeting.txt") == BUILTIN_GREETING
    assert BUILTIN_GREETING == "    . . . Heresay . . .\nYou had to be there."


# ---- over a real socket ---------------------------------------------------

async def _collect(reader, timeout=0.5):
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


def _blank_between(lines, last_banner_line, prompt_line):
    """Count the blank lines between the banner's last line and the prompt."""
    i = lines.index(last_banner_line)
    j = lines.index(prompt_line, i + 1)
    return sum(1 for ln in lines[i + 1:j] if ln == "")


def _cfg(tmp_path, **over):
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
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )
    base.update(over)
    return Config(**base)


async def _serve(cfg):
    srv = TalkerServer(cfg)
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_greeting_is_sent_verbatim_at_connect_then_the_prompt(tmp_path):
    # greeting_file explicit so the test does not depend on the CWD.
    srv, port = await _serve(_cfg(tmp_path, greeting_file=str(GREETING)))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        intro = await _collect(r)
    finally:
        await srv.aclose()
        w.close()

    lines = intro.replace("\r\n", "\n").split("\n")
    # Each distinctive line arrives with its exact indentation intact...
    assert WORDMARK_TOP in lines, "the wordmark's first line, leading space and all"
    assert KOAN_LINE in lines
    assert TAGLINE in lines
    assert KOAN in lines
    # ...and the name prompt follows, with exactly ONE blank line of breathing
    # room between the banner and it.
    assert "Name (or 'quit'):" in intro
    assert intro.index(KOAN) < intro.index("Name (or 'quit'):")
    # The version line rides the banner's own unit, so it sits DIRECTLY under the
    # koan with no blank; the one blank of breathing room is the prompt's.
    assert lines[lines.index(KOAN) + 1] == VERSION_LINE
    assert _blank_between(lines, VERSION_LINE, "Name (or 'quit'): ") == 1
    assert _blank_between(lines, KOAN, VERSION_LINE) == 0
    # Nothing injected: the built-in banner's text is gone, the file's is shown.
    assert ". . . Heresay . . ." not in intro


@pytest.mark.asyncio
async def test_a_custom_greeting_file_is_shown_as_written(tmp_path):
    custom = tmp_path / "greeting.txt"
    custom.write_text("  HELLO, TRAVELLER\nmind the gap\n", encoding="utf-8")
    srv, port = await _serve(_cfg(tmp_path, greeting_file=str(custom)))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        intro = await _collect(r)
    finally:
        await srv.aclose()
        w.close()
    lines = intro.replace("\r\n", "\n").split("\n")
    assert "  HELLO, TRAVELLER" in lines        # leading spaces kept
    assert "mind the gap" in lines
    assert "Name (or 'quit'):" in intro
    # The version follows whatever banner the house wrote, never inside it.
    assert lines[lines.index("mind the gap") + 1] == VERSION_LINE


@pytest.mark.asyncio
async def test_missing_greeting_shows_the_builtin_banner_over_the_wire(tmp_path):
    srv, port = await _serve(_cfg(tmp_path, greeting_file=str(tmp_path / "absent.txt")))
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        intro = await _collect(r)
    finally:
        await srv.aclose()
        w.close()
    assert ". . . Heresay . . ." in intro
    assert "You had to be there." in intro
    assert "Name (or 'quit'):" in intro
    # The breathing-room blank is unconditional — the fallback banner gets it too,
    # and so does the version line, which belongs to the login screen rather than
    # to any one greeting file.
    lines = intro.replace("\r\n", "\n").split("\n")
    assert lines[lines.index(KOAN) + 1] == VERSION_LINE
    assert _blank_between(lines, VERSION_LINE, "Name (or 'quit'): ") == 1


# ---- ships with the deploy ------------------------------------------------

# ⚠ This one asserts a property of the DEPLOY SCRIPTS, so it is an engine test
# living in a file the instance tree also reclaims (for the wordmark's
# typography, which is art). The instance tree has `deploy-content.sh` and no
# `deploy.sh` — nothing there could satisfy the code-deploy half — so it skips
# rather than failing. ⛔ A skip says "not applicable here"; a failure would say
# "the deploy is broken", which would be a lie in that tree.
@pytest.mark.skipif(not (REPO / "deploy" / "deploy.sh").is_file(),
                    reason="engine-only: no deploy.sh in the instance tree")
def test_greeting_ships_by_deploy_CONTENT_not_by_the_code_deploy():
    """⚠ THIS TEST REVERSED IN v1.59.0, and the reversal is the split.

    It used to assert the greeting rides the CODE rsync, which was true while the
    engine and the house shared a tree. They do not any more: `deploy.sh` now
    EXCLUDES `house/` and PROTECTS the server's copy, because the engine tree
    ships a SKELETON house and a code deploy carrying it would replace a live
    one. The greeting is content, and content reaches the server by
    `deploy-content.sh` — which rsyncs its own repo's `house/`, whichever repo
    that is.

    ⚠ The old assertion is kept in spirit: no state-exclude may sweep the
    greeting out of the CONTENT push either, which is the failure the original
    was guarding against.
    """
    code = (REPO / "deploy" / "deploy.sh").read_text()
    assert "--exclude '/house/'" in code
    assert "--filter='protect /house/'" in code

    content = (REPO / "deploy" / "deploy-content.sh").read_text()
    excludes = [ln for ln in content.split("\n") if "--exclude" in ln]
    assert not any("greeting" in ln for ln in excludes)
    assert '"$REPO/house/"' in content
    assert GREETING.is_file()


# ---- the login screen's version line (v1.26.1) -----------------------------
#
# "What am I connected to?" is the one question a login screen can answer for
# free, and the answer must never be able to go stale — so it is read from the
# constant at every login and is deliberately NOT written into greeting.txt.


def test_the_login_banner_appends_the_current_version(tmp_path):
    """`login_banner_for` is the seam: the greeting, then the version."""
    srv = TalkerServer(_cfg(tmp_path, greeting_file=str(GREETING)))
    wide = _FakeWidth(is_narrow=False)
    banner = srv.login_banner_for(wide)
    assert banner == f"{srv.greeting_for(wide)}\n{VERSION_LINE}"
    assert banner.split("\n")[-1] == VERSION_LINE
    assert banner.split("\n")[-2] == KOAN      # directly under it, no blank


def test_the_version_line_tracks_the_constant(tmp_path, monkeypatch):
    """Bump the constant and the line moves with it — the whole point of not
    hardcoding it in the greeting file."""
    import heresay.server as server_mod
    monkeypatch.setattr(server_mod, "__version__", "9.8.7")
    srv = TalkerServer(_cfg(tmp_path, greeting_file=str(GREETING)))
    assert srv.login_banner_for(_FakeWidth(False)).endswith("\nHeresay version 9.8.7")


def test_the_version_line_names_the_engine_not_the_house(tmp_path):
    """No instance name: the art above has already said whose house this is."""
    srv = TalkerServer(_cfg(tmp_path, greeting_file=str(GREETING)))
    assert VERSION_LINE == f"Heresay version {heresay.__version__}"
    assert "Commons" not in VERSION_LINE
    assert srv.login_banner_for(_FakeWidth(False)).count(VERSION_LINE) == 1


def test_both_widths_get_the_version_line(tmp_path):
    """Wide banner and narrow banner alike — it belongs to the login screen."""
    srv = TalkerServer(_cfg(tmp_path, greeting_file=str(GREETING),
                            house_dir=str(REPO / "tests" / "fixtures" / "house")))
    for narrow in (False, True):
        banner = srv.login_banner_for(_FakeWidth(narrow))
        assert banner.split("\n")[-1] == VERSION_LINE
    # ...and the two banners really are different art, so this is two paths.
    assert (srv.login_banner_for(_FakeWidth(True))
            != srv.login_banner_for(_FakeWidth(False)))


class _FakeWidth:
    """The only thing `greeting_for` asks a session for."""

    def __init__(self, is_narrow):
        self.is_narrow = is_narrow


# ---- the narrow wordmark (v1.26.1) -----------------------------------------
#
# The pipeline is verbatim — send_raw does not wrap, _emit_unit drops only blank
# LINES, strip_controls keeps spaces, and the web client sets textContent in a
# `white-space: pre-wrap` box. So the art's alignment is the FILE's business, and
# this is where it is pinned.

def _art_rows(text, first, count):
    return text.split("\n")[first:first + count]


def test_the_narrow_greeting_fits_forty_columns():
    text = NARROW_GREETING.read_text(encoding="utf-8").rstrip("\n")
    for line in text.split("\n"):
        assert len(line) <= 40, repr(line)


def test_the_wordmark_caps_sit_over_their_letter_bodies():
    """The bug: each word's top row sat one column right of its body, so every
    cap overhung the letter under it. Checked per letter, not per word."""
    text = NARROW_GREETING.read_text(encoding="utf-8").rstrip("\n")
    wonder = _art_rows(text, 0, 4)
    land = _art_rows(text, 4, 4)

    # W: the top bar starts exactly where the left stroke starts.
    assert wonder[0].index("__") == wonder[1].index("\\")
    # L: its single cap sits one column right of the stem, as the glyph is drawn.
    assert land[0].index("_") == land[1].index("|") + 1

    # And every cap-row run of underscores lies within its own word's body.
    for rows in (wonder, land):
        body_left = min(len(r) - len(r.lstrip()) for r in rows[1:])
        body_right = max(len(r.rstrip()) for r in rows[1:])
        cap = rows[0]
        assert len(cap) - len(cap.lstrip()) >= body_left, cap
        assert len(cap.rstrip()) <= body_right, cap


def test_the_two_words_stay_centred_at_forty():
    text = NARROW_GREETING.read_text(encoding="utf-8").rstrip("\n")
    for rows in (_art_rows(text, 0, 4), _art_rows(text, 4, 4)):
        left = min(len(r) - len(r.lstrip()) for r in rows)
        right = 40 - max(len(r.rstrip()) for r in rows)
        assert abs(left - right) <= 1, (left, right, rows)


def test_the_bunny_and_the_tagline_were_not_touched():
    """Only the two cap rows moved."""
    text = NARROW_GREETING.read_text(encoding="utf-8").rstrip("\n")
    assert "  What's said here stays here." in text.split("\n")
    assert text.split("\n")[-1] == KOAN


# ---------------------------------------------------------------------
# Removed from the generic suite: each of these pins the authored ART of
# one particular house — the exact bytes of a map, a mirror-written room,
# a file of atmospheric lines. Another house's art is not a translation
# of that one, so there is nothing to rewrite them into. Every test of
# the MECHANISM around them is kept. (tools/build-generic.py)
del test_the_wordmark_caps_sit_over_their_letter_bodies
del test_the_two_words_stay_centred_at_forty
