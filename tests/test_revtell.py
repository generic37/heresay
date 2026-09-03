# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

""".revtell — your own last private lines. The covenant's SECOND stored content.

Encrypted with mail's key, owner-read-only, rolling at 20, deleted with the
account. The bounds are the feature.
"""

import asyncio
import importlib
import inspect
import json
import re
from pathlib import Path

import pytest

from heresay import commands as C
from heresay.commands import NOTHING_YET, REVTELL_HEADER, dispatch
from heresay.config import Config
from heresay.revtell import CAP, RevTellStore
from heresay.server import TalkerServer

from conftest import FakeSession


# ⛔ THE SHARED, NOBODY'S-OWN MAILBOXES — the only keys a command may read other
# than the caller's own name.
#
# Both are addressed-and-consented mail to a COLLECTIVE, read by the people it
# was addressed to; neither is a private box, and neither can BE one — a mailbox
# key beginning with '#' cannot be an account, because a name must start with a
# letter. `test_the_shared_boxes_really_are_nobody_s` proves that rather than
# trusting the naming, so this list stays a list of boxes that are structurally
# incapable of belonging to a person.
SHARED_BOXES = ("STAFF_BOX", "REPORT_BOX")


def _reads_only_shared_or_own(arg: str) -> bool:
    return "resident.name" in arg or any(b in arg for b in SHARED_BOXES)


def test_the_shared_boxes_really_are_nobody_s():
    """⚠ What makes the allowlist above safe to hold two entries instead of one.

    A shared box is legitimate to read only because it belongs to no account. If
    a future `#`-less key were added to SHARED_BOXES, the covenant tests would
    start permitting a read of somebody's private mail — so the property is
    asserted, not assumed.
    """
    import heresay.commands as _C
    from heresay.accounts import valid_name
    for name in SHARED_BOXES:
        key = getattr(_C, name)
        assert key.startswith("#"), key
        assert not valid_name(key), f"{key} could be a real account"
# The one non-content reader the owner-only sweep skips; defined once, in
# test_levels, so the two mirrored guards cannot drift apart.
from test_levels import EVENT_STORE_READERS

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def store(tmp_path):
    return RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key")


def _pair(world, clock, store):
    a = FakeSession(world, name="Corvid", clock=clock, revtells=store)
    b = FakeSession(world, name="Wren", clock=clock, revtells=store)
    return a, b


def _revtell(session):
    session.clear()
    dispatch(session, ".revtell")
    return session.text


# ---- both sides, in the wording each saw ---------------------------------

def test_tell_is_buffered_for_sender_and_recipient(world, clock, store):
    """Both hold the SAME line now: a tell renders as marked speech, and the
    sender's copy is the line they actually saw — which is the recipient's."""
    a, b = _pair(world, clock, store)
    dispatch(a, ".tell Wren the rain stopped")
    line = ">> Corvid says: the rain stopped"
    assert store.read("Corvid") == [line]
    assert store.read("Wren") == [line]


def test_remote_is_buffered_in_its_own_wording(world, clock, store):
    """Each side holds what THAT side saw — which, for a remote, differs: the
    sender's copy carries the '(to Name)' tag and the recipient's does not."""
    a, b = _pair(world, clock, store)
    dispatch(a, ".remote Wren waves")
    assert store.read("Corvid") == [">> (to Wren) Corvid waves"]
    assert store.read("Wren") == [">> Corvid waves"]


def test_a_self_remote_is_held_once(world, clock, store):
    a, _ = _pair(world, clock, store)
    dispatch(a, ".remote Corvid waves")
    assert store.read("Corvid") == [">> (to Corvid) Corvid waves"]


def test_a_self_tell_is_held_once(world, clock, store):
    a, _ = _pair(world, clock, store)
    dispatch(a, ".tell Corvid a note to self")
    assert store.read("Corvid") == [">> Corvid says: a note to self"]


