# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""⛔ One file per box — the real fix for audit C2 (v1.52.5).

**The bug.** Both stores kept every box in one JSON document and rewrote it —
serialise, write, **fsync** — on every mutation. So one `.tell` cost a function
of how many people had ever used the house, on the event loop, with everyone
else waiting behind it. Measured on the single-file tree, ten messages per box:

    MailStore.deliver()      10 boxes  4.5 ms   1000 boxes  50.1 ms
    RevTellStore.record()    10 boxes  6.1 ms   1000 boxes  50.9 ms

and after the split, the same measurement is flat at ~4.5 ms — the cost of the
one fsync that makes "Mail sent." true, and nothing else.

**What these tests hold in place**, in the order the risk runs:

1. the per-op cost is O(one box) — asserted STRUCTURALLY (how many box files a
   mutation touches), because a timing assertion in a suite is a flake;
2. the migration is lossless, idempotent, and moves ciphertext without ever
   decrypting it;
3. a hostile box key cannot escape the directory;
4. an interrupted write leaves the previous box intact;
5. the modes: 0700 on the directory, 0600 on every file.
"""

import json
import os
import stat
from pathlib import Path

import pytest

from heresay import boxfiles
from heresay.mail import MailStore
from heresay.revtell import RevTellStore


def _mail(tmp_path, **kw):
    return MailStore(tmp_path / "mailboxes.json", tmp_path / "mail.key", **kw)


def _rev(tmp_path, **kw):
    return RevTellStore(tmp_path / "revtells.json", tmp_path / "mail.key", **kw)


def _mail_dir(tmp_path):
    return boxfiles.box_dir(tmp_path / "mailboxes.json")


def _files(directory):
    return sorted(p.name for p in Path(directory).glob("*.json"))


# ---------------------------------------------------------------------------
# 1. The point of the whole exercise: one mutation touches ONE box
# ---------------------------------------------------------------------------

def _count_writes(monkeypatch):
    """Count box-file writes and removals through the one place they happen."""
    seen = {"write": [], "remove": []}
    real_write, real_remove = boxfiles.write_box, boxfiles.remove_box

    def write(directory, key, payload):
        seen["write"].append(key)
        return real_write(directory, key, payload)

    def remove(directory, key):
        seen["remove"].append(key)
        return real_remove(directory, key)

    monkeypatch.setattr(boxfiles, "write_box", write)
    monkeypatch.setattr(boxfiles, "remove_box", remove)
    return seen


@pytest.mark.parametrize("others", [0, 50, 500])
def test_a_delivery_writes_ONE_box_whatever_the_store_holds(tmp_path, monkeypatch,
                                                            others):
    """⛔ THE FIX, stated as an invariant rather than a stopwatch. Before this,
    every delivery rewrote every box; the cost of speaking to one person was a
    function of everybody else."""
    mail = _mail(tmp_path)
    for i in range(others):
        mail.deliver(f"resident{i}", "Corvid", "hello")

    seen = _count_writes(monkeypatch)
    mail.deliver("Wren", "Corvid", "one line for one person")
    assert seen["write"] == ["wren"]
    assert seen["remove"] == []


@pytest.mark.parametrize("others", [0, 50, 500])
def test_a_revtell_record_writes_ONE_buffer(tmp_path, monkeypatch, others):
    """`record()` runs on every tell and remote, for BOTH participants — which
    is where C2 bit hardest."""
    rev = _rev(tmp_path)
    for i in range(others):
        rev.record(f"resident{i}", ">> Corvid says: hello")

    seen = _count_writes(monkeypatch)
    rev.record("Wren", ">> Corvid says: pick this up later")
    assert seen["write"] == ["wren"]


def test_mark_read_delete_and_purge_touch_only_what_changed(tmp_path, monkeypatch):
    mail = _mail(tmp_path, wall_clock=lambda: 1_000_000.0)
    for name in ("Wren", "Corvid", "Cara"):
        mail.deliver(name, "Someone", "hello")

    seen = _count_writes(monkeypatch)
    mail.mark_read("Wren")
    assert seen["write"] == ["wren"]

    seen["write"].clear()
    mail.delete("Corvid", 1)                    # empties Corvid's box
    assert seen["write"] == [] and seen["remove"] == ["corvid"]

    # ⚠ A purge that expires nothing writes NOTHING — a nightly sweep over a
    # quiet house costs no disk at all.
    seen["remove"].clear()
    assert mail.purge(now=1_000_001.0) == 0
    assert seen["write"] == [] and seen["remove"] == []


def test_purge_writes_only_the_boxes_that_lost_something(tmp_path, monkeypatch):
    day = 86400.0
    mail = _mail(tmp_path, wall_clock=lambda: 0.0)
    mail.deliver("Old", "Someone", "ancient", timestamp=0.0)
    mail.deliver("New", "Someone", "recent", timestamp=199 * day)

    seen = _count_writes(monkeypatch)
    removed = mail.purge(now=200 * day)          # retention is 180 days
    assert removed == 1
    assert seen["write"] == []                  # Old emptied entirely...
    assert seen["remove"] == ["old"]            # ...so its file went
    assert mail.count("New") == 1               # and New was never touched


# ---------------------------------------------------------------------------
# 2. The migration: lossless, idempotent, and never decrypts
# ---------------------------------------------------------------------------

LEGACY_MAIL = {
    "version": 2,
    "mailboxes": {
        "wren": [{"ts": 1.0, "read": False, "blob": "BLOB-A"},
                 {"ts": 2.0, "read": True, "blob": "BLOB-B"}],
        "corvid": [{"ts": 3.0, "read": False, "blob": "BLOB-C"}],
        "#staff": [{"ts": 4.0, "read": False, "blob": "BLOB-D"}],
        "#report": [{"ts": 5.0, "read": True, "blob": "BLOB-E"}],
    },
}


def _write_legacy_mail(tmp_path, payload=None):
    (tmp_path / "mailboxes.json").write_text(
        json.dumps(payload or LEGACY_MAIL), encoding="utf-8")


def test_the_legacy_store_is_split_losslessly(tmp_path):
    """Every message, every flag, every timestamp — and the ciphertext byte for
    byte, because nothing here decrypts anything."""
    _write_legacy_mail(tmp_path)
    mail = _mail(tmp_path)

    assert mail.count("Wren") == 2 and mail.count("Corvid") == 1
    assert mail.count("#staff") == 1 and mail.count("#report") == 1
    assert mail.unread_count("Wren") == 1        # the read flag survived
    assert mail.unread_count("#report") == 0

    boxes = dict(boxfiles.read_boxes(_mail_dir(tmp_path)))
    assert sorted(boxes) == ["#report", "#staff", "corvid", "wren"]
    assert [m["blob"] for m in boxes["wren"]["messages"]] == ["BLOB-A", "BLOB-B"]
    assert [m["ts"] for m in boxes["wren"]["messages"]] == [1.0, 2.0]
    assert [m["read"] for m in boxes["wren"]["messages"]] == [False, True]


def test_the_legacy_file_is_RENAMED_not_deleted(tmp_path):
    """⛔ A rollback stays possible. A storage migration must never be the only
    copy of what it moved."""
    _write_legacy_mail(tmp_path)
    _mail(tmp_path)

    assert not (tmp_path / "mailboxes.json").exists()
    retired = tmp_path / "mailboxes.json.pre-split"
    assert retired.exists()
    assert json.loads(retired.read_text()) == LEGACY_MAIL   # untouched, verbatim


def test_a_second_load_is_a_no_op_that_loses_nothing(tmp_path):
    """Idempotent: with no legacy file there is nothing to migrate, and the boxes
    read back exactly as they were left."""
    _write_legacy_mail(tmp_path)
    first = _mail(tmp_path)
    before = {k: first.count(k) for k in ("Wren", "Corvid", "#staff", "#report")}
    files = _files(_mail_dir(tmp_path))

    second = _mail(tmp_path)
    assert {k: second.count(k) for k in before} == before
    assert _files(_mail_dir(tmp_path)) == files
    assert second.unread_count("Wren") == 1


def test_a_restored_legacy_file_beside_split_boxes_is_IGNORED(tmp_path, caplog):
    """⚠ THE PRECEDENCE, and it is the dangerous case: re-importing a restored
    legacy file would resurrect deleted mail and double-count what is still
    there. The split boxes win, the old file is left strictly alone, and the
    operator is told — every boot, because an ambiguous store should not go
    quiet."""
    _write_legacy_mail(tmp_path)
    mail = _mail(tmp_path)
    mail.delete("Wren", 1)                       # Wren is down to one message
    assert mail.count("Wren") == 1

    _write_legacy_mail(tmp_path)                 # somebody restores the old file
    with caplog.at_level("WARNING"):
        reloaded = _mail(tmp_path)

    assert reloaded.count("Wren") == 1           # NOT 2, and not 3
    assert (tmp_path / "mailboxes.json").exists()        # left alone
    assert any("ignoring" in r.message.lower() or "ignoring" in r.getMessage().lower()
               for r in caplog.records)


def test_migration_preserves_a_pre_v2_message_as_unread(tmp_path):
    """The v1.34.1 rule still runs, in the one place that still reads the old
    shape: no `read` field means nobody knows, and the flag may not claim it was
    read."""
    _write_legacy_mail(tmp_path, {"version": 1, "mailboxes": {
        "wren": [{"ts": 1.0, "blob": "BLOB-OLD"}]}})
    mail = _mail(tmp_path)
    assert mail.has_unread("Wren")
    box = dict(boxfiles.read_boxes(_mail_dir(tmp_path)))["wren"]
    assert box["version"] == 2 and box["messages"][0]["read"] is False


def test_migration_recases_a_non_casefolded_key(tmp_path):
    _write_legacy_mail(tmp_path, {"version": 2, "mailboxes": {
        "WREN": [{"ts": 1.0, "read": False, "blob": "BLOB-X"}]}})
    mail = _mail(tmp_path)
    assert mail.count("wren") == 1
    assert sorted(k for k, _ in boxfiles.read_boxes(_mail_dir(tmp_path))) == ["wren"]


def test_migrated_mail_still_DECRYPTS_under_the_same_key(tmp_path):
    """⛔ The end-to-end proof that ciphertext was MOVED, not re-encrypted or
    mangled: real blobs, written by a real store, read back after the split."""
    first = _mail(tmp_path)
    first.deliver("Wren", "Corvid", "the door is unlocked")
    first.deliver("Wren", "Corvid", "and the kettle is on")

    # Fold the split store back into a legacy file, then load it fresh.
    boxes = {k: d["messages"]
             for k, d in boxfiles.read_boxes(_mail_dir(tmp_path))}
    import shutil
    shutil.rmtree(_mail_dir(tmp_path))
    _write_legacy_mail(tmp_path, {"version": 2, "mailboxes": boxes})

    migrated = _mail(tmp_path)
    bodies = [m.body for m in migrated.read("Wren")]
    assert bodies == ["the door is unlocked", "and the kettle is on"]
    assert migrated.read("Wren")[0].sender == "Corvid"


def test_revtell_migrates_losslessly_too(tmp_path):
    (tmp_path / "revtells.json").write_text(json.dumps({
        "version": 1,
        "buffers": {"corvid": [{"ts": 1.0, "blob": "R-A"},
                               {"ts": 2.0, "blob": "R-B"}],
                    "WREN": [{"ts": 3.0, "blob": "R-C"}]},
    }), encoding="utf-8")
    rev = _rev(tmp_path)

    assert rev.count("Corvid") == 2 and rev.count("wren") == 1
    assert (tmp_path / "revtells.json.pre-split").exists()
    d = boxfiles.box_dir(tmp_path / "revtells.json")
    boxes = dict(boxfiles.read_boxes(d))
    assert sorted(boxes) == ["corvid", "wren"]
    assert [e["blob"] for e in boxes["corvid"]["entries"]] == ["R-A", "R-B"]


def test_a_revtell_line_survives_the_split_and_still_decrypts(tmp_path, tmp_path_factory):
    first = _rev(tmp_path)
    first.record("Corvid", ">> Wren says: pick this up later")
    d = boxfiles.box_dir(tmp_path / "revtells.json")
    entries = {k: v["entries"] for k, v in boxfiles.read_boxes(d)}
    import shutil
    shutil.rmtree(d)
    (tmp_path / "revtells.json").write_text(
        json.dumps({"version": 1, "buffers": entries}), encoding="utf-8")

    migrated = _rev(tmp_path)
    assert migrated.read("Corvid") == [">> Wren says: pick this up later"]


def test_an_over_cap_buffer_is_not_trimmed_by_the_migration(tmp_path):
    """⚠ A migration is not the place to quietly delete somebody's lines: a
    buffer written under a larger `revtell_count` keeps what it holds until its
    owner's next line rolls it, exactly as a restart used to leave it."""
    (tmp_path / "revtells.json").write_text(json.dumps({
        "version": 1,
        "buffers": {"corvid": [{"ts": float(i), "blob": f"R-{i}"}
                               for i in range(10)]},
    }), encoding="utf-8")
    rev = _rev(tmp_path, cap=3)
    assert rev.count("Corvid") == 10            # kept...
    rev.record("Corvid", "one more")
    assert rev.count("Corvid") == 3             # ...and rolled on the next line


