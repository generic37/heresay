# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Mail: send/read/delete, encryption at rest, caps, purge, rate limiting."""

import json
import os
import stat

import pytest

from heresay import boxfiles
from heresay.accounts import AccountStore
from heresay.commands import MAIL_FLOOD_MSG, dispatch
from heresay.config import Config
from heresay.mail import MailStore

from conftest import FakeClock, FakeSession


# ---- reading the store on disk (v1.52.5: one file per box) ---------------
#
# The single `mailboxes.json` became one file per mailbox, so the tests that
# check what reaches the DISK read it there instead. What they assert is
# unchanged — same fields, same plaintext/ciphertext split, same canaries.

def _box_dir(tmp_path):
    return boxfiles.box_dir(tmp_path / "mailboxes.json")


def _stored(tmp_path, key):
    """One mailbox's file, as JSON."""
    return json.loads(boxfiles.box_path(_box_dir(tmp_path), key.casefold())
                      .read_text(encoding="utf-8"))


def _store_bytes(tmp_path):
    """Every byte the mail store has written — the canary sweep's haystack."""
    return b"".join(p.read_bytes() for p in _box_dir(tmp_path).rglob("*")
                    if p.is_file())


def _box_keys(tmp_path):
    """Which mailboxes exist on disk, by decoding the filenames."""
    return sorted(k for k, _ in boxfiles.read_boxes(_box_dir(tmp_path)))


def _write_legacy(tmp_path, mailboxes, version=2):
    """Write a genuine pre-v1.52.5 single-file store and remove the split one,
    so the next load takes the MIGRATION path — which is the only thing that
    still reads this shape."""
    import shutil
    shutil.rmtree(_box_dir(tmp_path), ignore_errors=True)
    (tmp_path / "mailboxes.json").write_text(
        json.dumps({"version": version, "mailboxes": mailboxes}),
        encoding="utf-8")


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Corvid", "pw1", description="")
    store.create("Wren", "pw2", description="")
    return store


def _mail(tmp_path, cap=50, purge_days=180, wall=None):
    return MailStore(
        tmp_path / "mailboxes.json",
        tmp_path / "mail.key",
        cap=cap,
        purge_days=purge_days,
        wall_clock=wall or (lambda: 1_720_000_000.0),
    )


def _session(world, accounts, mail, name="Corvid", clock=None, config=None):
    return FakeSession(world, name=name, clock=clock or FakeClock(),
                       accounts=accounts, mail=mail,
                       config=config or Config(wrap_columns=1000))


# ---- send / read ---------------------------------------------------------

def test_one_line_send_and_read(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    sender = _session(world, accounts, mail, name="Corvid")
    dispatch(sender, ".mail Wren the door is unlocked")
    assert "Mail sent to Wren." in sender.text

    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".mail")
    assert "*** You have 1 message *" in reader.text
    assert "from Corvid" in reader.text
    assert "the door is unlocked" in reader.text


def test_empty_mailbox(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".mail")
    assert "No mail." in reader.text


def test_editor_compose(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    sender = _session(world, accounts, mail, name="Corvid")
    dispatch(sender, ".mail Wren")
    assert sender.in_editor()
    sender.feed_editor("Dear Wren,")
    sender.feed_editor("meet me in the reading room at dusk.")
    sender.feed_editor(".end")
    assert not sender.in_editor()

    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".mail")
    assert "Dear Wren," in reader.text
    assert "meet me in the reading room at dusk." in reader.text