def test_revtell_renders_oldest_first_with_a_header(world, clock, store):
    a, b = _pair(world, clock, store)
    for n, text in enumerate(("first", "second", "third")):
        clock.t += 5
        dispatch(a, f".tell Wren {text}")
    out = _revtell(a)
    assert REVTELL_HEADER in out
    body = [ln for ln in out.split("\n") if ln.startswith(">> Corvid")]
    assert body == [">> Corvid says: first", ">> Corvid says: second",
                    ">> Corvid says: third"]


def test_empty_buffer_is_quiet(world, clock, store):
    a, _ = _pair(world, clock, store)
    assert _revtell(a) == NOTHING_YET


def test_rolls_at_twenty(world, clock, store):
    a, b = _pair(world, clock, store)
    for i in range(CAP + 4):
        clock.t += 5
        dispatch(a, f".tell Wren line {i}")
    assert store.count("Corvid") == CAP == 20
    lines = store.read("Corvid")
    assert "line 0" not in " ".join(lines) and "line 3" not in " ".join(lines)
    assert lines[-1] == f">> Corvid says: line {CAP + 3}"


def test_the_cap_comes_from_config(tmp_path, world, clock):
    """v1.29.4: the held count is `[world] revtell_count`, not a constant — and
    the agreement quotes the same key, so the document and the behaviour cannot
    describe two different numbers. CAP is only the fallback now."""
    store = RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key", cap=3)
    a, b = _pair(world, clock, store)
    for i in range(6):
        clock.t += 5
        dispatch(a, f".tell Wren line {i}")
    assert store.count("Corvid") == 3
    assert store.read("Corvid")[-1] == ">> Corvid says: line 5"
    assert "line 2" not in " ".join(store.read("Corvid"))


def test_the_server_wires_the_config_value_through(tmp_path):
    """The seam that would silently rot: a config key nothing reads."""
    cfg = Config(
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
        revtell_count=7,
    )
    assert TalkerServer(cfg).revtells.cap == 7


def test_a_muted_recipient_holds_nothing(world, clock, store):
    """A line they never saw is never held for them — the sender still keeps
    their own copy, because they did see theirs."""
    a, b = _pair(world, clock, store)
    dispatch(b, ".mute Corvid")
    dispatch(a, ".tell Wren unheard")
    assert store.count("Wren") == 0
    assert store.read("Corvid") == [">> Corvid says: unheard"]


def test_keeper_calls_are_not_buffered(world, clock, store):
    wiz = FakeSession(world, name="Wizzy", clock=clock, level="wizard", revtells=store)
    a = FakeSession(world, name="Corvid", clock=clock, revtells=store)
    dispatch(a, ".call_staff the ceiling is falling")
    assert store.count("Corvid") == 0
    assert store.count("Wizzy") == 0


def test_an_ordinary_tell_to_a_keeper_is_buffered(world, clock, store):
    wiz = FakeSession(world, name="Wizzy", clock=clock, level="wizard", revtells=store)
    a = FakeSession(world, name="Corvid", clock=clock, revtells=store)
    dispatch(a, ".tell Wizzy a quiet word")
    assert store.read("Wizzy") == [">> Corvid says: a quiet word"]   # it IS a tell


# ---- at rest -------------------------------------------------------------

def test_encrypted_at_rest(tmp_path, world, clock):
    """⚠ Reads the per-account files (v1.52.5) instead of the single
    `revtells.json`. What it checks is unchanged: the line is a token, the owner
    is plaintext, and a timestamp is the only other thing stored."""
    from heresay import boxfiles
    store = RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key")
    a, b = _pair(world, clock, store)
    canary = "zzqqxx-revtell-canary-42"
    dispatch(a, f".tell Wren {canary}")

    d = boxfiles.box_dir(tmp_path / "revtells.json")
    raw = b"".join(p.read_bytes() for p in d.rglob("*") if p.is_file())
    assert canary.encode() not in raw                # the line itself is a token
    boxes = dict(boxfiles.read_boxes(d))
    # Plaintext: WHOSE buffer — the filename now, decoded back here.
    assert sorted(boxes) == ["corvid", "wren"]
    for data in boxes.values():
        for e in data["entries"]:
            assert set(e) == {"ts", "blob"}          # plaintext: only a timestamp
            assert isinstance(e["ts"], (int, float))