# ---------------------------------------------------------------------------
# 3. A hostile box key cannot leave the directory
# ---------------------------------------------------------------------------

HOSTILE = [
    "../../../etc/passwd",
    "..",
    "/absolute/elsewhere",
    "back\\slash",
    "nul\x00byte",
    "new\nline",
    "  spaces  ",
    "#staff",
    "réservé",
]


@pytest.mark.parametrize("key", HOSTILE)
def test_a_hostile_box_key_never_writes_outside_the_store(tmp_path, key):
    """⛔ Path-safe BY CONSTRUCTION, not by sanitising at the call sites: the
    filename is hex, so its alphabet is `[0-9a-f]` and there is nothing in it
    that a filesystem reads as structure."""
    mail = _mail(tmp_path)
    assert mail.deliver(key, "Corvid", "a message")

    written = [p for p in _mail_dir(tmp_path).iterdir()]
    assert len(written) == 1
    name = written[0].name
    assert name.endswith(".json")
    assert set(name[:-5]) <= set("0123456789abcdef"), name
    # It stayed inside, and it round-trips back to the key it came from.
    assert written[0].parent == _mail_dir(tmp_path)
    assert boxfiles.decode_key(name[:-5]) == key.casefold()
    assert mail.count(key) == 1

    reloaded = _mail(tmp_path)                   # and survives a restart
    assert reloaded.count(key) == 1


