# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A bare command should SHOW, not surprise (v1.31.0).

Two commands answered a bare invocation badly. `.desc` **cleared** your
description — one stray line and your entry was blank, with nothing asked and
nothing to undo it. `.examine` answered "Examine whom?", a question nobody was
asking: the interesting thing about your own entry is what other people see when
they look you up, and there was no way to check.

Both now show. `.desc` additionally offers to clear — and only what you wrote
yourself, because there is nothing personal to clear about a house default.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.commands import DESC_CONFIRM, clear_description, dispatch
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import StateStore

from conftest import FakeSession, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw-corvid", description="staff of crows")
    store.create("Wizzy", "pw-wizzy", description="a staff of the peace")
    return store


def _session(world, clock, accounts, name="Corvid", state=None, **kw):
    """A session whose resident mirrors the stored account, the way a real
    login leaves it (`_load_prefs` copies the description across)."""
    s = FakeSession(world, name=name, clock=clock, accounts=accounts,
                    config=Config(wrap_columns=1000), state=state, **kw)
    acct = accounts.get(name)
    if acct is not None:
        s.resident.description = acct.description
    return s


def _house_desc(tmp_path, text):
    """A StateStore with a house-default description set, the way
    `.ownerset desc` leaves one."""
    st = StateStore(tmp_path / "state.json")
    st.set_house_setting("desc", text)
    return st


def _say(session, line):
    session.clear()
    result = dispatch(session, line)
    return session.text, result


# ---------------------------------------------------------------------------
# Part 1 — bare .examine examines you
# ---------------------------------------------------------------------------

def test_bare_examine_shows_your_own_entry(world, clock, accounts):
    s = _session(world, clock, accounts)
    out, _ = _say(s, ".examine")
    assert "Examining Corvid" in out
    assert "staff of crows" in out
    assert "Examine whom?" not in out           # the old answer is gone


@pytest.mark.parametrize("verb", [".examine", ".ex", ".exa"])
def test_every_alias_examines_you_when_bare(world, clock, accounts, verb):
    """The bare form belongs to the command, not to one spelling of it."""
    s = _session(world, clock, accounts)
    out, _ = _say(s, verb)
    assert "Examining Corvid" in out


def test_bare_examine_is_the_same_screen_someone_else_gets(world, clock, accounts):
    """THE point: it is a preview, so it must go through the same path. A
    look-alike built separately would be a preview of the wrong thing."""
    me = _session(world, clock, accounts, name="Corvid")
    other = _session(world, clock, accounts, name="Wren")

    mine, _ = _say(me, ".examine")
    theirs, _ = _say(other, ".examine Corvid")
    assert mine == theirs


def test_your_own_marking_shows_in_your_own_examine(world, clock, accounts):
    """Rank is the one thing `.examine` marks, and it marks it for you too —
    otherwise the preview would hide the part you would most want to check."""
    accounts.set_rank("Wizzy", "staff")
    wiz = _session(world, clock, accounts, name="Wizzy", level="wizard")
    out, _ = _say(wiz, ".examine")
    assert "their rank here is staff" in out.lower()

    # ...and it is the same line a stranger sees, not a special self-view.
    onlooker = _session(world, clock, accounts, name="Corvid")
    assert _say(onlooker, ".examine Wizzy")[0] == out


def test_examine_with_an_argument_is_unchanged(world, clock, accounts):
    s = _session(world, clock, accounts)
    out, _ = _say(s, ".examine Wizzy")
    assert "Examining Wizzy" in out
    assert "a staff of the peace" in out
    # ...and the failures still read as before.
    out, _ = _say(s, ".examine Nobody")
    assert "There's no one called Nobody" in out


def test_bare_examine_is_narrow_aware(world, clock, accounts):
    """It renders through the ordinary path, so it inherits the 40-column
    layout rather than needing its own."""
    s = _session(world, clock, accounts, width=40)
    out, _ = _say(s, ".examine")
    assert "Examining Corvid" in out
    for line in out.split("\n"):
        assert len(line) <= 40, repr(line)


# ---------------------------------------------------------------------------
# Part 2 — bare .desc shows, and asks before clearing
# ---------------------------------------------------------------------------

def test_bare_desc_shows_your_own_and_asks(world, clock, accounts):
    s = _session(world, clock, accounts)
    out, result = _say(s, ".desc")
    assert "staff of crows" in out
    # It has NOT cleared anything — it asked. The prompt is the session loop's.
    assert result == DESC_CONFIRM
    assert s.resident.description == "staff of crows"


def test_bare_desc_does_not_clear_by_itself(world, clock, accounts):
    """The old bug in one line: dispatching a bare `.desc` used to empty it."""
    s = _session(world, clock, accounts)
    for _ in range(3):
        _say(s, ".desc")
    assert s.resident.description == "staff of crows"
    assert accounts.get("Corvid").description == "staff of crows"


