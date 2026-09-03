# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Narrow mode — the 40-column portrait-phone layout.

One switch, two values: wide (the operator's `wrap_columns`, 78 by default) and
narrow (40). The browser measures its own columns and reports them; the server
resolves that to one of the two and lays every screen out accordingly.

What these tests are actually protecting:

  * that narrow is WEB-ONLY, by construction — a socket door has no way to
    report a width and must never end up at 40;
  * that every width-driven screen is narrow-AWARE, because one screen still
    emitting a 78-column rule on a 40-column phone is instantly visible;
  * that the `.narrow.txt` art convention FALLS BACK silently, so adding this
    to a house that has drawn no narrow art changes nothing there;
  * and that the wide output is byte-for-byte what it always was.
"""

import asyncio
from pathlib import Path

import pytest

from conftest import FakeClock, FakeSession, RecordingTransport
from heresay import reboot
from heresay.commands import (NARROW_COLUMNS, dispatch, frame_rule, framed,
                              help_text, narrow_columns, narrow_stacked,
                              pack_tokens, space_out, wrap_at)
from heresay.config import Config
from heresay.session import (NARROW_THRESHOLD, Session, resolve_width)
from heresay.transport import CTRL_WIDTH, SocketTransport, parse_width_frame
from heresay.world import load_world, narrow_variant, read_narrow

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def world():
    return load_world(WORLD, "atrium", "holding_cell")


def narrow(world, **kw):
    return FakeSession(world, width=NARROW_COLUMNS, **kw)


def wide(world, **kw):
    return FakeSession(world, **kw)


def block(session) -> list[str]:
    """The last thing sent, as lines."""
    return session.out[-1].split("\n")


def widest(lines) -> int:
    return max((len(ln) for ln in lines), default=0)


# ---------------------------------------------------------------------------
# The width switch itself
# ---------------------------------------------------------------------------

def test_no_report_means_wide():
    assert resolve_width(None, 78) == 78


def test_a_phone_width_resolves_to_narrow():
    assert resolve_width(40, 78) == NARROW_COLUMNS
    assert resolve_width(46, 78) == NARROW_COLUMNS


def test_a_landscape_width_resolves_to_wide():
    assert resolve_width(72, 78) == 78
    assert resolve_width(200, 78) == 78


def test_the_threshold_is_exclusive_and_sits_between_the_two_clusters():
    # Below is narrow, at-or-above is wide. Stated as a test because the
    # boundary is the whole rule.
    assert resolve_width(NARROW_THRESHOLD - 1, 78) == NARROW_COLUMNS
    assert resolve_width(NARROW_THRESHOLD, 78) == 78
    # A portrait phone (~40-46) and a landscape one (~70+) are both a long way
    # from the boundary, so neither can flicker across it.
    assert 46 < NARROW_THRESHOLD < 70


def test_wide_follows_the_operators_wrap_columns_not_a_hard_78():
    # An operator who set 100 keeps 100; narrow is still exactly 40.
    assert resolve_width(None, 100) == 100
    assert resolve_width(120, 100) == 100
    assert resolve_width(40, 100) == NARROW_COLUMNS


def test_session_width_defaults_from_config_wrap_columns():
    world = load_world(WORLD, "atrium", "holding_cell")
    sess = Session(RecordingTransport(), world, None, Config(wrap_columns=100),
                   clock=FakeClock(), wall_clock=lambda: 0.0)
    assert sess.width == 100
    assert not sess.is_narrow


def test_narrowing_a_session_never_touches_the_shared_config():
    """The one hazard worth a test of its own: one Config is shared by every
    connection, so a width that lived on it would narrow the whole server."""
    world = load_world(WORLD, "atrium", "holding_cell")
    cfg = Config(wrap_columns=78)
    tr = RecordingTransport()
    tr.reported_width = 40
    sess = Session(tr, world, None, cfg, clock=FakeClock(), wall_clock=lambda: 0.0)
    assert sess.width == NARROW_COLUMNS
    assert cfg.wrap_columns == 78          # untouched


def test_rotating_the_phone_flips_the_width_live():
    world = load_world(WORLD, "atrium", "holding_cell")
    tr = RecordingTransport()
    tr.reported_width = 40
    sess = Session(tr, world, None, Config(wrap_columns=78),
                   clock=FakeClock(), wall_clock=lambda: 0.0)
    assert sess.is_narrow
    tr.reported_width = 90                 # turned sideways
    assert sess.width == 78
    assert not sess.is_narrow


# ---------------------------------------------------------------------------
# The wire format: \x01width <cols>
# ---------------------------------------------------------------------------

def test_a_well_formed_width_frame_parses():
    assert parse_width_frame(CTRL_WIDTH + "40") == 40
    assert parse_width_frame(CTRL_WIDTH + "  132  ") == 132


def test_junk_in_a_width_frame_is_ignored_not_answered():
    for junk in ("", "abc", "40 40", "4.5", "-", "0x20"):
        assert parse_width_frame(CTRL_WIDTH + junk) is None


def test_an_out_of_range_width_is_ignored():
    assert parse_width_frame(CTRL_WIDTH + "0") is None
    assert parse_width_frame(CTRL_WIDTH + "-40") is None
    assert parse_width_frame(CTRL_WIDTH + "100000") is None


def test_a_line_that_is_not_a_width_frame_is_not_one():
    assert parse_width_frame("width 40") is None          # no control prefix
    assert parse_width_frame("\x01title Commons") is None


class _FakeWS:
    """The minimum of a websockets connection: a scripted list of frames."""

    def __init__(self, frames):
        self._frames = list(frames)

    async def recv(self):
        # Out of script = a quiet peer, which is an ordinary condition (an older
        # client that sends a width report and nothing else), so BLOCK rather
        # than raise. A bounded wait is supposed to time out here.
        if not self._frames:
            await asyncio.sleep(3600)
        return self._frames.pop(0)

    async def send(self, frame):
        pass

    async def close(self):
        pass


def _ws(frames):
    from heresay.transport import WebSocketTransport
    tr = WebSocketTransport.__new__(WebSocketTransport)
    tr._conn = _FakeWS(frames)
    tr._closed = False
    tr._sender = None
    tr.peer_ip = "127.0.0.1"
    tr.reported_width = None
    tr.pending_resume = None
    tr._pushback = []
    return tr


@pytest.mark.asyncio
async def test_a_width_frame_is_consumed_and_never_surfaces_as_a_line():
    """It used to come back as "" — harmless while nothing sent one, but a
    resize during login would have read as a blank line and re-prompted."""
    tr = _ws([CTRL_WIDTH + "40", "hello"])
    line = await tr.recv_line()
    assert line == "hello"                 # the blank never appeared
    assert tr.reported_width == 40


@pytest.mark.asyncio
async def test_the_prebanner_wait_takes_the_width_report():
    # A current client sends width THEN a resume frame; the resume frame is what
    # ends the wait (v1.24.0), so the hello costs a round trip, not the timeout.
    tr = _ws([CTRL_WIDTH + "42", "\x01resume"])
    await tr.wait_for_hello(0.05)
    assert tr.reported_width == 42
    assert tr._pushback == []


@pytest.mark.asyncio
async def test_the_prebanner_wait_still_serves_an_older_client():
    """One that sends a width report and nothing else: it pays the timeout once
    and is otherwise unaffected."""
    tr = _ws([CTRL_WIDTH + "42"])
    await tr.wait_for_hello(0.05)          # runs out the clock, does not raise
    assert tr.reported_width == 42


@pytest.mark.asyncio
async def test_the_prebanner_wait_pushes_back_a_real_line():
    """A fast typer must not lose their first line to a timing optimisation."""
    tr = _ws(["Corvid"])
    await tr.wait_for_hello(0.05)
    assert tr.reported_width is None       # nothing was reported
    assert await tr.recv_line() == "Corvid"   # and the line survived


@pytest.mark.asyncio
async def test_the_prebanner_wait_gives_up_quietly():
    tr = _ws([])

    class _Silent:
        async def recv(self):
            import asyncio
            await asyncio.sleep(10)

    tr._conn = _Silent()
    await tr.wait_for_hello(0.01)          # returns, does not raise
    assert tr.reported_width is None


# ---------------------------------------------------------------------------
# Narrow is web-only
# ---------------------------------------------------------------------------

def test_a_socket_door_can_never_report_a_width():
    assert SocketTransport.reported_width is None
    assert SocketTransport.supports_control_frames is False


@pytest.mark.asyncio
async def test_a_socket_session_is_always_wide(world):
    """The plaintext/telnet door has no way to say it is small, so it is not."""
    import asyncio
    reader = asyncio.StreamReader()
    reader.feed_data(b"hello\n")
    reader.feed_eof()

    class _W:
        def is_closing(self): return False
        def write(self, b): pass
        def close(self): pass

    tr = SocketTransport(reader, _W(), "127.0.0.1")
    sess = Session(tr, world, None, Config(wrap_columns=78),
                   clock=FakeClock(), wall_clock=lambda: 0.0)
    assert sess.width == 78
    assert not sess.is_narrow


@pytest.mark.asyncio
async def test_a_control_line_on_the_socket_door_is_dropped_not_spoken():
    """A Ctrl-A-prefixed line is never interpreted here and never becomes
    speech — the same rule as the web door, so there is one rule."""
    import asyncio
    reader = asyncio.StreamReader()
    reader.feed_data(CTRL_WIDTH.encode() + b"40\nhello\n")
    reader.feed_eof()

    class _W:
        def is_closing(self): return False
        def write(self, b): pass
        def close(self): pass

    tr = SocketTransport(reader, _W(), "127.0.0.1")
    assert await tr.recv_line() == "hello"     # the frame never surfaced
    assert tr.reported_width is None           # and was never interpreted


# ---------------------------------------------------------------------------
# The *** filler and the frame
# ---------------------------------------------------------------------------

def test_the_filler_rule_is_the_session_width():
    assert len(frame_rule()) == 78
    assert len(frame_rule("", 40)) == 40
    assert frame_rule("", 40) == "*" * 40


def test_a_titled_rule_fills_to_the_session_width():
    for w in (40, 78):
        rule = frame_rule("Accounts", w)
        assert len(rule) == w
        assert rule.startswith("*** Accounts ")
        assert rule.endswith("*")


def test_a_framed_block_is_square_at_either_width():
    for w in (40, 78):
        lines = framed("Title", "body", "footer", width=w).split("\n")
        assert len(lines[0]) == w
        assert len(lines[-1]) == w


def test_an_over_long_title_backs_off_to_a_word_boundary():
    # Chopped mid-word a title reads as corruption; abbreviated at a space it
    # reads as an abbreviation. At 40 columns real titles reach this.
    rule = frame_rule("Ignore — .ignore <category> yes|no", 40)
    assert len(rule) == 40
    assert "yes|" not in rule              # not severed inside the word


def test_the_presence_brackets_are_content_length_at_any_width():
    """`[*** name … ***]` is a wrapper, not a filler — there is no width in it
    to get wrong, and it wraps through send() like any other line."""
    from heresay.state import jail_presence_line, presence_line
    assert presence_line("Bob", "enters the world") == "[Bob enters the world]"
    assert jail_presence_line("Bob", "arrives (jail)") == "[*** Bob arrives (jail) ***]"


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------

def test_speech_wraps_to_the_session_width(world):
    long_line = "commons " * 30
    for w, cols in ((40, 40), (78, 78)):
        tr = RecordingTransport()
        tr.reported_width = 40 if w == 40 else None
        sess = Session(tr, world, None, Config(wrap_columns=78),
                       clock=FakeClock(), wall_clock=lambda: 0.0)
        sess.send(long_line)
        body = [ln for ln in tr.text.split("\n") if ln.strip()]
        assert body and widest(body) <= cols


def test_wrap_at_indents_every_line_after_the_first():
    lines = wrap_at("one two three four five six seven eight", 20, "  ")
    assert widest(lines) <= 20
    assert all(ln.startswith("  ") for ln in lines)


def test_wrap_at_never_swallows_an_empty_value():
    assert wrap_at("", 40) == [""]


# ---------------------------------------------------------------------------
# The narrow layout primitives
# ---------------------------------------------------------------------------

def test_narrow_columns_aligns_continuations_under_the_right_column():
    rows = [(".say", "Speak aloud to everyone in the room."),
            (".tell", "Send one person a private message.")]
    lines = narrow_columns(rows, 40, gutter=2)
    assert widest(lines) <= 40
    assert lines[0].startswith(".say")
    col = len(".tell") + 2                 # longest label + gutter
    continuations = [ln for ln in lines if not ln.startswith(".")]
    assert continuations, "the sample must actually wrap"
    for ln in continuations:
        assert ln.startswith(" " * col)
        assert ln[col] != " "              # aligned exactly, not merely indented


def test_narrow_columns_stacks_a_row_whose_label_is_too_long():
    rows = [("short", "a value"),
            (".promote / .demote <name>", "resident <-> staff (persisted)")]
    lines = narrow_columns(rows, 40, gutter=2)
    assert widest(lines) <= 40
    assert ".promote / .demote <name>" in lines      # on its own line
    assert lines[lines.index(".promote / .demote <name>") + 1].startswith("  ")


def test_narrow_stacked_puts_a_blank_between_entries():
    lines = narrow_stacked([("a", "one"), ("b", "two")], 40)
    assert lines == ["a", "  one", "", "b", "  two"]


def test_pack_tokens_never_breaks_a_token_in_half():
    tokens = [".tell <text> (.t >)", ".remote <text> (.r <)"]
    lines = pack_tokens(tokens, 24)
    assert widest(lines) <= 24
    joined = " ".join(lines)
    for t in tokens:
        assert t in joined                 # each survives intact on some line


def test_pack_tokens_counts_the_TRAILING_SEPARATOR_in_the_width():
    """⛔ A latent off-by-one, fixed in v1.50.0 — and this test used to permit it.

    A line that is continued ends with ``,``. The fit test measured the content
    without that comma, so a group landing exactly on the width came back one
    column over. The assertion above said `<= 24 + len(",")` with the comment
    "a trailing separator may ride the edge" — which is precisely the bug,
    written down as though it were a decision. It cannot ride the edge: the
    caller drops these lines into a fixed-width field, so one extra column
    punches through the frame.

    It stayed hidden because it needs a group that fills the width EXACTLY and
    another token after it. Adding `.report` to the `house` category made
    ``.call_staff, .mail_staff`` (24 in a 24-column field) no longer last, and
    three 40-column help screens overflowed at once.
    """
    # Two tokens that together fill the width exactly, plus a third to force
    # the flush — the shape that was overflowing.
    tokens = ["aaaaaaaaaa", "bbbbbbbbbbbb", "c"]
    assert len("aaaaaaaaaa, bbbbbbbbbbbb") == 24
    for line in pack_tokens(tokens, 24):
        assert len(line) <= 24, f"{len(line)} cols: {line!r}"
    # ...and nothing was dropped on the way.
    joined = " ".join(pack_tokens(tokens, 24))
    for t in tokens:
        assert t in joined


def test_pack_tokens_gives_an_over_long_token_its_own_line():
    lines = pack_tokens(["short", "x" * 60], 20)
    assert "x" * 60 in lines


def test_space_out_separates_entries_but_not_fragments():
    text = "header\n\nalpha  one\nbeta  two"
    out = space_out(text).split("\n")
    assert out == ["header", "", "alpha  one", "", "beta  two"]


def test_space_out_leaves_an_already_spaced_page_alone():
    text = "a\n\nb\n\nc"
    assert space_out(text) == text


# ---------------------------------------------------------------------------
# Every width-driven screen: nothing overflows at 40
# ---------------------------------------------------------------------------

SCREENS = [".help", ".who", ".set", ".ignore", ".map", ".examine Tester",
           ".accounts", ".ownerset"]


def _screen_store(tmp_path):
    from heresay.accounts import AccountStore
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Tester", "a-good-passphrase-here")
    store.get("Tester").description = "a bird of considerable and stated opinion"
    return store


@pytest.mark.parametrize("cmd", SCREENS)
def test_no_screen_overflows_forty_columns(world, cmd, tmp_path):
    """The whole point, stated once per screen. A single block still emitting a
    78-column rule on a phone is the most visible way this could ship broken."""
    from heresay.state import StateStore
    sess = narrow(world, name="Tester", level="arch",
                  accounts=_screen_store(tmp_path),
                  state=StateStore(tmp_path / "state.json"))
    sess.resident.description = "a bird of considerable and stated opinion"
    dispatch(sess, cmd)
    assert sess.out, f"{cmd} produced nothing"
    lines = [ln for blk in sess.out for ln in blk.split("\n")]
    over = [ln for ln in lines if len(ln) > NARROW_COLUMNS]
    assert not over, f"{cmd}: {len(over)} line(s) over 40: {over[:2]}"


@pytest.mark.parametrize("cmd", SCREENS)
def test_no_screen_overflows_seventy_eight_columns(world, cmd, tmp_path):
    """The wide side of the same promise, unchanged by any of this."""
    from heresay.state import StateStore
    sess = wide(world, name="Tester", level="arch",
                accounts=_screen_store(tmp_path),
                state=StateStore(tmp_path / "state.json"))
    sess.resident.description = "a bird of considerable and stated opinion"
    dispatch(sess, cmd)
    lines = [ln for blk in sess.out for ln in blk.split("\n")]
    assert not [ln for ln in lines if len(ln) > 78]


# ---------------------------------------------------------------------------
# Help — both styles
# ---------------------------------------------------------------------------

def test_narrow_long_help_is_command_gutter_explanation():
    from heresay.theme import DEFAULT_THEME
    text = help_text(DEFAULT_THEME, "long", "resident", width=40)
    lines = text.split("\n")
    assert widest(lines) <= 40
    row = next(ln for ln in lines if ln.startswith(".who "))
    assert row.startswith(".who")
    assert "  " in row                      # a gutter, then the explanation


def test_narrow_long_help_still_lists_every_command():
    from heresay.theme import DEFAULT_THEME
    narrow_text = help_text(DEFAULT_THEME, "long", "resident", width=40)
    wide_text = help_text(DEFAULT_THEME, "long", "resident", width=78)
    for verb in (".who", ".go <room>", ".map", ".mail", ".quit", ".raise_prompt"):
        assert verb in wide_text
        assert verb in narrow_text, f"{verb} vanished from the narrow list"


def test_narrow_short_help_is_section_then_its_commands():
    from heresay.theme import DEFAULT_THEME
    text = help_text(DEFAULT_THEME, "short", "resident", width=40)
    lines = text.split("\n")
    assert widest(lines) <= 40
    assert any(ln.startswith("talking") for ln in lines)
    assert any(ln.startswith("administrative") for ln in lines)


def test_narrow_short_help_never_breaks_a_command_from_its_shortcut():
    from heresay.theme import DEFAULT_THEME
    text = help_text(DEFAULT_THEME, "short", "resident", width=40)
    # These carry their shortcut in parentheses; a naive wrap splits them.
    for token in (".tell (.t >)", ".remote (.r <)", ".examine (.ex .exa)"):
        assert token in text, f"{token!r} was broken across lines"


def test_the_wide_help_screens_are_untouched():
    from heresay.theme import DEFAULT_THEME
    for style in ("long", "short"):
        assert help_text(DEFAULT_THEME, style, "resident") == \
               help_text(DEFAULT_THEME, style, "resident", width=78)


def test_the_staff_sections_are_narrow_aware():
    """v1.29.0: one generated screen, so the staff tiers are SECTIONS of it —
    and they have to lay out at 40 like everything else."""
    from heresay.theme import DEFAULT_THEME
    for rank in ("staff", "owner"):
        for style in ("long", "short"):
            assert widest(help_text(DEFAULT_THEME, style, rank, 40).split("\n")) <= 40
            assert widest(help_text(DEFAULT_THEME, style, rank, 78).split("\n")) <= 78


# ---------------------------------------------------------------------------
# .help set / .help ignore — spaced out in narrow
# ---------------------------------------------------------------------------

HELP_DIR = Path(__file__).resolve().parent / "fixtures" / "house" / "help"


def _topics():
    return {p.stem: p.read_text(encoding="utf-8").rstrip("\n")
            for p in HELP_DIR.glob("*.txt")}


@pytest.mark.parametrize("topic", ["set", "ignore"])
def test_the_list_pages_are_spaced_out_in_narrow(world, topic):
    sess = narrow(world, help_topics=_topics())
    dispatch(sess, f".help {topic}")
    rendered = "\n".join(block(sess))
    original = _topics()[topic]
    # Two entries that are adjacent in the authored page must NOT be adjacent
    # here: something (a blank) came between them.
    authored = [ln for ln in original.split("\n") if ln.strip()]
    a, b = authored[-2].strip()[:8], authored[-1].strip()[:8]
    assert a in rendered and b in rendered
    body = block(sess)
    ia = next(i for i, ln in enumerate(body) if a in ln)
    ib = next(i for i, ln in enumerate(body) if b in ln)
    assert ib - ia >= 2, "narrow must put a blank line between entries"
    assert widest(body) <= NARROW_COLUMNS


@pytest.mark.parametrize("topic", ["set", "ignore"])
def test_the_list_pages_are_not_spaced_out_wide(world, topic):
    """Wide is a terminal screen, where the vertical space is the scarce thing."""
    sess = wide(world, help_topics=_topics())
    dispatch(sess, f".help {topic}")
    body = [ln for ln in block(sess)[2:-2] if ln.strip()]
    rendered = "\n".join(block(sess))
    original = _topics()[topic]
    # Every authored line survives contiguously — no blanks were injected.
    assert original.split("\n")[2] in rendered
    assert original.split("\n")[3] in rendered


def test_a_prose_page_is_not_spaced_out(world):
    """Only the LIST pages get this; a prose page would be shredded by it."""
    sess = narrow(world, help_topics=_topics())
    dispatch(sess, ".help who")
    assert widest(block(sess)) <= NARROW_COLUMNS


# ---------------------------------------------------------------------------
# .who
# ---------------------------------------------------------------------------

def test_narrow_who_is_two_lines_per_resident(world):
    sess = narrow(world, name="Corvid")
    sess.resident.description = "a bird of considerable opinion"
    dispatch(sess, ".who")
    lines = block(sess)
    body = [ln for ln in lines[2:-2] if ln.strip()]
    assert body[0].startswith("Corvid")
    assert "a bird of considerable opinion" in "\n".join(body)
    where = next(ln for ln in body if "idle" in ln)
    assert where.startswith("  ")                 # indented beneath the sentence
    assert "atrium" in where


def test_narrow_who_does_not_truncate_the_description(world):
    """The wide geometry degrades at 40 by truncating the description to
    nothing — which throws away the only part of the row anyone reads."""
    sess = narrow(world, name="Corvid")
    sess.resident.description = "keeps a pocket watch and is perpetually late"
    dispatch(sess, ".who")
    assert "perpetually late" in sess.out[-1]


def test_narrow_who_keeps_its_afk_marker(world):
    sess = narrow(world, name="Corvid")
    sess.resident.afk = True
    dispatch(sess, ".who")
    assert "(AFK)" in sess.out[-1]


def test_wide_who_is_unchanged(world):
    sess = wide(world, name="Corvid")
    dispatch(sess, ".who")
    assert "*** Current users on " in sess.out[-1]
    assert "idle" in sess.out[-1]


# ---------------------------------------------------------------------------
# The other column screens
# ---------------------------------------------------------------------------

def test_narrow_examine_gives_the_name_its_own_line(world, tmp_path):
    from heresay.accounts import AccountStore
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "a-good-passphrase-here")
    store.get("Corvid").description = "a bird of considerable opinion"
    sess = narrow(world, name="Corvid", accounts=store)
    dispatch(sess, ".examine Corvid")
    lines = block(sess)
    # In wide the name and description share a line; in narrow the name leads.
    body = [ln for ln in lines[2:-2] if ln.strip()]
    assert body[0] == "Corvid"
    assert widest(lines) <= NARROW_COLUMNS


def test_narrow_accounts_puts_the_name_on_its_own_line(world, tmp_path):
    from heresay.accounts import AccountStore
    from heresay.state import StateStore
    accts = AccountStore(tmp_path / "accounts.json")
    accts.create("Corvid", "a-good-passphrase-here")
    sess = narrow(world, name="Boss", level="arch", accounts=accts,
                  state=StateStore(tmp_path / "state.json"))
    dispatch(sess, ".accounts")
    lines = block(sess)
    assert widest(lines) <= NARROW_COLUMNS
    assert any(ln.strip() == "Corvid" for ln in lines), \
        "the name must never be truncated — every staff command takes it as input"


def test_narrow_ownerset_stacks_its_values(world, tmp_path):
    from heresay.state import StateStore
    sess = narrow(world, name="Boss", level="arch",
                  state=StateStore(tmp_path / "state.json"))
    dispatch(sess, ".ownerset")
    lines = block(sess)
    assert widest(lines) <= NARROW_COLUMNS
    assert any(ln.rstrip() == "login_msg" for ln in lines)


def test_narrow_mailbox_splits_the_sender_from_the_date(world, tmp_path):
    from heresay.mail import MailStore
    mail = MailStore(tmp_path / "mail.json", tmp_path / "mail.key")
    mail.deliver("Corvid", "Wren", "a short note")
    sess = narrow(world, name="Corvid", mail=mail)
    dispatch(sess, ".mail")
    lines = block(sess)
    assert widest(lines) <= NARROW_COLUMNS
    assert any(ln.startswith("1) from Wren") for ln in lines)


def test_narrow_keeper_letterbox_splits_the_sender_from_the_date(world, tmp_path):
    from heresay.commands.staff import STAFF_BOX
    from heresay.mail import MailStore
    mail = MailStore(tmp_path / "mail.json", tmp_path / "mail.key")
    mail.deliver(STAFF_BOX, "Corvid", "someone is shouting")
    sess = narrow(world, name="Wizzy", level="wizard", mail=mail)
    dispatch(sess, ".staffmail")
    lines = block(sess)
    assert widest(lines) <= NARROW_COLUMNS
    assert any(ln.startswith("1) from Corvid") for ln in lines)


def test_narrow_set_marks_the_house_defaults(world):
    sess = narrow(world)
    dispatch(sess, ".set")
    text = sess.out[-1]
    assert "(house default)" in text
    assert widest(text.split("\n")) <= NARROW_COLUMNS


def test_narrow_ignore_shows_every_category_and_its_state(world):
    from heresay.commands.settings import IGNORE_CATEGORIES
    sess = narrow(world)
    dispatch(sess, ".ignore")
    text = sess.out[-1]
    for cat in IGNORE_CATEGORIES:
        assert cat in text
    assert widest(text.split("\n")) <= NARROW_COLUMNS


# ---------------------------------------------------------------------------
# The .narrow.txt convention: selection and silent fallback
# ---------------------------------------------------------------------------

def test_narrow_variant_names_the_sibling_file():
    assert narrow_variant(Path("world/map.txt")).name == "map.narrow.txt"
    assert narrow_variant(Path("a/greeting.txt")).name == "greeting.narrow.txt"


def test_read_narrow_is_empty_when_there_is_no_variant(tmp_path):
    plain = tmp_path / "map.txt"
    plain.write_text("wide art\n")
    assert read_narrow(plain) == ""          # silently optional, never an error


def test_read_narrow_reads_the_variant(tmp_path):
    plain = tmp_path / "map.txt"
    plain.write_text("wide art\n")
    (tmp_path / "map.narrow.txt").write_text("narrow art\n")
    assert read_narrow(plain) == "narrow art"


def test_the_shipped_house_has_narrow_art(world):
    """Installed as part of this release; both must fit the width they are for."""
    assert world.ascii_map_narrow, "house/world/map.narrow.txt should be installed"
    assert widest(world.ascii_map_narrow.split("\n")) <= NARROW_COLUMNS


def test_map_for_picks_the_narrow_art(world):
    assert world.map_for(True) == world.ascii_map_narrow
    assert world.map_for(False) == world.ascii_map


def test_map_for_falls_back_when_no_narrow_art_exists(world):
    world.ascii_map_narrow = ""
    assert world.map_for(True) == world.ascii_map      # a map, just a wide one


def test_the_shipped_narrow_greeting_fits(world):
    path = Path(__file__).resolve().parent / "fixtures" / "house" / "greeting.narrow.txt"
    assert path.is_file(), "house/greeting.narrow.txt should be installed"
    text = path.read_text(encoding="utf-8").rstrip("\n")
    assert widest(text.split("\n")) <= NARROW_COLUMNS


def test_a_house_with_no_narrow_greeting_still_serves_one(tmp_path):
    from heresay.server import _load_greeting
    g = tmp_path / "greeting.txt"
    g.write_text("the wide banner\n")
    assert _load_greeting(g) == "the wide banner"
    assert read_narrow(g) == ""            # and narrow simply falls back


# ---------------------------------------------------------------------------
# .reboot
# ---------------------------------------------------------------------------

REBOOT_LINE = "A very long boot line that will certainly need wrapping somewhere"


def test_reboot_is_parsed_at_both_widths():
    wide_steps = reboot.parse(REBOOT_LINE, width=78)
    narrow_steps = reboot.parse(REBOOT_LINE, width=40)
    wide_lines = [s[1] for s in wide_steps if s[0] == "line"]
    narrow_lines = [s[1] for s in narrow_steps if s[0] == "line"]
    assert widest(wide_lines) <= 78
    assert widest(narrow_lines) <= 40
    assert len(narrow_lines) > len(wide_lines)     # genuinely re-wrapped


def test_a_session_picks_the_reboot_script_for_its_width(world):
    sess = narrow(world)
    sess.reboot_steps = reboot.parse(REBOOT_LINE, width=78)
    sess.reboot_steps_narrow = reboot.parse(REBOOT_LINE, width=40)
    assert sess.reboot_script() is sess.reboot_steps_narrow
    sess.width = 78
    assert sess.reboot_script() is sess.reboot_steps


def test_a_narrow_session_falls_back_to_the_wide_script(world):
    """No narrow parse (no reboot.txt at all, or an older server) simply plays
    the wide one rather than playing nothing."""
    sess = narrow(world)
    sess.reboot_steps = reboot.parse(REBOOT_LINE, width=78)
    sess.reboot_steps_narrow = None
    assert sess.reboot_script() is sess.reboot_steps


# ---------------------------------------------------------------------------
# The narrow column block — one helper, four screens (v1.26.1)
# ---------------------------------------------------------------------------
#
# An over-long label used to drop its description into a two-space hang at
# nearly full width, which broke the one vertical line the reader follows down
# the screen — and at 40 the rows that trip it (`.tell <name> <text>`,
# `.gag / .ungag <name>`) are exactly the ones the eye most needs placed. The
# rule now lives in `narrow_columns` and all four narrow help screens go through
# it, so none of them can drift away from the others.

def _desc_col(lines):
    """The column the descriptions sit in, read off a row that fits its label."""
    row = next(ln for ln in lines
               if ln.startswith(".who") or ln.startswith(".gag")
               or ln.startswith(".accounts") or ln.startswith("talking"))
    return len(row) - len(row[len(row.split("  ")[0]):].lstrip())


def _continuation_indent(lines, label):
    """The indent of the line AFTER an over-long label's own line."""
    i = lines.index(label)
    return len(lines[i + 1]) - len(lines[i + 1].lstrip())


# The rows that still trip the rule at 40. The paired forms
# (".gag / .ungag <name>") are two commands with two rows since v1.29.0, so the
# over-long labels that remain are the multi-argument ones.
# A label longer than the capped label column, per screen — the rows that
# actually exercise the over-long path. Each must ALSO carry a description long
# enough to wrap, or the second test below has nothing to measure.
#
# The staff entry was `.ban <name|ip> [hours]` until v1.32.0 split the command;
# `.ban <name> [hours]` is 19 against an 18-column budget, so it still overruns,
# and its description still wraps. (`.boot <name> [reason]` is longer still but
# its description fits on one line, which is why it is not the sample.)
OVERLONG = {
    "long": ".tell <name> <text>",
    "staff": ".ban <name> [hours]",
    "owner": ".resetpassword <name>",
}


def _screen(kind):
    from heresay.theme import DEFAULT_THEME as T
    rank = {"long": "resident", "staff": "staff", "owner": "owner"}[kind]
    return help_text(T, "long", rank, width=40).split("\n")


@pytest.mark.parametrize("kind", ["long", "staff", "owner"])
def test_an_over_long_label_keeps_the_description_column(kind):
    """The fix, stated three ways: indented to the description column, NOT a
    two-space hang, NOT back at the margin."""
    lines = _screen(kind)
    label = OVERLONG[kind]
    assert label in lines, "the over-long label takes a line of its own"
    indent = _continuation_indent(lines, label)
    assert indent == _desc_col(lines), (indent, _desc_col(lines))
    assert indent != 2, "the old two-space hang is gone"
    assert indent > 2


@pytest.mark.parametrize("kind", ["long", "staff", "owner"])
def test_an_over_long_labels_description_wraps_to_the_column(kind):
    """...and wrapped to the COLUMN's width, not the screen's."""
    lines = _screen(kind)
    label = OVERLONG[kind]
    i = lines.index(label)
    col = _desc_col(lines)
    block = []
    for ln in lines[i + 1:]:
        if not ln.startswith(" " * col):
            break
        block.append(ln)
    assert block, label
    assert len(block) > 1, "the sample must actually wrap"
    assert all(len(ln) <= 40 for ln in block)


@pytest.mark.parametrize("kind", ["long", "staff", "owner"])
def test_every_row_on_the_screen_shares_one_description_column(kind):
    """The point of columning: one vertical line down the whole screen."""
    lines = _screen(kind)
    col = _desc_col(lines)
    for ln in lines:
        if ln.startswith(" ") and ln.strip() and "*" not in ln:
            assert len(ln) - len(ln.lstrip()) == col, ln


def test_the_narrow_help_routes_through_narrow_columns(monkeypatch):
    """One shared helper, so a fix to the rule reaches the whole screen at once.
    (v1.29.0: one screen, so what used to be four callers is now the long style
    at every rank; short packs tokens instead, which is its own flavour.)"""
    from heresay.theme import DEFAULT_THEME as T
    import heresay.commands as cmds

    seen = []
    real = cmds.narrow_columns

    def spy(rows, width, **kw):
        seen.append(width)
        return real(rows, width, **kw)

    monkeypatch.setattr(cmds, "narrow_columns", spy)
    for rank in ("resident", "staff", "owner"):
        seen.clear()
        help_text(T, "long", rank, width=40)
        assert seen, f"long help at {rank} does not use the shared helper"


def test_the_helper_columns_a_synthetic_over_long_label():
    """Directly, and with the short index's own token-safe wrap — which is how
    `short` is provably protected even though no real label of its trips it."""
    packed = lambda text, right: pack_tokens(
        [t.strip() for t in text.split(",")], right)
    rows = [("short", "a b c"),
            ("a-really-very-long-label", "one two three four five six seven")]
    lines = narrow_columns(rows, 40, gutter=2, label_width=len("short"),
                           wrap=packed)
    assert lines[0].startswith("short  ")
    col = len("short") + 2
    assert lines[1] == "a-really-very-long-label"
    assert all(ln.startswith(" " * col) for ln in lines[2:])
    assert all(len(ln) <= 40 for ln in lines)
    # Never the old shape.
    assert not any(ln.startswith("  ") and not ln.startswith(" " * col)
                   for ln in lines[2:])


def test_the_helper_never_leaves_a_two_space_hang_at_forty():
    rows = [("x", "short"), (".remote <name> <text>", "a private emote sent to "
                             "one person anywhere at all")]
    lines = narrow_columns(rows, 40, gutter=2, label_width=1)
    assert lines[1] == ".remote <name> <text>"
    assert lines[2].startswith("   ")        # the column (1 + 2), not a 2-hang
    assert not lines[2].startswith("  a")


def test_an_over_long_label_with_no_text_is_just_the_label():
    lines = narrow_columns([("a", "x"), ("a-very-long-label", "")], 20,
                           gutter=2, label_width=1)
    assert lines[-1] == "a-very-long-label"


# ---- the regression guards ------------------------------------------------

def test_the_narrow_short_index_still_reads_as_one_line_per_group():
    """The byte-for-byte fixture went with the hand-written index in v1.29.0 —
    the screen is generated now, so pinning its bytes would pin the registry
    rather than the layout. What must hold is the SHAPE: a label column, its
    commands beside it, nothing over the width."""
    from heresay.theme import DEFAULT_THEME as T
    from heresay.commands.registry import CATEGORY_LABELS
    lines = [ln for ln in help_text(T, "short", "resident", width=40).split("\n")
             if ln and not ln.startswith("*")]
    labelled = [ln for ln in lines
                if ln[:1].isalpha() and not ln.startswith("More:")]
    assert labelled, lines
    for ln in labelled:
        assert ln.split("  ")[0].strip() in CATEGORY_LABELS.values(), ln
    assert widest(lines) <= 40


def test_the_wide_screens_never_take_the_narrow_path(monkeypatch):
    """78 is untouched by all of this, by construction: the wide renderer does
    not call the narrow helper at all."""
    from heresay.theme import DEFAULT_THEME as T
    import heresay.commands as cmds

    def boom(*a, **k):
        raise AssertionError("the wide path used the narrow column helper")

    monkeypatch.setattr(cmds, "narrow_columns", boom)
    for rank in ("resident", "staff", "owner"):
        help_text(T, "long", rank, width=78)
        help_text(T, "short", rank, width=78)


# ---- the staff footer (v1.26.1) -------------------------------------------

@pytest.mark.parametrize("width", [40, 78])
def test_there_is_no_such_power_is_gone_from_the_staff_footer(width):
    """It said again what the sentence before it had just said."""
    from heresay.theme import DEFAULT_THEME as T
    for rank in ("staff", "owner"):
        text = help_text(T, "long", rank, width)
        assert "There is no such power" not in text
        assert "no such power" not in text
        # ...and what remains still says the rule, cleanly, with no gap left
        # where the sentence was.
        assert "can read anyone's mail but their own." in " ".join(text.split())
        assert "  can read" not in text
        assert "\n\n\n" not in text


@pytest.mark.parametrize("width", [40, 78])
def test_the_staff_footer_still_states_the_mail_rule_and_the_logging(width):
    from heresay.theme import DEFAULT_THEME as T
    flat = " ".join(help_text(T, "long", "staff", width).split())
    assert "No one — staff or owner — can read anyone's mail but their own." in flat
    assert "Staff actions are logged as facts (who, what, to whom)." in flat
