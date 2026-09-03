# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Levels: promote/demote persist, arch-from-config, invisibility of privilege."""

from pathlib import Path

import importlib
import inspect
import re

import pytest

import heresay.commands as C
from heresay.accounts import Account, AccountStore
from heresay.commands import dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    store.create("Wren", "pw")
    return store


def test_promote_demote_persist(world, accounts, clock):
    arch = FakeSession(world, name="Generic", clock=clock, accounts=accounts, level="arch")
    wren = FakeSession(world, name="Wren", clock=clock, accounts=accounts, level="resident")
    dispatch(arch, ".promote Wren")
    assert accounts.get("Wren").rank == "staff"    # persisted to accounts.json
    assert wren.resident.rank == "staff"           # live resident updated
    dispatch(arch, ".demote Wren")
    assert accounts.get("Wren").rank == "resident"
    assert wren.resident.rank == "resident"


def test_resident_cannot_use_wizard_commands(world, clock):
    r = FakeSession(world, name="Res", clock=clock, level="resident")
    for cmd in (".gag Bob", ".jail Bob", ".boot Bob", ".ban Bob", ".staffmail"):
        r.clear()
        dispatch(r, cmd)
        verb = cmd.split()[0][1:]
        assert f"Unknown command: .{verb}" in r.text   # a typo's answer, not a hint


def test_resident_cannot_use_arch_commands(world, clock):
    r = FakeSession(world, name="Res", clock=clock, level="resident")
    for cmd in (".promote Bob", ".demote Bob", ".mailpurge Bob"):
        r.clear()
        dispatch(r, cmd)
        verb = cmd.split()[0][1:]
        assert f"Unknown command: .{verb}" in r.text


def test_wizard_cannot_use_arch_commands(world, clock):
    w = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    dispatch(w, ".promote Bob")
    assert "Unknown command: .promote" in w.text   # arch-only invisible to a wizard
    w.clear()
    dispatch(w, ".wizhelp")                          # old name is gone entirely
    assert "Unknown command: .wizhelp" in w.text


def test_arch_named_in_config_overrides_stored_rank(tmp_path):
    cfg = Config(
        owner_name="Generic",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
    )
    srv = TalkerServer(cfg)
    # Stored at the floor, but the top rank by config name.
    arch_acct = Account(name="Generic", password_hash="x", rank="resident")
    assert srv._rank_for(arch_acct) == "owner"
    # A stored staff stays a staff; a plain account stays a resident; a rank
    # the ladder does not know reads as the floor.
    assert srv._rank_for(Account(name="Someone", password_hash="x",
                                 rank="staff")) == "staff"
    assert srv._rank_for(Account(name="Plain", password_hash="x")) == "resident"
    assert srv._rank_for(Account(name="Odd", password_hash="x",
                                 rank="wizard")) == "resident"


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


# The receivers whose `.read(` is NOT a content read, and may therefore skip the
# owner-only sweep below. Exactly one: the operational EVENT store (v1.30.0),
# which by construction holds no message content at all — a closed set of twelve
# fact types, guarded by its own covenant tests in test_events.py (a field-name
# scan and a spoken canary). The sweep stays deliberately broad for everything
# else: any OTHER `.read(` in the command layer must still name the caller.
EVENT_STORE_READERS = frozenset({"events"})

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


def test_the_privileged_set_still_equals_the_pre_1_28_gate_sets():
    """The covenant tests read `privileged_commands()` now (v1.28.0) instead of
    the two hardcoded sets. Pinned here so the source they read cannot quietly
    stop meaning "every command above the floor"."""
    assert C.privileged_commands() == frozenset({
        # the pre-1.28 _WIZARD_VERBS, less .keeperhelp (removed in v1.29.0)
        "gag", "ungag", "jail", "free", "boot", "ban", "unban",
        "staffmail", "dstaffmail",
        # the pre-1.28 _ARCH_VERBS, less .caretakerhelp
        "accounts", "promote", "demote", "mailpurge", "newusers", "logins",
        # `.ownerset` -> `.ownerset` (v1.54.0), with `.ownerset` kept as
        # a dispatch alias — the CANONICAL name is what privileged_commands lists.
        "logoutall", "ownerset", "deleteuser", "resetpassword",
        # Added since, each named deliberately rather than folded in silently:
        "staff_actions",                        # v1.30.0, arch
        "ban_ip", "unban_ip", "bans",           # v1.32.0 / v1.33.0, staff
        "staff_calls",                          # v1.35.0, staff
        "security",                             # v1.35.0, arch
        "load_report",                          # v1.36.0, staff
        "read_report",                          # v1.50.0, arch — the report box
    })