def test_nothing_was_written_above_the_store_directory(tmp_path):
    """The blunt version of the same question: after a hostile key, is anything
    at all outside the box directory?"""
    mail = _mail(tmp_path)
    for key in HOSTILE:
        mail.deliver(key, "Corvid", "a message")
    stray = sorted(p.name for p in tmp_path.iterdir())
    assert stray == ["mail.key", "mailboxes.json.d"]
    assert len(list(_mail_dir(tmp_path).iterdir())) == len(HOSTILE)


def test_a_stray_file_in_the_box_directory_is_ignored(tmp_path):
    """Not everything in a directory is ours. A store that refused to open
    because somebody left a file there would be worse than one that skips it."""
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "hello")
    (_mail_dir(tmp_path) / "notes.json").write_text("{}", encoding="utf-8")
    (_mail_dir(tmp_path) / "zzz.txt").write_text("hi", encoding="utf-8")

    reloaded = _mail(tmp_path)
    assert reloaded.count("Wren") == 1
    assert dict(boxfiles.read_boxes(_mail_dir(tmp_path))).keys() == {"wren"}


def test_a_corrupt_box_file_does_not_cost_everybody_their_mail(tmp_path, caplog):
    """⚠ A property the single-file store could not have: one unreadable box is
    one person's problem, not the house's."""
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "hello")
    mail.deliver("Cara", "Corvid", "hello")
    boxfiles.box_path(_mail_dir(tmp_path), "wren").write_text("{ not json",
                                                              encoding="utf-8")
    with caplog.at_level("WARNING"):
        reloaded = _mail(tmp_path)
    assert reloaded.count("Cara") == 1           # everybody else is fine
    assert reloaded.count("Wren") == 0
    assert any("unreadable box file" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 4. Atomicity — an interrupted write leaves the previous box intact
# ---------------------------------------------------------------------------

def test_a_crash_mid_write_leaves_the_previous_box_intact(tmp_path, monkeypatch):
    """The temp file is written and fsynced, and only `os.replace` makes it the
    box. A crash before that leaves the old file exactly where it was — the same
    guarantee the single-file store had, now per box."""
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "the first message")
    before = boxfiles.box_path(_mail_dir(tmp_path), "wren").read_bytes()

    class Boom(Exception):
        pass

    def explode(src, dst):
        raise Boom("power cut")

    monkeypatch.setattr(boxfiles.os, "replace", explode)
    with pytest.raises(Boom):
        mail.deliver("Wren", "Corvid", "the second message")
    monkeypatch.undo()

    assert boxfiles.box_path(_mail_dir(tmp_path), "wren").read_bytes() == before
    reloaded = _mail(tmp_path)
    assert [m.body for m in reloaded.read("Wren")] == ["the first message"]