def test_survives_a_restart(tmp_path, world, clock):
    store = RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key")
    a, b = _pair(world, clock, store)
    dispatch(a, ".tell Wren pick this up later")
    # A fresh store over the same files is what a restart looks like.
    fresh = RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key")
    line = ">> Corvid says: pick this up later"
    assert fresh.read("Corvid") == [line] and fresh.read("Wren") == [line]


def test_case_folded_owner_key(store):
    store.record("CoRvId", "a line")
    assert store.read("corvid") == ["a line"]
    assert store.count("CORVID") == 1


def test_purge_backstop_drops_old_entries(tmp_path):
    now = 1_000_000_000.0
    store = RevTellStore(tmp_path / "r.json", tmp_path / "mail.key",
                         purge_days=180, wall_clock=lambda: now)
    store.record("Corvid", "ancient", timestamp=now - 181 * 86400)
    store.record("Corvid", "recent", timestamp=now - 1 * 86400)
    assert store.purge() == 1
    assert store.read("Corvid") == ["recent"]
    fresh = RevTellStore(tmp_path / "r.json", tmp_path / "mail.key")
    assert fresh.read("Corvid") == ["recent"]        # the purge was written through


def test_purge_never_decrypts():
    """Like .mailpurge: it drops entries by timestamp, never opening one."""
    src = inspect.getsource(RevTellStore.purge)
    body = src.split('"""')[-1]                  # the code, minus the docstring
    assert "decrypt" not in body and "self.read(" not in body


# ---- exactly one read path ----------------------------------------------

def _command_layer_source() -> str:
    """The source of EVERY module in the command package, concatenated.

    ⚠ `inspect.getsource(C)` returns `commands/__init__.py` alone — and that file
    contains no `.read(` at all, so the sweep below was passing vacuously while
    the three real reads sat in social.py, info.py and staff.py. Widened in
    v1.28.0 when these tests were re-pointed. The guarantee is about the whole
    command layer, so the scan has to be.
    """
    import pkgutil
    import heresay.commands as pkg
    parts = [inspect.getsource(pkg)]
    for mod in pkgutil.iter_modules(pkg.__path__):
        parts.append(inspect.getsource(
            importlib.import_module(f"heresay.commands.{mod.name}")))
    return "\n".join(parts)


def test_no_other_reader_of_revtells_at_any_rank():
    """Structural, like the mail rule: the ONLY read is the owner's own.

    Mirrors test_levels.test_no_mail_reading_power_at_any_level — every
    ``.read(...)`` in the command layer must name the caller themselves.
    """
    src = _command_layer_source()
    for recv, arg in re.findall(r"(\w+)\.read\(([^)]*)\)", src):
        if recv in EVENT_STORE_READERS:      # the operational log, never content
            continue
        assert _reads_only_shared_or_own(arg), \
            f"unexpected read: {recv}.read({arg})"
    # No command reachable above the floor touches another's buffer. Read from
    # the CODE's seed (`privileged_commands()`), never from state.json — an
    # operator re-assigning a rank must not be able to switch this check off.
    privileged = C.privileged_commands()
    assert privileged, "the privileged set must never be empty — see v1.28.0"
    for verb in sorted(privileged):
        handler = C._COMMANDS[verb]
        assert "revtell" not in inspect.getsource(handler), verb
    # And the one arch mail power leaves it alone entirely.
    assert "revtell" not in inspect.getsource(C.cmd_mailpurge)