def test_no_mail_reading_power_at_any_level():
    """Structural guarantee: nothing reads another account's PRIVATE mailbox.

    The only reads are the owner reading their own (session.resident.name) and
    the SHARED boxes — the staff letterbox and, since v1.50.0, the report box.
    Both are addressed-and-consented mail to a COLLECTIVE, read by the people it
    was addressed to; neither is a private box, and neither CAN be one (see
    SHARED_BOXES and its own test — a '#' key cannot be an account name).
    The sole arch mail power is .mailpurge, which deletes without reading.

    ⚠ v1.50.0 widened the allowlist from one shared box to two, and widened the
    CHECK at the same time so the widening is not a loosening: the permitted
    keys are now required to be structurally incapable of belonging to a person,
    rather than simply named here. The guarantee — nobody reads anyone's private
    mail — is unchanged.

    ⚠ The purge MOVED in v1.41.0 — the command stages and `server._mailpurge`
    does the work behind the caller's own password — so this scans BOTH halves.
    Following it was the whole point: a guarantee whose test stops covering the
    code it is about becomes a guarantee about nothing (the v1.28.0 lesson,
    where `.read(` was being swept over a file that contained none).
    """
    from heresay.server import TalkerServer
    src = _command_layer_source()
    for recv, arg in re.findall(r"(\w+)\.read\(([^)]*)\)", src):
        if recv in EVENT_STORE_READERS:
            continue                # see EVENT_STORE_READERS
        assert _reads_only_shared_or_own(arg), \
            f"unexpected mail read: {recv}.read({arg})"
    assert "mailpurge" in C.privileged_commands()
    # The handler validates and stages; the server half purges. Neither reads.
    handler_src = inspect.getsource(C.cmd_mailpurge)
    server_src = inspect.getsource(TalkerServer._mailpurge)
    assert "purge_mailbox" in server_src
    assert ".read(" not in handler_src
    assert ".read(" not in server_src
    # ...and the server half is reached ONLY through the staged sentinel, so the
    # password gate cannot be stepped around.
    assert "pending_target" in handler_src and "pending_target" in server_src


# ---- one generated screen, sectioned by rank (v1.29.0) --------------------
#
# `.keeperhelp` / `.caretakerhelp` are gone. There is ONE `.help`, generated from
# the registry crossed with the command table: a section per rank from the floor
# up to the caller's. A resident sees one section; each higher rank adds its own.

def test_the_two_staff_verbs_are_gone_at_every_rank(world, clock):
    for level in ("resident", "wizard", "arch"):
        s = FakeSession(world, name="U" + level, clock=clock, level=level)
        for cmd in (".keeperhelp", ".caretakerhelp"):
            s.clear()
            dispatch(s, cmd)
            assert f"Unknown command: {cmd}" in s.text, (level, cmd)


def test_a_resident_sees_only_the_floor_section(world, clock):
    r = FakeSession(world, name="Res", clock=clock, level="resident")
    dispatch(r, ".help")
    for verb in (".gag", ".jail", ".promote", ".deleteuser", ".staffmail"):
        assert verb not in r.text, verb
    assert ".look" in r.text                       # ...but their own commands are
    assert "[ " not in r.text                      # one section: no rank headers


def test_a_keeper_gains_the_keeper_section(world, clock):
    w = FakeSession(world, name="Wiz", clock=clock, level="wizard")
    dispatch(w, ".help")
    assert "[ resident ]" in w.text and "[ staff ]" in w.text
    assert "[ owner ]" not in w.text
    for verb in (".gag", ".jail", ".boot", ".ban", ".staffmail"):
        assert verb in w.text, verb
    for verb in (".promote", ".mailpurge", ".deleteuser", ".resetpassword"):
        assert verb not in w.text, verb


def test_the_arch_sees_every_section(world, clock):
    a = FakeSession(world, name="Boss", clock=clock, level="arch")
    dispatch(a, ".help")
    for header in ("[ resident ]", "[ staff ]", "[ owner ]"):
        assert header in a.text, header
    for verb in (".gag", ".jail", ".staffmail",                     # staff tier
                 ".promote", ".mailpurge", ".newusers", ".logoutall",
                 ".ownerset", ".deleteuser", ".resetpassword"):     # arch tier
        assert verb in a.text, verb


def test_the_mail_power_line_survives_the_screens_it_closed(world, clock):
    """The covenant line naming what staff CANNOT do outlived the two screens:
    it is shown to anyone who can see a section above the floor, and to nobody
    else — a resident has no staff powers to be told the limits of."""
    r = FakeSession(world, name="Res", clock=clock, level="resident")
    dispatch(r, ".help")
    assert "can read anyone's mail" not in r.text
    for level in ("wizard", "arch"):
        s = FakeSession(world, name="S" + level, clock=clock, level=level)
        dispatch(s, ".help")
        assert "can read anyone's mail but their own" in s.text, level
        assert "logged as facts" in s.text, level


# ---- .help <topic> is gated by the same tiers -----------------------------

def test_help_topic_gating_by_tier(world, clock, tmp_path):
    from heresay.server import _load_help_topics
    from pathlib import Path
    topics = _load_help_topics(Path(__file__).resolve().parent / "fixtures" / "house" / "help")

    def sess(level):
        return FakeSession(world, name="X" + level, clock=clock, level=level,
                           help_topics=topics)

    # A resident may read a resident page and neither staff tier's.
    r = sess("resident")
    dispatch(r, ".help who")
    assert "who" in r.text and "Unknown command" not in r.text
    for topic in ("gag", "deleteuser", "ownerset"):
        r.clear()
        dispatch(r, f".help {topic}")
        assert f"Unknown command: .{topic}" in r.text, topic

    # A staff may read staff pages, but not the owner's.
    w = sess("wizard")
    w.clear()
    dispatch(w, ".help gag")
    assert "take someone's voice" in w.text
    for topic in ("deleteuser", "resetpassword", "ownerset", "promote"):
        w.clear()
        dispatch(w, f".help {topic}")
        assert f"Unknown command: .{topic}" in w.text, topic

    # The arch may read everything.
    a = sess("arch")
    # The page is `ownerset.txt` now; `.help ownerset` (the alias) has no page.
    for topic in ("who", "gag", "deleteuser", "resetpassword", "ownerset"):
        a.clear()
        dispatch(a, f".help {topic}")
        assert "Unknown command" not in a.text, topic
        assert "No additional help available." not in a.text, topic
