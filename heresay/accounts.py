# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Account storage.

The account file is one of only two pieces of disk state (the other being the
room definitions). It is human-readable JSON and operator-editable. It holds
NO conversation, ever — only who may log in.

Schema deliberately leaves room for Phase 3 (profile text, mailbox); those keys
are simply absent for now.
"""

from __future__ import annotations

import asyncio
import json
import string
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from . import recovery
from .atomicwrite import atomic_write_json

# One hasher instance; argon2id is the default type.
_hasher = PasswordHasher()

# --- async hashing off the event loop --------------------------------------
# argon2id is deliberately expensive (~64 MiB, tens of ms). Run it in a worker
# thread so one login attempt can't freeze the whole asyncio loop (and every
# other connected session) for its duration. A small semaphore bounds how many
# 64 MiB hashes run at once. The semaphore is created per running loop, so the
# per-test event loops pytest-asyncio spins up never collide.
_HASH_CONCURRENCY = 4
_hash_sems: dict = {}


def _hash_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _hash_sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_HASH_CONCURRENCY)
        _hash_sems[loop] = sem
    return sem


async def _ahash(secret: str) -> str:
    """argon2id hash of ``secret``, computed in a worker thread."""
    async with _hash_sem():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _hasher.hash, secret)


async def _averify(stored_hash: str, secret: str) -> bool:
    """Verify ``secret`` against ``stored_hash`` in a worker thread. Returns a
    plain bool (the argon2 exceptions never cross back onto the loop)."""
    def _run() -> bool:
        try:
            _hasher.verify(stored_hash, secret)
            return True
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
    async with _hash_sem():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _run)


@dataclass
class Account:
    name: str
    password_hash: str
    description: str = ""
    created: str = ""
    profile: str = ""   # multi-line, public; mail lives separately (mailboxes.json)
    # The rank NAME from `[ranks] order` (v1.27.0, was `level` ∈ resident|wizard).
    # A rank's name is its identity and its display word both. The ARCH is still
    # config-driven and never stored — an account records only what it was
    # granted. Empty means "the floor", resolved against the live ladder, so an
    # account written before the ladder existed reads correctly.
    rank: str = ""
    # Resident prefs: the .set toggles {"dim","time"}, the message strings
    # {"enter_msg","exit_msg","afk_msg","login_msg","logout_msg"} and the
    # {"ignore": {category: bool}} noise filter. A free dict on purpose — unknown
    # keys (e.g. "color", from before the dim rename) load and are ignored.
    settings: dict = field(default_factory=dict)
    recovery: list = field(default_factory=list)   # argon2id hashes of recovery codes
    agreed: str = ""   # ISO timestamp the house rules were accepted (a fact, not PII)
    # sha256 of the agreement text they accepted. Absent (the empty string) on
    # every account written before the agreement was versioned — which is what
    # makes those accounts re-consent once, on their next login.
    agreed_hash: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "password_hash": self.password_hash,
            "description": self.description,
            "created": self.created,
            "profile": self.profile,
            "rank": self.rank,
            "settings": self.settings,
            "recovery": self.recovery,
            "agreed": self.agreed,
            "agreed_hash": self.agreed_hash,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(
            name=d["name"],
            password_hash=d["password_hash"],
            description=d.get("description", ""),
            created=d.get("created", ""),
            profile=d.get("profile", ""),
            rank=str(d.get("rank", "") or ""),
            settings=dict(d.get("settings", {})),
            recovery=list(d.get("recovery", [])),
            agreed=d.get("agreed", ""),
            agreed_hash=d.get("agreed_hash", ""),
        )


# Current on-disk schema version. Canonical shape:
#   {"version": 3, "accounts": {"<casefold name>": {record incl. display "name"}}}
#
# v3 (1.27.0) replaced the two-value `level` field with `rank`, a NAME from the
# configured ladder. ⚠ An account with no `rank` reads as the RESIDENT level
# everywhere — the fail-safe direction — so a pre-v3 record simply lands there.
SCHEMA_VERSION = 3

# The 1.16.0 `.set` message-key rename. Unlike the house-setting remap (see
# state.migrate_house_settings), this one is COLLISION-FREE — no new name equals
# any old name — so order cannot matter and no value can overwrite another. A
# resident who set their wording years ago keeps it, under the new name, without
# doing anything.
LEGACY_SETTING_KEYS = {
    "inmsg": "enter_msg",
    "outmsg": "exit_msg",
    "afkmsg": "afk_msg",
    "loginmsg": "login_msg",
    "logoutmsg": "logout_msg",
}


def migrate_settings(settings: dict) -> dict:
    """Pre-1.16 per-resident setting keys -> the current scheme."""
    return {LEGACY_SETTING_KEYS.get(k, k): v for k, v in settings.items()}


def collapse_rank(stored: str, ladder, is_arch: bool = False) -> str:
    """Where one account lands when the ladder is REBUILT (v1.38.0).

    ⚠ The whole idea is that you cannot trust the old mapping across a reorg. A
    house that inserts a rung, renames two and re-points `staff` has changed what
    every stored word MEANS, and there is no honest way to guess which of the new
    rungs somebody was on. So everyone lands at the BOTTOM OF THEIR TIER and is
    re-promoted deliberately. The churn is the price of not inventing authority
    nobody granted.

        arch (by NAME)  -> arch, always
        >= staff        -> the staff anchor
        >= floor        -> the floor anchor
        <  floor        -> newb
        not on the ladder at all -> newb

    ⚠ The arch is identified by ACCOUNT NAME, not by rank — the owner is named in
    config and holds the top rank regardless of what is stored, so a collapse
    that read their rank would be reading the wrong thing. It is also the one
    account a collapse must never be able to demote.
    """
    if is_arch:
        return ladder.owner_rank
    return ladder.level_anchor(stored)


def _key(name: str) -> str:
    """The single, deliberate normalization for account identity.

    casefold() (not lower()) so that names comparing equal under Unicode case
    folding are the same account. Display capitalization is preserved separately
    in Account.name.
    """
    return name.casefold()


class AccountStore:
    """In-memory account table backed by an atomically-written JSON file.

    Identity is a casefolded key; the display name (original capitalization) is
    kept in the record and used for all output.
    """

    def __init__(self, path: str | Path, ladder=None, owner_name: str = "",
                 landing_level: str = "newb"):
        self.path = Path(path)
        # The rank ladder (a Theme), needed to migrate `level` -> `rank` and to
        # spot stored ranks the ladder no longer knows. The server loads the
        # theme BEFORE the accounts (server.py: theme, then AccountStore), so
        # this is an ordinary argument rather than a re-ordering; a bare store in
        # a test gets the built-in ladder.
        from .theme import DEFAULT_THEME
        self.ladder = ladder if ladder is not None else DEFAULT_THEME
        # The arch OWNER's name, for the reorg collapse: the one account whose
        # rank is decided by identity rather than by what is stored. Empty in a
        # bare test store, which simply means no account is exempt.
        self.owner_name = str(owner_name or "")
        # Where a NEW account lands, resolved once from the configured landing
        # LEVEL to that level's anchor rank on this ladder. Default `newb` = the
        # bottom of the ladder (order[0]), exactly the pre-v1.54 behaviour.
        self.landing_rank = self.ladder.level_rank(landing_level or "newb")
        # Ranks read on load that are not on the ladder — a count, never names.
        # A rank RENAME in theme.toml would otherwise demote everyone holding the
        # old word in silence; the server says this number once at startup.
        self.unknown_ranks = 0
        # How many accounts the last load's reorg collapse moved, and whether it
        # ran at all. Counts, never names; the server says them once.
        self.reorg_collapsed = 0
        self.reorg_ran = False
        self._accounts: dict[str, Account] = {}  # keyed by casefolded name
        # Preference writes (.set / .desc / .profile) mark this instead of doing a
        # full fsync-rewrite per keystroke; flush() coalesces them. Security-
        # bearing writes (password, level, recovery, create/delete) still persist
        # immediately (and clear the flag, since a save writes everything).
        self._dirty = False
        # The ladder shape the stored ranks were last placed against. Set by
        # load(); written by _save().
        self._fingerprint = self.ladder.rank_fingerprint()
        self.load()

    # ---- persistence -------------------------------------------------

    def load(self) -> None:
        """Load accounts, tolerating legacy shapes and migrating in place.

        Reads BOTH the historical shapes:
          - list of records:        {"accounts": [ {record}, ... ]}
          - object keyed by name:    {"accounts": {"Generic": {record}, ...}}
        Re-keys everything by casefold(name) and, if the file was not already in
        the canonical version-2 form, rewrites it atomically. A malformed record
        is skipped rather than crashing the whole store (login must never be
        blocked by one bad row).
        """
        self._accounts.clear()
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        raw = data.get("accounts")
        version = data.get("version")
        records: list[dict] = []

        if isinstance(raw, list):
            records = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key, val in raw.items():
                if not isinstance(val, dict):
                    continue
                rec = dict(val)
                rec.setdefault("name", key)   # display name falls back to the key
                records.append(rec)
        # else: unknown/empty shape -> no accounts (never crash)

        # The 1.27.0 level -> rank migration happens HERE, with the raw record
        # still in hand, and is stamped by the schema version so it runs exactly
        # once. From v3 on, `rank` is authoritative and `level` is never read.
        for rec in records:
            try:
                acct = Account.from_dict(rec)
            except (KeyError, TypeError):
                continue  # skip a malformed row; keep the rest usable
            k = _key(acct.name)
            if k not in self._accounts:  # first writer wins on a cased duplicate
                self._accounts[k] = acct

        # The 1.16.0 message-key rename, in place. Riding the same load-time
        # migration+rewrite the schema itself uses means there is exactly one
        # place accounts.json is quietly brought up to date.
        renamed = False
        for acct in self._accounts.values():
            migrated = migrate_settings(acct.settings)
            if migrated != acct.settings:
                acct.settings = migrated
                renamed = True

        # Stored ranks the ladder no longer knows. Counted, never named: a rank
        # RENAME in theme.toml turns every holder of the old word into a floor
        # resident, and the count is what stops that being silent. The reading
        # itself is unchanged (fail-safe to the floor) — this only makes it
        # visible, which is the difference between a bad afternoon and a mystery.
        self.unknown_ranks = sum(
            1 for a in self._accounts.values()
            if a.rank and self.ladder.rank_index(a.rank) is None
        )

        # The REORG COLLAPSE (v1.38.0), fingerprint-triggered: it runs only when
        # the ladder's SHAPE has changed since the last load. See
        # `_collapse_if_reorganised` for why it must not run otherwise.
        #
        # ⚠ AFTER the count above, not before. `unknown_ranks` reports what the
        # FILE held — "a rename demoted this many people" — and a collapse
        # resolves exactly those records, so running it first would zero the one
        # number that says a rename happened. The two are different facts: what
        # was read, and what was done about it (`reorg_collapsed`).
        collapsed = self._collapse_if_reorganised(data.get("rank_fingerprint"))

        # Migrate in place unless the file is already canonical.
        canonical = (
            version == SCHEMA_VERSION
            and isinstance(raw, dict)
            and all(k == _key(v.get("name", k)) for k, v in raw.items()
                    if isinstance(v, dict))
        )
        if not canonical or renamed or collapsed:
            self._save()

    def _collapse_if_reorganised(self, stored_fingerprint) -> bool:
        """Collapse every account into its zone, but ONLY if the ladder moved.

        ⚠ **A normal restart must never touch a rank.** That is the entire
        reason this is fingerprint-gated rather than run on every load: a
        collapse is a demotion for anybody above their zone's anchor, and doing
        it on an ordinary reboot would quietly undo `.promote` every night. So
        the shape is hashed, stored beside the accounts, and compared.

        The first load after this ships has NO stored fingerprint. That is
        recorded, not collapsed: nothing has changed — the file simply predates
        the field — and demoting a house for upgrading would be the worst
        possible introduction to the feature.

        Coexists with the v1.27.0 `level` -> `rank` migration above rather than
        replacing it: that one interprets an OLD FIELD once, this one responds to
        a NEW LADDER whenever it appears. They can both run on the same load (an
        upgrade that also reorganises), and the order is right — `level` is read
        into a rank first, and the collapse then places that rank.
        """
        current = self.ladder.rank_fingerprint()
        self.reorg_ran = False
        self.reorg_collapsed = 0
        if not stored_fingerprint:
            # First sight of the field: adopt the shape, move nobody.
            self._fingerprint = current
            return bool(self._accounts)          # write it down if there is a file
        if stored_fingerprint == current:
            self._fingerprint = current
            return False
        self.reorg_ran = True
        arch_key = _key(self.owner_name) if self.owner_name else None
        for key, acct in self._accounts.items():
            was = acct.rank
            now = collapse_rank(was, self.ladder, is_arch=(key == arch_key))
            if now != was:
                acct.rank = now
                self.reorg_collapsed += 1
        self._fingerprint = current
        return True

    def _save(self) -> None:
        payload = {
            "version": SCHEMA_VERSION,
            # The ladder shape these ranks were last placed against (v1.38.0).
            # Beside the accounts because it is a fact ABOUT them: it answers
            # "have these ranks been read against the current ladder?", which is
            # a question about this file, not about the daemon's state.
            "rank_fingerprint": self._fingerprint,
            "accounts": {k: a.to_dict() for k, a in self._accounts.items()},
        }
        atomic_write_json(self.path, payload)   # 0600, fsync, temp+replace
        self._dirty = False   # a save persists everything, pending prefs too

    def flush(self) -> None:
        """Persist any pending preference changes. Called on session end, on a
        periodic timer, and on clean shutdown — so deferred .set/.desc/.profile
        writes survive, without an fsync per keystroke."""
        if self._dirty:
            self._save()

    # ---- queries -----------------------------------------------------

    def exists(self, name: str) -> bool:
        return _key(name) in self._accounts

    def get(self, name: str) -> Account | None:
        return self._accounts.get(_key(name))

    def all(self) -> list[Account]:
        """Every account, in unspecified order (the caller sorts). A snapshot
        list, so iterating it can't be disturbed by a concurrent save."""
        return list(self._accounts.values())

    def resolve(self, query: str) -> tuple[str | None, bool]:
        """Resolve a typed name to a canonical display name. **EXACT ONLY.**

        ⛔ **A name is a complete match or it is not a match** (v1.52.0). Case is
        still free — `wren`, `Wren` and `WREN` are one person — but an
        abbreviation is not: `.tell wr` no longer finds Wren.

        **Why the abbreviation went.** A mis-resolved name is not a typo, it is
        an action taken on the wrong person: a `.tell` is a private message
        delivered to somebody who was never meant to read it — a privacy
        incident with no undo — and on `.ban` / `.boot` / `.jail` / `.promote` it
        is moderation applied to a stranger. The convenience was never worth
        that, and it costs almost nothing to give up: `>` repeats to a sticky
        correspondent, so the common case of talking to one person is already
        abbreviation-free.

        ⛔ **Fixed HERE rather than at the ~13 call sites**, so every command
        inherits it at once and a command written next year cannot reintroduce
        fuzzy matching by forgetting to ask for exactness.

        ⚠ **The `(name, ambiguous)` shape is KEPT, and `ambiguous` is now always
        False** — an exact casefold key is unique by construction, because the
        store is keyed on it. Callers are therefore untouched: their "Who do you
        mean by X?" branches remain, correct against the declared contract and
        simply never reached. That is deliberate. Rewriting thirteen call sites
        to drop a flag would be a far larger change than the fix, and would leave
        them wrong if this ever returned True again.
        """
        q = query.strip().casefold()
        if not q:
            return None, False
        acct = self._accounts.get(q)
        return (acct.name, False) if acct is not None else (None, False)

    # ---- mutations ---------------------------------------------------

    def create(self, name: str, password: str, description: str = "",
               agreed: bool = False, agreed_hash: str = "") -> Account:
        if self.exists(name):  # casefold: 'generic' is a duplicate of 'Generic'
            raise ValueError("account already exists")
        acct = Account(
            name=name,   # preserve the capitalization chosen at creation
            password_hash=_hasher.hash(password),
            description=description,
            created=date.today().isoformat(),
            # ⚠ NEW ACCOUNTS land at the configured `landing_level`'s anchor
            # rank (default `newb` = order[0], the pre-v1.54 behaviour). Written
            # EXPLICITLY rather than left empty: an empty rank reads as the floor
            # everywhere, and on a ladder with a newb rung that would be a free
            # promotion at sign-up.
            rank=self.landing_rank,
            agreed=datetime.now().isoformat(timespec="seconds") if agreed else "",
            agreed_hash=agreed_hash if agreed else "",
        )
        self._accounts[_key(name)] = acct
        self._save()
        return acct

    async def acreate(self, name: str, password: str, description: str = "",
                      agreed: bool = False, agreed_hash: str = "") -> Account:
        """Async ``create`` — hashes off the event loop. Used by the server; the
        dict mutation + save run on the loop, so store state stays single-threaded."""
        if self.exists(name):
            raise ValueError("account already exists")
        password_hash = await _ahash(password)
        acct = Account(
            name=name,
            password_hash=password_hash,
            description=description,
            created=date.today().isoformat(),
            # ⚠ NEW ACCOUNTS land at the configured `landing_level`'s anchor
            # rank (default `newb` = order[0], the pre-v1.54 behaviour). Written
            # EXPLICITLY rather than left empty: an empty rank reads as the floor
            # everywhere, and on a ladder with a newb rung that would be a free
            # promotion at sign-up.
            rank=self.landing_rank,
            agreed=datetime.now().isoformat(timespec="seconds") if agreed else "",
            agreed_hash=agreed_hash if agreed else "",
        )
        self._accounts[_key(name)] = acct
        self._save()
        return acct

    def verify(self, name: str, password: str) -> bool:
        acct = self.get(name)
        if acct is None:
            return False
        try:
            _hasher.verify(acct.password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False
        # Transparently upgrade the hash if parameters changed.
        if _hasher.check_needs_rehash(acct.password_hash):
            acct.password_hash = _hasher.hash(password)
            self._save()
        return True

    async def averify(self, name: str, password: str) -> bool:
        """Async ``verify`` — argon2 runs off the event loop. The optional
        rehash-on-login is offloaded too; the save runs on the loop."""
        acct = self.get(name)
        if acct is None:
            return False
        if not await _averify(acct.password_hash, password):
            return False
        if _hasher.check_needs_rehash(acct.password_hash):
            acct.password_hash = await _ahash(password)
            self._save()
        return True

    def record_agreement(self, name: str, agreed_hash: str) -> None:
        """Record that this account accepted the agreement version ``agreed_hash``,
        stamped now. Called on creation's consent and on each re-consent."""
        acct = self.get(name)
        if acct is None:
            return
        acct.agreed = datetime.now().isoformat(timespec="seconds")
        acct.agreed_hash = agreed_hash
        self._save()

    def set_display_name(self, name: str, new_display: str) -> None:
        """Restyle an account's DISPLAY name (``Account.name``) — capitalization
        only. The casefold identity key is unchanged (callers enforce that
        ``new_display.casefold() == name.casefold()``), so the dict key, and thus
        lookup / login / uniqueness / mail addressing, are all unaffected.
        Persisted immediately (an identity restyle, not a coalesced preference)."""
        acct = self.get(name)
        if acct is None:
            return
        acct.name = new_display
        self._save()

    def set_description(self, name: str, description: str) -> None:
        acct = self.get(name)
        if acct is None:
            return
        acct.description = description
        self._dirty = True   # a preference: coalesced, see flush()

    def set_profile(self, name: str, profile: str) -> None:
        acct = self.get(name)
        if acct is None:
            return
        acct.profile = profile
        self._dirty = True   # a preference: coalesced, see flush()

    # Called with the account name after ANY successful password change. The
    # server points it at the resume store, so changing a password signs every
    # remembered device out.
    #
    # A hook HERE rather than at the call sites on purpose: there are three
    # password-change paths (`.passwd`, the arch's `.resetpassword`, and the
    # recovery-code reset inside login), and the third is the one that matters
    # most and the easiest to overlook. Hooking the single place they all funnel
    # through means a fourth path added later cannot forget.
    on_password_change = None

    def _password_changed(self, name: str) -> None:
        if self.on_password_change is not None:
            self.on_password_change(name)

    def change_password(self, name: str, new_password: str) -> None:
        acct = self.get(name)
        if acct is None:
            return
        acct.password_hash = _hasher.hash(new_password)
        self._save()
        self._password_changed(acct.name)

    async def achange_password(self, name: str, new_password: str) -> None:
        """Async ``change_password`` — hashes off the event loop."""
        acct = self.get(name)
        if acct is None:
            return
        acct.password_hash = await _ahash(new_password)
        self._save()
        self._password_changed(acct.name)

    # ---- recovery codes (argon2id hashes only, exactly like passwords) ----

    def set_recovery(self, name: str, codes: list[str]) -> None:
        """Replace the account's recovery set with hashes of ``codes`` (plaintext).
        Voids any prior set."""
        acct = self.get(name)
        if acct is None:
            return
        acct.recovery = [_hasher.hash(recovery.normalize(c)) for c in codes]
        self._save()

    async def aset_recovery(self, name: str, codes: list[str]) -> None:
        """Async ``set_recovery`` — hashes each code off the event loop."""
        acct = self.get(name)
        if acct is None:
            return
        acct.recovery = [await _ahash(recovery.normalize(c)) for c in codes]
        self._save()

    def recovery_count(self, name: str) -> int:
        acct = self.get(name)
        return len(acct.recovery) if acct else 0

    def find_recovery(self, name: str, code: str) -> int:
        """Return the index of the (unused) recovery code matching ``code``, or -1.
        Does not burn it — call burn_recovery(name, index) after a successful use."""
        acct = self.get(name)
        if acct is None:
            return -1
        norm = recovery.normalize(code)
        if not norm:
            return -1
        for i, h in enumerate(acct.recovery):
            try:
                if _hasher.verify(h, norm):
                    return i
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                continue
        return -1

    async def afind_recovery(self, name: str, code: str) -> int:
        """Async ``find_recovery`` — verifies each stored code off the event loop."""
        acct = self.get(name)
        if acct is None:
            return -1
        norm = recovery.normalize(code)
        if not norm:
            return -1
        for i, h in enumerate(acct.recovery):
            if await _averify(h, norm):
                return i
        return -1

    def burn_recovery(self, name: str, index: int) -> None:
        acct = self.get(name)
        if acct is None or index < 0 or index >= len(acct.recovery):
            return
        del acct.recovery[index]
        self._save()

    def set_rank(self, name: str, rank: str) -> None:
        """Persist a granted rank. Security-bearing, so it saves immediately."""
        acct = self.get(name)
        if acct is None:
            return
        acct.rank = rank
        self._save()

    def delete(self, name: str) -> bool:
        """Permanently remove an account (atomic rewrite). Returns True if it
        existed. The freed name becomes registrable again."""
        if self.get(name) is None:
            return False
        del self._accounts[_key(name)]
        self._save()
        return True

    def set_setting(self, name: str, key: str, value) -> None:
        acct = self.get(name)
        if acct is None:
            return
        acct.settings[key] = value
        self._dirty = True   # a preference: coalesced, see flush()

    def clear_setting(self, name: str, key: str) -> None:
        """Forget a preference entirely, so the house default applies again.
        (Storing the default would freeze it; absence lets it move.)"""
        acct = self.get(name)
        if acct is None or key not in acct.settings:
            return
        del acct.settings[key]
        self._dirty = True   # a preference: coalesced, see flush()


# Validation used by the login flow. Names are ASCII-only: a leading ASCII
# letter, then ASCII letters/digits/hyphen/underscore. Deliberately NOT the
# Unicode-aware isalpha/isalnum — those accept homoglyphs (Cyrillic "А", Greek
# "Ο", full-width digits) that let one resident impersonate another in room
# listings and speech attribution. Identity here is one flat ASCII namespace.
_NAME_FIRST = set(string.ascii_letters)
_NAME_CHARS = set(string.ascii_letters + string.digits + "-_")


def valid_name(name: str) -> bool:
    if not (2 <= len(name) <= 16):
        return False
    if name[0] not in _NAME_FIRST:
        return False
    return all(c in _NAME_CHARS for c in name)