def test_mail_to_unknown(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    sender = _session(world, accounts, mail, name="Corvid")
    dispatch(sender, ".mail Nobody hello?")
    assert "no one called Nobody" in sender.text


# ---- persistence & deletion ---------------------------------------------

def test_survives_restart(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    _session(world, accounts, mail, name="Corvid")  # ensure sender exists
    mail.deliver("Wren", "Corvid", "persistent words")

    # A fresh MailStore over the same files == a server restart.
    reloaded = _mail(tmp_path)
    msgs = reloaded.read("Wren")
    assert len(msgs) == 1 and msgs[0].body == "persistent words"


def test_dmail_removes_from_disk(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "delete-me-please")
    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".dmail 1")
    assert "Message 1 deleted." in reader.text

    # Gone from memory and from a fresh reload of the file.
    assert mail.count("Wren") == 0
    assert _mail(tmp_path).count("Wren") == 0
    assert b"delete-me-please" not in _store_bytes(tmp_path)


def test_dmail_bad_index(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".dmail 5")
    assert "No such message." in reader.text


# ---- encryption at rest --------------------------------------------------

def test_body_encrypted_at_rest(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "SECRET-CANARY-9f3a body text")

    raw = _store_bytes(tmp_path)
    assert b"SECRET-CANARY-9f3a" not in raw     # body not on disk in plaintext
    assert b"Corvid" not in raw                 # sender-name field encrypted too
    # ⚠ The recipient is STILL plaintext, as the FILENAME rather than a JSON
    # key (v1.52.5). Hex is reversible, so a directory listing names the box
    # owners exactly as opening the old file did — the same disclosure,
    # differently spelled, which is why this assertion moved rather than went.
    assert _box_keys(tmp_path) == ["wren"]

    # ...but it is readable through the store.
    msgs = mail.read("Wren")
    assert msgs[0].body == "SECRET-CANARY-9f3a body text"
    assert msgs[0].sender == "Corvid"


def test_key_file_is_0600(world, tmp_path, accounts):
    _mail(tmp_path)
    mode = stat.S_IMODE(os.stat(tmp_path / "mail.key").st_mode)
    assert mode == 0o600


# ---- caps ----------------------------------------------------------------

def test_mailbox_cap(world, tmp_path, accounts):
    mail = _mail(tmp_path, cap=2)
    sender = _session(world, accounts, mail, name="Corvid",
                      config=Config(wrap_columns=1000, mail_send_burst=100))
    dispatch(sender, ".mail Wren one")
    dispatch(sender, ".mail Wren two")
    sender.clear()
    dispatch(sender, ".mail Wren three")
    assert "Their mailbox is full." in sender.text
    assert mail.count("Wren") == 2   # nothing stored beyond the cap


# ---- purge ---------------------------------------------------------------

def test_purge_old_messages(world, tmp_path):
    now = 1_720_000_000.0
    mail = _mail(tmp_path, purge_days=180, wall=lambda: now)
    day = 86400
    mail.deliver("Wren", "Corvid", "ancient", timestamp=now - 200 * day)
    mail.deliver("Wren", "Corvid", "recent", timestamp=now - 5 * day)
    removed = mail.purge(now)
    assert removed == 1
    bodies = [m.body for m in mail.read("Wren")]
    assert bodies == ["recent"]


def test_purge_at_reload(world, tmp_path):
    now = 1_720_000_000.0
    day = 86400
    mail = _mail(tmp_path, purge_days=180, wall=lambda: now)
    mail.deliver("Wren", "Corvid", "old-one", timestamp=now - 300 * day)
    # A reload followed by purge (as the server does at startup) clears it.
    reloaded = _mail(tmp_path, purge_days=180, wall=lambda: now)
    assert reloaded.purge() == 1
    assert reloaded.count("Wren") == 0


# ---- rate limiting -------------------------------------------------------

def test_mail_send_rate_bucket(world, tmp_path, accounts):
    # Fixed clock: burst of 3 sends, the 4th is refused (sender-only, no escalate).
    clock = FakeClock(100.0)
    cfg = Config(wrap_columns=1000, mail_send_burst=3, mail_send_refill_per_sec=0.05)
    mail = _mail(tmp_path)
    sender = _session(world, accounts, mail, name="Corvid", clock=clock, config=cfg)
    for _ in range(3):
        dispatch(sender, ".mail Wren hi")
    sender.clear()
    dispatch(sender, ".mail Wren hi")
    assert MAIL_FLOOD_MSG in sender.text
    assert sender._closing is False   # mail flooding never escalates to disconnect


def test_has_mail(world, tmp_path):
    mail = _mail(tmp_path)
    assert not mail.has_mail("Wren")
    mail.deliver("Wren", "Corvid", "hi")
    assert mail.has_mail("Wren")


# ---- unread / read (v1.34.0) --------------------------------------------
#
# The login notice used to fire on a NON-EMPTY box, so it kept saying "You have
# mail." after you had read everything. It fires on UNREAD now; the flag is
# plaintext metadata beside the ts, never anywhere near the encrypted body.

def test_delivered_mail_starts_unread(world, tmp_path):
    mail = _mail(tmp_path)
    assert mail.unread_count("Wren") == 0 and not mail.has_unread("Wren")
    mail.deliver("Wren", "Corvid", "hi")
    assert mail.unread_count("Wren") == 1 and mail.has_unread("Wren")
    assert mail.read("Wren")[0].read is False


def test_mark_read_clears_unread_and_is_idempotent(world, tmp_path):
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "one")
    mail.deliver("Wren", "Corvid", "two")
    assert mail.mark_read("Wren") == 2          # both changed
    assert not mail.has_unread("Wren")
    assert mail.mark_read("Wren") == 0          # nothing left to change; no rewrite
    assert mail.count("Wren") == 2              # marking read never deletes


