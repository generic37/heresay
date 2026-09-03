# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""AccountStore: case-insensitive identity, and migration of legacy file shapes.

Regression coverage for the bug where a known account (`Generic`) was treated as
unknown at login because the on-disk file used the object-keyed shape (the same
shape `mailboxes.json` uses) while the loader only understood a list of records.
"""

import json

import pytest

from heresay.accounts import SCHEMA_VERSION, AccountStore

# A syntactically-valid-looking but fake hash — we never handle real hashes here.
FAKE_HASH = "$argon2id$v=19$m=65536,t=3,p=4$" + "A" * 22 + "$" + "B" * 43


def _write(path, obj):
    path.write_text(json.dumps(obj, indent=2))


# ---- case-insensitive identity ------------------------------------------

def test_create_then_login_exact_and_other_case(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "s3cret")
    assert store.verify("Generic", "s3cret")   # exact case
    assert store.verify("generic", "s3cret")   # different case
    assert store.verify("GENERIC", "s3cret")
    assert not store.verify("Generic", "wrong")


def test_exists_is_case_insensitive(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    for typed in ("Generic", "generic", "GENERIC", "GeNeRiC"):
        assert store.exists(typed)


def test_duplicate_creation_refused_across_case(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    with pytest.raises(ValueError):
        store.create("generic", "other")
    with pytest.raises(ValueError):
        store.create("GENERIC", "other")


def test_display_capitalization_preserved(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    assert store.get("generic").name == "Generic"
    assert store.get("GENERIC").name == "Generic"


def test_resolve_is_exact_and_case_insensitive(tmp_path):
    """⛔ REWRITTEN in v1.52.0 — this test used to assert prefix matching and an
    ambiguity flag, both of which are gone. A name is a complete match or it is
    not a match; a mis-resolved name is an action taken on the wrong person."""
    store = AccountStore(tmp_path / "accounts.json")
    store.create("Generic", "pw")
    store.create("Wren", "pw")
    assert store.resolve("generic") == ("Generic", False)   # exact, cross-case
    assert store.resolve("gen") == (None, False)            # a prefix is nobody
    store.create("Genie", "pw")
    # Two names sharing a prefix are simply two names now — no ambiguity to
    # report, because nothing partial resolves in the first place.
    assert store.resolve("gen") == (None, False)
    assert store.resolve("Genie") == ("Genie", False)
    assert store.resolve("nobody") == (None, False)


# ---- migration -----------------------------------------------------------

def test_migrate_from_object_shape(tmp_path):
    """The reported live shape: {"accounts": {"Generic": {...}}}."""
    p = tmp_path / "accounts.json"
    _write(p, {
        "version": 1,
        "accounts": {
            "Generic": {"name": "Generic", "password_hash": FAKE_HASH,
                        "description": "d", "created": "2026-01-01", "profile": "p"},
        },
    })
    store = AccountStore(p)
    assert store.exists("Generic") and store.exists("generic")
    acct = store.get("generic")
    assert acct.name == "Generic" and acct.description == "d" and acct.profile == "p"

    on_disk = json.loads(p.read_text())
    assert on_disk["version"] == SCHEMA_VERSION
    assert isinstance(on_disk["accounts"], dict)
    assert list(on_disk["accounts"].keys()) == ["generic"]
    assert on_disk["accounts"]["generic"]["name"] == "Generic"


def test_migrate_from_object_shape_missing_name_uses_key(tmp_path):
    # If a record omits the display name, the object key supplies it.
    p = tmp_path / "accounts.json"
    _write(p, {"version": 1, "accounts": {"Generic": {"password_hash": FAKE_HASH}}})
    store = AccountStore(p)
    assert store.get("generic").name == "Generic"


def test_migrate_from_legacy_list_shape(tmp_path):
    """The shape earlier phases actually wrote: {"accounts": [ {...} ]}."""
    p = tmp_path / "accounts.json"
    _write(p, {
        "version": 1,
        "accounts": [
            {"name": "Generic", "password_hash": FAKE_HASH, "description": "",
             "created": "2026-01-01"},
        ],
    })
    store = AccountStore(p)
    assert store.exists("generic")
    on_disk = json.loads(p.read_text())
    assert on_disk["version"] == SCHEMA_VERSION
    assert isinstance(on_disk["accounts"], dict)
    assert list(on_disk["accounts"].keys()) == ["generic"]


def test_passwords_survive_migration(tmp_path):
    # Create with the real hasher, dump to the legacy list shape, reload, verify.
    seed = AccountStore(tmp_path / "seed.json")
    seed.create("Generic", "keepme")
    real_hash = seed.get("generic").password_hash

    p = tmp_path / "accounts.json"
    _write(p, {"version": 1, "accounts": [
        {"name": "Generic", "password_hash": real_hash, "description": "",
         "created": "2026-01-01"}]})
    store = AccountStore(p)
    assert store.verify("generic", "keepme")   # password intact through migration


def test_canonical_file_not_rewritten(tmp_path):
    p = tmp_path / "accounts.json"
    AccountStore(p).create("Generic", "pw")   # writes canonical v2
    before = p.read_bytes()
    AccountStore(p)                            # reload: already canonical
    assert p.read_bytes() == before            # untouched


def test_malformed_record_skipped_not_crash(tmp_path):
    p = tmp_path / "accounts.json"
    _write(p, {
        "version": 1,
        "accounts": {
            "Good": {"name": "Good", "password_hash": FAKE_HASH},
            "Bad": {"description": "no hash here"},   # missing password_hash
            "AlsoBad": "not even a dict",
        },
    })
    store = AccountStore(p)              # must not raise
    assert store.exists("good")
    assert not store.exists("bad")       # malformed rows skipped, login unblocked


def test_missing_file_is_empty(tmp_path):
    store = AccountStore(tmp_path / "nope.json")
    assert not store.exists("anyone")