def test_a_failed_write_leaves_no_temp_file_behind(tmp_path, monkeypatch):
    mail = _mail(tmp_path)
    mail.deliver("Wren", "Corvid", "one")

    def explode(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(boxfiles.os, "replace", explode)
    with pytest.raises(OSError):
        mail.deliver("Wren", "Corvid", "two")
    monkeypatch.undo()

    assert [p.name for p in _mail_dir(tmp_path).glob(".box.*")] == []


# ---------------------------------------------------------------------------
# 5. Modes — 0700 on the directory, 0600 on every file
# ---------------------------------------------------------------------------

def test_the_box_directory_is_0700_and_every_box_is_0600(tmp_path):
    """A world-listable directory of box files is a list of who has mail, which
    is the same argument that made the state dir 0700 in v1.49.0."""
    mail = _mail(tmp_path)
    for name in ("Wren", "Corvid", "#staff"):
        mail.deliver(name, "Someone", "hello")

    assert stat.S_IMODE(os.stat(_mail_dir(tmp_path)).st_mode) == 0o700
    for path in _mail_dir(tmp_path).glob("*.json"):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path.name


def test_the_revtell_directory_and_files_have_the_same_modes(tmp_path):
    rev = _rev(tmp_path)
    rev.record("Corvid", ">> Wren says: hello")
    d = boxfiles.box_dir(tmp_path / "revtells.json")
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    for path in d.glob("*.json"):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_the_migrated_store_gets_the_same_modes(tmp_path):
    """The migration writes through the same helper, so it cannot land a box
    with laxer permissions than an ordinary delivery would."""
    _write_legacy_mail(tmp_path)
    _mail(tmp_path)
    assert stat.S_IMODE(os.stat(_mail_dir(tmp_path)).st_mode) == 0o700
    for path in _mail_dir(tmp_path).glob("*.json"):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ---------------------------------------------------------------------------
# 6. The layout is DERIVED — no new Config field to misconfigure
# ---------------------------------------------------------------------------

def test_the_box_directory_is_derived_from_the_configured_path():
    assert boxfiles.box_dir(Path("/var/lib/heresay/mailboxes.json")) == \
        Path("/var/lib/heresay/mailboxes.json.d")
    assert boxfiles.box_dir(Path("/var/lib/heresay/revtells.json")) == \
        Path("/var/lib/heresay/revtells.json.d")
    # ⚠ Derived from the WHOLE name, not the stem: two stores configured as
    # `x.json` and `x.dat` in one directory must not share a box directory.
    assert boxfiles.box_dir(Path("/s/x.json")) != boxfiles.box_dir(Path("/s/x.dat"))


def test_no_new_config_field_was_added():
    """⛔ Pass 2's classification guard would fail on an unclassified field; the
    honest way to keep that true is not to add one. The layout hangs off the
    path the operator already configured."""
    from heresay.config import Config
    fields = set(Config.__dataclass_fields__)
    paths = {f for f in fields if f.endswith("_file") or f.endswith("_dir")}
    assert paths == {
        "accounts_file", "agreement_file", "ban_history_file", "bans_file",
        "base_dir", "cert_file", "control_socket_file", "events_file",
        "forbidden_names_file", "greeting_file", "help_dir", "house_dir",
        "key_file", "mail_file", "mail_key_file", "permissions_file",
        "reboot_file", "resume_file", "revtell_file", "state_file",
        "theme_file", "world_dir",
    }, "a storage path field was added — classify it in reload.py"
