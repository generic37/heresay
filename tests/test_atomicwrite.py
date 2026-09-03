# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The shared atomic-write helper (CODE_AUDIT.md #1 — one write for six stores).

Pins the four guarantees the six call sites relied on: atomic temp+replace, an
fsync before the rename (durability — `deliver()` leans on it), the mode set on
the fd before any bytes, and a failed write left as a no-op with no temp behind.
"""
import json
import os
import stat

import pytest

from heresay.atomicwrite import atomic_write, atomic_write_json


def test_json_roundtrip_indent_ascii_and_trailing_newline(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"b": "ä", "a": 1})
    text = p.read_text(encoding="utf-8")
    assert json.loads(text) == {"b": "ä", "a": 1}
    assert text.endswith("}\n")            # the trailing newline every store wrote
    assert "ä" in text                     # ensure_ascii=False (not ä)
    assert '\n  "b"' in text               # indent=2


def test_default_mode_is_0600(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_mode_is_configurable(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1}, mode=0o640)
    assert stat.S_IMODE(p.stat().st_mode) == 0o640


def test_replace_is_atomic_and_leaves_no_temp(tmp_path):
    p = tmp_path / "x.json"
    atomic_write_json(p, {"a": 1})
    atomic_write_json(p, {"a": 2})
    assert json.loads(p.read_text()) == {"a": 2}
    # only the target — the sibling temp was renamed, not left behind
    assert sorted(q.name for q in tmp_path.iterdir()) == ["x.json"]


def test_fsync_is_called_by_default(tmp_path, monkeypatch):
    called = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: called.append(fd) or real(fd))
    atomic_write_json(tmp_path / "x.json", {"a": 1})
    assert called, "durability: fsync must run before the rename"


def test_fsync_can_be_skipped(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(os, "fsync", lambda fd: called.append(fd))
    atomic_write_json(tmp_path / "x.json", {"a": 1}, fsync=False)
    assert not called


def test_a_failed_write_is_a_no_op_with_no_temp(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("OLD\n", encoding="utf-8")

    class Unserializable:
        pass

    with pytest.raises(TypeError):
        atomic_write_json(p, {"bad": Unserializable()})
    assert p.read_text() == "OLD\n"        # the old file is untouched
    assert sorted(q.name for q in tmp_path.iterdir()) == ["x.json"]   # temp cleaned up


def test_text_variant_writes_raw(tmp_path):
    # the events-trim path: a raw JSONL blob, not a json.dump of a dict
    p = tmp_path / "events.jsonl"
    atomic_write(p, '{"ts":1}\n{"ts":2}\n')
    assert p.read_text() == '{"ts":1}\n{"ts":2}\n'
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_creates_the_parent_directory(tmp_path):
    p = tmp_path / "nested" / "deep" / "x.json"
    atomic_write_json(p, {"a": 1})
    assert json.loads(p.read_text()) == {"a": 1}
