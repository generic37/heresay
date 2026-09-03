# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Resume tokens — "remember me on this device", for the web door only.

A resume token lets a browser log back in without retyping a password. It exists
because a phone browser routinely discards a backgrounded tab to save memory, and
a discarded tab is indistinguishable from a fresh open when you come back — so
without this, glancing at a text message costs you a password.

**What this is, in covenant terms.** It is auth data, not conversation. Nothing
here holds, or could hold, a word anyone said: a record is an opaque hash, an
account name, and two timestamps. Room talk remains never-persisted; this widens
nothing about what is stored about what people *say*. It is the same class of
thing as ``bans.json`` — operational facts about who may come in — and it is
modelled on that store deliberately.

**Consent is explicit and opt-in.** The setting ships OFF. It is asked once, at
account creation, defaulting to no, and explained at length in
``help/remember.txt``. Nobody is remembered who did not say so.

**Tokens are stored HASHED (sha256), never raw.** The token itself exists in
exactly two places: the browser that owns it, and the wire, once. What lands on
disk — and therefore in every nightly backup tarball — is a hash, so a stolen
``resume.json`` is inert rather than a set of live bearer credentials. sha256 is
enough here precisely because the token is not a password: it is 256 bits of
``secrets`` randomness with no guessable structure, so there is nothing for a
slow hash to defend against.

**It rotates on every use.** A presented token is deleted and a fresh one issued
in the same write, so a token that leaks dies the next time the real owner
returns. And it is revoked outright on any password change (all three paths),
on ``.quit`` (a full account logout), on ``.remember off``, and with the
account.

Never logged: not the token, not the hash. The log lines around it say only that
a resume succeeded or failed, and from where — exactly like any other auth event.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from .atomicwrite import atomic_write_json

DAY_SECONDS = 86400

# How long a token survives without being used. Refreshed on every rotation, so
# a device you keep using stays remembered; one you abandon expires.
RESUME_TOKEN_DAYS = 30

# 32 bytes -> 256 bits of entropy, URL-safe so it rides a control frame with no
# escaping. Never derived from the password, and never reused.
TOKEN_BYTES = 32

# A presented token longer than this is not one of ours; refuse it without
# hashing. Bounds the pre-auth work a stranger can ask us to do.
MAX_TOKEN_LENGTH = 256

# ⛔ How many live tokens one account may hold (v1.52.2, external review).
#
# `issue()` minted without ever counting, so `resume.json` grew one record per
# remembered login for thirty days. ⚠ NOT an attack — you need the password to
# mint one — just untidy growth, and the untidiness is the whole reason to bound
# it: a file that only ever gets longer is one nobody can reason about.
#
# 20 is deliberately generous. Real people are not logged in on twenty devices;
# what this actually catches is a CHURN of logins from the same few browsers,
# which is the shape the growth really takes.
#
# ⚠ The eviction is oldest-first by `issued`, so the device you signed in on
# most recently is the last to go — which is the order a person would choose if
# they were asked.
MAX_TOKENS_PER_ACCOUNT = 20