def test_confirming_clears_it(world, clock, accounts):
    s = _session(world, clock, accounts)
    _say(s, ".desc")
    s.clear()
    clear_description(s)                        # what a `y` runs
    assert s.resident.description == ""
    assert accounts.get("Corvid").description == ""
    assert "cleared" in s.text.lower()


def test_a_house_default_is_shown_but_not_offered_for_clearing(
        world, clock, accounts, tmp_path):
    """There is nothing personal to clear, and offering to clear something you
    did not write would be offering to do nothing."""
    accounts.set_description("Corvid", "")
    st = _house_desc(tmp_path, "one of the many")
    s = _session(world, clock, accounts, state=st)
    s.resident.description = ""

    out, result = _say(s, ".desc")
    assert "one of the many" in out
    assert result is None                       # NO prompt
    assert "no description of your own" in out.lower()


def test_neither_says_so_plainly(world, clock, accounts, tmp_path):
    accounts.set_description("Corvid", "")
    s = _session(world, clock, accounts, state=StateStore(tmp_path / "state.json"))
    s.resident.description = ""

    out, result = _say(s, ".desc")
    assert result is None                       # NO prompt
    assert "no description" in out.lower()
    assert ".desc <text>" in out                # ...and how to get one


def test_desc_with_text_still_sets(world, clock, accounts):
    s = _session(world, clock, accounts)
    out, result = _say(s, ".desc a bird of considerable opinion")
    assert result is None
    assert s.resident.description == "a bird of considerable opinion"
    assert accounts.get("Corvid").description == "a bird of considerable opinion"
    assert "Description set:" in out


def test_setting_still_collapses_whitespace_and_caps_length(world, clock, accounts):
    s = _session(world, clock, accounts)
    _say(s, ".desc   spaced    out   ")
    assert s.resident.description == "spaced out"
    _say(s, ".desc " + "x" * 200)
    assert len(s.resident.description) == 120


def test_bare_desc_is_narrow_aware(world, clock, accounts):
    """`.desc` emits ordinary lines, which `Session.send` wraps at the session's
    width — so narrowness costs this command nothing and is inherited rather
    than re-implemented. What IS this command's business is that a narrow reader
    still gets the description and the offer, which a truncating layout would
    have cost them."""
    s = _session(world, clock, accounts, width=40)
    out, result = _say(s, ".desc")
    assert "staff of crows" in out
    assert result == DESC_CONFIRM


# ---------------------------------------------------------------------------
# The confirm step end to end, over a real socket
# ---------------------------------------------------------------------------

def _cfg(tmp_path):
    return Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        events_file=str(tmp_path / "events.jsonl"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )


async def _collect(r, timeout=0.3):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _login(port, name, pw):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name)
    await _collect(r); await _send(w, pw)
    await _collect(r, timeout=0.5)
    return r, w


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,kept", [
    ("y", False), ("yes", False),
    ("n", True), ("no", True), ("", True), ("maybe", True),
])
async def test_only_an_explicit_yes_clears_it(tmp_path, answer, kept):
    """Anything but a yes keeps it — INCLUDING a bare Enter, which is the
    likeliest way somebody arrives at this prompt by accident."""
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        port = srv._servers[0].sockets[0].getsockname()[1]
        seed_account(srv.accounts, "Corvid", "correct horse battery")
        srv.accounts.set_description("Corvid", "staff of crows")

        r, w = await _login(port, "Corvid", "correct horse battery")
        await _send(w, ".desc")
        shown = await _collect(r, timeout=0.5)
        assert "staff of crows" in shown
        assert "Clear it?" in shown

        await _send(w, answer)
        out = await _collect(r, timeout=0.5)
        w.close()

        stored = srv.accounts.get("Corvid").description
        if kept:
            assert stored == "staff of crows"
            assert "Kept" in out
        else:
            assert stored == ""
            assert "cleared" in out.lower()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_no_prompt_appears_when_there_is_nothing_of_yours_to_clear(tmp_path):
    """The house-default case over a real socket: shown, and the session loop
    is never handed a confirm — so a stray Enter afterwards is just a blank
    line, not an answer to a question nobody asked."""
    srv = TalkerServer(_cfg(tmp_path))
    srv.state.set_house_setting("desc", "one of the many")
    await srv.start()
    try:
        port = srv._servers[0].sockets[0].getsockname()[1]
        seed_account(srv.accounts, "Corvid", "correct horse battery")

        r, w = await _login(port, "Corvid", "correct horse battery")
        await _send(w, ".desc")
        shown = await _collect(r, timeout=0.5)
        assert "one of the many" in shown
        assert "Clear it?" not in shown
        w.close()
    finally:
        await srv.aclose()