def test_mailpurge_does_not_touch_review_buffers(world, clock, tmp_path):
    from heresay.accounts import AccountStore
    from heresay.mail import MailStore
    accounts = AccountStore(tmp_path / "a.json")
    accounts.create("Wren", "pw")
    mail = MailStore(tmp_path / "m.json", tmp_path / "mail.key")
    store = RevTellStore(tmp_path / "r.json", tmp_path / "mail.key")
    arch = FakeSession(world, name="Generic", clock=clock, level="arch",
                       accounts=accounts, mail=mail, revtells=store)
    b = FakeSession(world, name="Wren", clock=clock, accounts=accounts,
                    mail=mail, revtells=store)
    dispatch(arch, ".tell Wren something private")
    assert store.count("Wren") == 1
    mail.deliver("Wren", "Generic", "spam")
    # ⚠ `.mailpurge` stages behind the caller's own password since v1.41.0, so
    # the purge itself is the store call the server half makes. What this test
    # is about is unchanged: purging a MAILBOX must not touch a .revtell buffer.
    mail.purge_mailbox("Wren")
    assert mail.count("Wren") == 0            # the mailbox went
    assert store.count("Wren") == 1           # the review buffer did not


def test_the_store_has_no_read_someone_elses_shape():
    """read() takes an owner and nothing else — there is no shape to misuse."""
    params = list(inspect.signature(RevTellStore.read).parameters)
    assert params == ["self", "owner"]


# ---- cross-door: a real server, .deleteme, and the wire ------------------

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
    )
    srv = TalkerServer(cfg)
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1], tmp_path
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
    await _send(w, pw); await _collect(r)
    await _send(w, "y"); await _collect(r, timeout=1.5)
    return r, w


async def _login(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name); await _collect(r); await _send(w, pw)
    await _collect(r, timeout=1.0)
    return r, w


@pytest.mark.asyncio
async def test_revtell_across_a_disconnect(server):
    """The boss-walks-in case: drop the connection, come back, pick it up."""
    srv, port, tmp = server
    ra, wa = await _new(port, "Corvid", "corvidpw")
    rb, wb = await _new(port, "Wren")
    await _collect(ra)

    await _send(wa, ".tell Wren meet me by the oak")
    await _collect(rb)
    wb.close()                                   # Wren's lid slams shut
    await asyncio.sleep(0.2)

    rb2, wb2 = await _login(port, "Wren")        # ...and comes back
    await _send(wb2, ".revtell")
    out = await _collect(rb2, timeout=1.0)
    assert ">> Corvid says: meet me by the oak" in out
    assert REVTELL_HEADER in out

    for w in (wa, wb2):
        w.close()


@pytest.mark.asyncio
async def test_deleteme_removes_the_review_buffer(server):
    srv, port, tmp = server
    ra, wa = await _new(port, "Corvid", "corvidpw")
    rb, wb = await _new(port, "Wren")
    await _send(wa, ".tell Wren something to forget")
    await _collect(ra); await _collect(rb)
    assert srv.revtells.count("Corvid") == 1

    await _send(wa, ".deleteme")
    await _collect(ra)
    await _send(wa, "corvidpw")
    await _collect(ra, timeout=1.5)
    assert not srv.accounts.exists("Corvid")
    assert srv.revtells.count("Corvid") == 0      # gone with the account
    assert srv.revtells.count("Wren") == 1        # Wren's own copy is Wren's
    wa.close(); wb.close()


# ---- where the file lands when nobody configured it ---------------------

def test_default_path_sits_beside_the_mailboxes_not_the_config():
    """An operator upgrading without touching their config must not end up
    writing state next to config.toml in /etc — where the daemon has no
    business writing, and usually no permission to."""
    cfg = Config(mail_file="/var/lib/heresay/mailboxes.json",
                 base_dir=Path("/etc/heresay"))
    assert cfg.revtell_path() == Path("/var/lib/heresay/revtells.json")


def test_an_explicit_revtell_file_always_wins():
    cfg = Config(mail_file="/var/lib/heresay/mailboxes.json",
                 revtell_file="/srv/elsewhere/rt.json",
                 base_dir=Path("/etc/heresay"))
    assert cfg.revtell_path() == Path("/srv/elsewhere/rt.json")


def test_the_server_uses_that_path(tmp_path):
    cfg = Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "state" / "mailboxes.json"),
        mail_key_file=str(tmp_path / "state" / "mail.key"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Generic",
    )   # NOTE: revtell_file deliberately left at its default
    srv = TalkerServer(cfg)
    assert srv.revtells.path == tmp_path / "state" / "revtells.json"