def test_new_mail_after_a_read_is_unread_again(world, tmp_path):
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "one")
    mail.mark_read("Wren")
    mail.deliver("Wren", "Corvid", "two")
    assert mail.unread_count("Wren") == 1       # only the new one


def test_read_flag_survives_a_reload_and_is_plaintext(world, tmp_path):
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "one")
    mail.deliver("Wren", "Corvid", "two")
    mail.mark_read("Wren", [1])                 # 1-based, partial
    raw = _stored(tmp_path, "Wren")
    stored = raw["messages"]
    assert raw["version"] == 2
    assert [m["read"] for m in stored] == [True, False]   # plaintext, per message
    assert all("blob" in m for m in stored)               # bodies still encrypted

    reloaded = _mail(tmp_path)
    assert reloaded.unread_count("Wren") == 1
    assert [m.read for m in reloaded.read("Wren")] == [True, False]


def _downgrade_to_v1(tmp_path):
    """Rebuild the store as a genuine pre-v2 SINGLE FILE: no `read` anywhere.

    ⚠ Since v1.52.5 the live store is one file per box, so a pre-v2 store is
    reached by writing the old shape back out and clearing the split files —
    which is exactly the state an upgrading house is in, and the only state in
    which the pre-v2 reader still runs.
    """
    boxes = {key: [{"ts": m["ts"], "blob": m["blob"]}       # no `read` field
                   for m in _stored(tmp_path, key)["messages"]]
             for key in _box_keys(tmp_path)}
    _write_legacy(tmp_path, boxes, version=1)


def test_pre_v2_messages_migrate_as_UNREAD(world, tmp_path):
    """The migration choice (v1.34.1, reversing v1.34.0): an existing message
    has no flag, so whether it was read is genuinely NOT KNOWN — and a flag
    that tracks read state may not claim otherwise. Guessing "read" would hide
    genuinely unread mail from the person it was addressed to; guessing the
    other way costs one more notice, which reading clears."""
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "legacy")
    _downgrade_to_v1(tmp_path)

    reloaded = _mail(tmp_path)                   # load migrates
    assert reloaded.has_mail("Wren")             # still there, still readable
    assert reloaded.read("Wren")[0].body == "legacy"
    assert reloaded.has_unread("Wren")           # ...and it is announced
    on_disk = _stored(tmp_path, "Wren")          # and the box is stamped v2
    assert on_disk["version"] == 2
    assert on_disk["messages"][0]["read"] is False


def test_a_migrated_message_clears_when_read_and_stays_clear(world, tmp_path, accounts):
    """The notice fires once after the upgrade, and reading it is the end of
    it — including across the restart that a fresh MailStore stands in for."""
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "legacy")
    _downgrade_to_v1(tmp_path)

    reloaded = _mail(tmp_path)
    assert reloaded.has_unread("Wren")           # what the login notice asks
    reader = _session(world, accounts, reloaded, name="Wren")
    dispatch(reader, ".mail")
    assert "legacy" in reader.text
    assert not reloaded.has_unread("Wren")       # cleared by reading it
    assert _mail(tmp_path).has_unread("Wren") is False   # and stays cleared