def new_token() -> str:
    """A fresh opaque token. The ONLY place one is minted."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """The sha256 hex of a token — what goes on disk, and the lookup key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ResumeStore:
    """Opaque resume tokens, hashed, per account, auto-expiring.

    Persisted to ``resume.json`` (gitignored) with the same atomic temp+rename
    write as every other store. A wall clock is injected so expiry is testable.
    """

    def __init__(self, path, wall_clock=time.time, lifetime_days: float = RESUME_TOKEN_DAYS):
        self.path = Path(path)
        self._wall = wall_clock
        self.lifetime = float(lifetime_days) * DAY_SECONDS
        # sha256hex -> {"account": casefolded, "issued": float, "expires": float}
        self._tokens: dict[str, dict] = {}
        self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        self._tokens.clear()
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            # A corrupt token file must never stop the house opening: the worst
            # case is that everyone logs in with a password once.
            return
        now = self._wall()
        raw = data.get("tokens", {})
        if not isinstance(raw, dict):
            return
        for key, rec in raw.items():
            if not isinstance(rec, dict):
                continue
            account = str(rec.get("account", "")).casefold()
            try:
                expires = float(rec.get("expires", 0.0))
                issued = float(rec.get("issued", 0.0))
            except (TypeError, ValueError):
                continue
            if not account or expires <= now:
                continue                       # purge expired on load
            self._tokens[str(key)] = {"account": account, "issued": issued,
                                      "expires": expires}

    def _save(self) -> None:
        payload = {"version": 1, "tokens": self._tokens}
        atomic_write_json(self.path, payload)

    # ---- queries -----------------------------------------------------

    def purge_expired(self, now: float | None = None) -> int:
        """Drop every lapsed token. Returns how many went (a count, never a name)."""
        now = self._wall() if now is None else now
        dead = [k for k, r in self._tokens.items() if r["expires"] <= now]
        for k in dead:
            del self._tokens[k]
        if dead:
            self._save()
        return len(dead)

    def count_for(self, account: str) -> int:
        key = account.casefold()
        return sum(1 for r in self._tokens.values() if r["account"] == key)

    def resolve(self, token: str, now: float | None = None) -> str | None:
        """The casefolded account this token belongs to, or None.

        Read-only and total: an unknown, malformed, over-long or expired token is
        simply None. It never raises and never says *why* — a caller cannot
        distinguish "never existed" from "expired" from "revoked", which is the
        right amount for an unauthenticated stranger to learn.
        """
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return None
        rec = self._tokens.get(token_hash(token))
        if rec is None:
            return None
        now = self._wall() if now is None else now
        if rec["expires"] <= now:
            return None
        return rec["account"]

    # ---- mutations ---------------------------------------------------

    def issue(self, account: str, now: float | None = None) -> str:
        """Mint and store a token for ``account``; return the RAW token.

        The raw value is returned once and never stored — it goes straight out
        to the one browser that asked for it.
        """
        now = self._wall() if now is None else now
        key = account.casefold()
        token = new_token()
        self._tokens[token_hash(token)] = {
            "account": key,
            "issued": now,
            "expires": now + self.lifetime,
        }
        # ⛔ Bound what one account can accumulate (see MAX_TOKENS_PER_ACCOUNT).
        # Oldest-issued go first, so the most recent devices survive.
        #
        # ⚠ AFTER the insert rather than before, so the token just minted is
        # counted: capping first would let the account sit one over the limit
        # until the next login. And the ONE `_save()` below covers the insert and
        # the eviction together — a token file that had dropped the eviction but
        # kept the insert would be a file that grew despite the cap.
        own = sorted(
            (h for h, r in self._tokens.items() if r["account"] == key),
            key=lambda h: self._tokens[h]["issued"],
        )
        for h in own[:-MAX_TOKENS_PER_ACCOUNT]:
            del self._tokens[h]
        self._save()
        return token

    def rotate(self, token: str, now: float | None = None) -> str | None:
        """Consume ``token`` and return a fresh one for the same account.

        One write: the presented hash is deleted and the new one stored together,
        so there is no window in which both are valid and none in which neither
        is. Returns None if the presented token was not valid, having changed
        nothing.

        ⚠ Deliberately NOT capped, because it cannot grow the count: it deletes
        one and adds one, so an account that rotates for a decade holds exactly
        what it held before. Only a fresh `issue()` adds a device, so that is the
        only place the bound belongs.
        """
        now = self._wall() if now is None else now
        account = self.resolve(token, now)
        if account is None:
            return None
        del self._tokens[token_hash(token)]
        fresh = new_token()
        self._tokens[token_hash(fresh)] = {
            "account": account,
            "issued": now,
            "expires": now + self.lifetime,
        }
        self._save()
        return fresh

    def revoke(self, token: str) -> bool:
        """Drop one specific token. True if it existed."""
        if not token or len(token) > MAX_TOKEN_LENGTH:
            return False
        key = token_hash(token)
        if key not in self._tokens:
            return False
        del self._tokens[key]
        self._save()
        return True

    def revoke_account(self, account: str) -> int:
        """Drop EVERY token for an account. Returns how many.

        This is the one that matters: it is what a password change, `.quit`,
        `.remember off` and account deletion all reach for. Idempotent, and safe
        to call for an account that has never had a token.
        """
        key = account.casefold()
        dead = [k for k, r in self._tokens.items() if r["account"] == key]
        for k in dead:
            del self._tokens[k]
        if dead:
            self._save()
        return len(dead)