# ---- replay renders exactly as the tell was delivered --------------------

from heresay.commands import REVTELL_HEADER as _RTH        # noqa: E402,F811
from heresay.session import Session as _Session            # noqa: E402
from heresay.world import Resident as _Resident, load_world as _load_world  # noqa: E402
from conftest import FakeClock as _FakeClock, RecordingTransport as _RT     # noqa: E402

LONG_TELL = ("meet me by the oak when the rain stops, and bring the lantern "
             "because the path under the copse is darker than you remember")


def _real_pair(store, wrap=78):
    """Two real Sessions over recording transports, sharing one world."""
    world = _load_world(WORLD, "atrium", "holding_cell")
    out = []
    for name in ("Corvid", "Wren"):
        tr = _RT()
        s = _Session(tr, world, None, Config(wrap_columns=wrap),
                     clock=_FakeClock(), wall_clock=lambda: 0.0, revtells=store)
        s.resident = _Resident(name, "", world.start_room)
        s.resident.session = s
        world.add_resident(s.resident)
        out.append((s, tr))
    return out


def _lines(tr):
    return tr.text.strip("\n").split("\n")


def test_replayed_tell_is_identical_to_what_the_recipient_saw_live(store):
    (a, _), (b, btr) = _real_pair(store)
    dispatch(a, f".tell Wren {LONG_TELL}")
    live = _lines(btr)
    assert len(live) > 1, "fixture must be long enough to wrap"
    assert all(len(ln) <= 78 for ln in live)

    btr.frames.clear()
    dispatch(b, ".revtell")
    replay = _lines(btr)
    assert replay[0] == _RTH
    assert replay[1] == ""
    assert replay[2:] == live               # same text, same wrap points


def test_replayed_tell_matches_the_senders_own_view_too(store):
    (a, atr), (b, _) = _real_pair(store)
    dispatch(a, f".tell Wren {LONG_TELL}")
    live = _lines(atr)                      # ">> Corvid says: ..." as the sender saw it
    assert len(live) > 1
    atr.frames.clear()
    dispatch(a, ".revtell")
    assert _lines(atr)[2:] == live


def test_the_remote_tag_counts_in_the_wrap_and_replays_the_same(store):
    """'(to Wren) ' is text like any other: it wraps at 78 live, and the replay
    reproduces the sender's line — tag, wrap points and all."""
    (a, atr), (b, btr) = _real_pair(store)
    dispatch(a, f".remote Wren {LONG_TELL}")
    live = _lines(atr)
    assert live[0].startswith(">> (to Wren) Corvid ")
    assert len(live) > 1 and all(len(ln) <= 78 for ln in live)
    assert all(len(ln) <= 78 for ln in _lines(btr))
    atr.frames.clear()
    dispatch(a, ".revtell")
    assert _lines(atr)[2:] == live


def test_no_replayed_revtell_line_exceeds_the_wrap_width(store):
    (a, atr), (b, _) = _real_pair(store)
    for i in range(3):
        dispatch(a, f".tell Wren {i} {LONG_TELL}")
    atr.frames.clear()
    dispatch(a, ".revtell")
    for ln in _lines(atr):
        assert len(ln) <= 78, ln


def test_revtell_block_is_one_unit_with_no_internal_blanks(store):
    (a, atr), (b, _) = _real_pair(store)
    dispatch(a, f".tell Wren {LONG_TELL}")
    atr.frames.clear()
    dispatch(a, ".revtell")
    assert len(atr.frames) == 1
    assert atr.text.startswith("\n")
    assert "\n\n\n" not in atr.text
    assert "" not in _lines(atr)[2:]


def test_the_store_still_holds_the_line_unwrapped(store):
    """The wrap is a render-time property: the buffer keeps one logical line."""
    (a, _), (b, _) = _real_pair(store)
    dispatch(a, f".tell Wren {LONG_TELL}")
    held = store.read("Wren")
    assert len(held) == 1
    assert "\n" not in held[0]
    assert len(held[0]) > 78                # longer than a screen line, on purpose