def test_reading_mail_marks_it_read(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    sender = _session(world, accounts, mail, name="Corvid")
    dispatch(sender, ".mail Wren the door is unlocked")
    assert mail.has_unread("Wren")

    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".mail")
    assert "the door is unlocked" in reader.text
    assert not mail.has_unread("Wren")           # shown is read
    assert mail.count("Wren") == 1               # and still in the box

    dispatch(sender, ".mail Wren and the kettle is on")
    assert mail.unread_count("Wren") == 1        # the new one only


def test_an_empty_read_marks_nothing(world, tmp_path, accounts):
    mail = _mail(tmp_path)
    reader = _session(world, accounts, mail, name="Wren")
    dispatch(reader, ".mail")
    assert "No mail." in reader.text
    assert mail.mark_read("Wren") == 0


def test_mailbox_key_is_casefolded(world, tmp_path):
    mail = _mail(tmp_path)
    mail.deliver("Generic", "Corvid", "note")   # any-case recipient
    # The mailbox key is the FILENAME now; it decodes back to the casefold.
    assert _box_keys(tmp_path) == ["generic"]
    assert mail.count("GENERIC") == 1                       # lookup casefolds


def test_mailbox_migrates_noncasefold_key(world, tmp_path):
    # A legacy file with a mixed-case recipient key is re-keyed as it is split.
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "legacy")
    _write_legacy(tmp_path, {"WREN": _stored(tmp_path, "wren")["messages"]})

    reloaded = _mail(tmp_path)                    # load migrates
    assert reloaded.count("wren") == 1
    assert reloaded.read("wren")[0].body == "legacy"
    assert _box_keys(tmp_path) == ["wren"]        # written out casefolded


# ---- stored bodies are wrapped on read, like live delivery ---------------
#
# Same class of bug as .rev/.revtell replaying through the raw path: anything
# that renders STORED text has to wrap it, or it goes out as one long line and
# the client breaks it wherever the window happens to end.

LONG_BODY = ("the rain finally stopped and the whole orchard smells of wet stone "
             "tonight, which is the sort of thing you only notice standing still")


def _real_reader(world, name, mail, accounts=None, level="resident"):
    from heresay.config import Config
    from heresay.session import Session
    from heresay.world import Resident
    from conftest import FakeClock, RecordingTransport
    tr = RecordingTransport()
    s = Session(tr, world, accounts, Config(wrap_columns=78),
                clock=FakeClock(), wall_clock=lambda: 0.0, mail=mail)
    from conftest import _legacy_rank
    s.resident = Resident(name, "", world.start_room,
                          rank=_legacy_rank(level, s.theme), ladder=s.theme)
    s.resident.session = s
    world.add_resident(s.resident)
    return s, tr


def test_mail_read_wraps_a_long_body_at_78(world, tmp_path):
    from heresay.mail import MailStore
    mail = MailStore(tmp_path / "m.json", tmp_path / "mail.key")
    mail.deliver("Corvid", "Wren", LONG_BODY)
    s, tr = _real_reader(world, "Corvid", mail)
    dispatch(s, ".mail")
    lines = tr.text.strip("\n").split("\n")
    assert any(LONG_BODY.split()[0] in ln for ln in lines)
    assert all(len(ln) <= 78 for ln in lines), [ln for ln in lines if len(ln) > 78]
    # ...and it really did have to wrap, so this is not a vacuous pass.
    assert len(LONG_BODY) > 78
    assert sum(1 for ln in lines if ln.strip()) > 3


def test_keeper_mail_read_wraps_a_long_body_at_78(world, tmp_path):
    from heresay.commands import STAFF_BOX
    from heresay.mail import MailStore
    mail = MailStore(tmp_path / "m.json", tmp_path / "mail.key")
    mail.deliver(STAFF_BOX, "Wren", LONG_BODY)
    s, tr = _real_reader(world, "Wizzy", mail, level="wizard")
    dispatch(s, ".staffmail")
    lines = tr.text.strip("\n").split("\n")
    assert "Staff mail" in lines[0]
    assert all(len(ln) <= 78 for ln in lines), [ln for ln in lines if len(ln) > 78]
    assert any(LONG_BODY.split()[0] in ln for ln in lines)
