# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Operational toggles + house-wide runtime settings for the whole talker.

Arch-controlled data that must survive a restart, all covenant-clean (facts and
config, never message content — the same class as ``bans.json``):
  - ``allow_new_accounts`` / ``allow_logins`` — the private-mode switches;
  - ``settings`` — the ``.ownerset`` house-wide strings (the login/logout,
    room enter/exit, afk and jail wording, the house desc/profile defaults, and
    the broadcast scope). Only OVERRIDES are stored; an absent key means the
    built-in default (see ``HOUSE_DEFAULTS``). This is the single source of truth
    for these — deliberately NOT theme.toml/defaults.txt — and since 1.16.0 that
    includes the room/afk wording, which used to live in defaults.txt and needed
    a restart to change;
  - ``jailed`` — the set of casefolded account names currently jailed. Jail is a
    PERSISTENT per-account status (a relog you can escape is no jail), so it lives
    here, in the daemon-owned state dir, next to the other moderation facts.

Persisted to ``state.json`` (gitignored) with the same atomic temp+rename write
as the other stores. Missing file = the historical, fully-open defaults.
"""

from __future__ import annotations

import json
from pathlib import Path

from .atomicwrite import atomic_write_json
from .permissions import DISABLED


# Built-in defaults for the `.ownerset` house-wide strings. A `.ownerset`
# override wins; otherwise these apply.
#
# Every message key here is a PHRASE (the verb part only) or a full standalone
# line, and which it is decides how the engine renders it. Three shapes, and the
# shape is the engine's, never the operator's:
#
#   login_msg / logout_msg   ->  [{name} {phrase}]        (presence_line)
#   jail_logon_msg           ->  [*** {name} {phrase} ***] (jail_presence_line)
#   enter_msg / exit_msg     ->  {name} {phrase}           (room, unbracketed)
#   afk_msg                  ->  {name} is AFK: {phrase}   (room, unbracketed)
#   jail_msg / release_msg /
#   jail_reply_msg           ->  the line itself, standalone, never name-prefixed
#
# So the owner sets WORDS and the house keeps its grammar: the brackets, the
# asterisks and the name can never be typed away, which is what makes comings
# and goings always recognisable as comings and goings.
#
# desc / profile are the HOUSE DEFAULTS for a resident's own .desc / .profile —
# absent-means-default, exactly like the message keys: an unset resident shows
# the house's words, and setting their own overrides it. Both ship EMPTY, so
# out of the box nothing shows that did not show before.
HOUSE_DEFAULTS = {
    "login_msg": "enters the world",
    "logout_msg": "leaves the world",
    "jail_logon_msg": "arrives (jail)",
    "enter_msg": "arrives.",
    "exit_msg": "slips out toward the <room>.",
    "afk_msg": "away for a while",
    "jail_msg": "You've been jailed.",
    "release_msg": "You have been released.",
    "jail_reply_msg": "Jailed residents cannot send tells or remotes.",
    "broadcast_scope": "world",
    "desc": "",
    "profile": "",
    # The three display SWITCHES an unset resident gets. They lived in
    # defaults.txt until v1.17.0, which meant the house had two config surfaces
    # for "what a resident sees by default" — one live, one needing a restart and
    # a file edit. They are here now, and defaults.txt is gone.
    #
    # Unlike the message keys these are read ONCE, when a session starts, so a
    # change reaches a resident at their NEXT LOGIN rather than immediately. That
    # is a deliberate, documented limit (making them truly live is a follow-up):
    # dim and time are session state the transport reads per line, and helpstyle
    # is read when .help renders. A resident's own `.set` always wins.
    "dim": "off",
    "time": "off",
    "helpstyle": "long",
    # The bounds `.raise_prompt` clamps to, in lines. The web client ships its
    # own fallback (0/10) for the moment before the frame arrives; these are the
    # house's word, pushed on connect and on change.
    "raise_prompt_min": "0",
    "raise_prompt_max": "10",
}

# `.ownerset` keys whose value is not free text. The validator is the whole
# definition — a house setting that silently accepts "maybe" is worse than one
# that refuses it.
ENUM_KEYS = {
    "broadcast_scope": ("world", "entry"),
    "dim": ("on", "off"),
    "time": ("on", "off"),
    "helpstyle": ("long", "short"),
}

HELP_STYLES = ENUM_KEYS["helpstyle"]        # ("long", "short")

# The three display switches, and the fact that they are display switches, is
# used in three places (the session's live resolution, `.set`, `.ownerset`).
SWITCH_KEYS = ("dim", "time", "helpstyle")


def house_switch(state, key: str):
    """The HOUSE value of a display switch, typed the way a session wants it:
    a bool for dim/time, the style word for helpstyle.

    Lives here, beside HOUSE_DEFAULTS, rather than in the command layer — the
    session resolves these per output unit now (v1.25.0) and importing the
    command package from `session` would be a cycle. A garbled stored value
    falls back to the built-in, so a hand-edited state.json can never make
    `.help` unrenderable.
    """
    raw = str(state.house_setting(key) if state is not None
              else HOUSE_DEFAULTS[key]).strip().lower()
    if key == "helpstyle":
        return raw if raw in HELP_STYLES else HOUSE_DEFAULTS["helpstyle"]
    return raw == "on"


# Keys whose value is a NON-NEGATIVE INTEGER. Validated as such, and the pair
# below is additionally validated against each other (min <= max) — a house whose
# floor is above its ceiling has no valid setting at all, so it is refused rather
# than silently normalised.
INT_KEYS = ("raise_prompt_min", "raise_prompt_max")
RAISE_BOUNDS_KEYS = ("raise_prompt_min", "raise_prompt_max")

# The three that a session reads at start rather than at each use.
SWITCH_KEYS = ("dim", "time", "helpstyle")


def raise_bounds(state) -> tuple[int, int]:
    """The house's `.raise_prompt` clamp, as ints, defaulted and ordered.

    Reads defensively: a hand-edited state.json cannot make the client's bounds
    nonsense, because a bad value falls back to the built-in and an inverted pair
    is swapped rather than left impossible.
    """
    def _int(key):
        raw = state.house_setting(key) if state is not None else HOUSE_DEFAULTS[key]
        try:
            n = int(str(raw).strip())
        except (TypeError, ValueError):
            n = int(HOUSE_DEFAULTS[key])
        return max(0, n)
    lo, hi = _int("raise_prompt_min"), _int("raise_prompt_max")
    return (lo, hi) if lo <= hi else (hi, lo)

# The world-wide presence keys whose value the engine wraps in brackets. Only
# these get the legacy-template self-heal (a pre-1.14 stored value was a whole
# `[{name} …]` template and would double-bracket); the unbracketed room keys
# take their value as written.
PHRASE_KEYS = ("login_msg", "logout_msg", "jail_logon_msg")

# Multi-line house settings — the ONE key whose value is not a single line, so
# it is set through the editor rather than as a command argument.
MULTILINE_KEYS = ("profile",)


def presence_line(name: str, phrase: str) -> str:
    """The ONE place a world login/logout line is formatted: `[{name} <phrase>]`."""
    return f"[{name} {phrase}]"


def jail_presence_line(name: str, phrase: str) -> str:
    """A JAILED resident's login line: `[*** {name} <phrase> ***]`.

    The asterisks are the engine's, not the phrase's — a jailed arrival has to be
    unmistakable at a glance, and that cannot be something the owner types away
    by rewording the phrase.
    """
    return f"[*** {name} {phrase} ***]"


def phrase_or_none(value):
    """A clean enter/leave PHRASE, or None if it's empty or a legacy full-template
    value that would double-bracket. A pre-1.14 stored value was a whole template
    like `[{name} enters the world]`; anything containing `{name}` or wrapped in
    brackets is treated as legacy, so callers fall back to the default phrase
    rather than rendering `[Name [Name enters the world]]`."""
    if not value:
        return None
    v = value.strip()
    if not v or "{name}" in v or v.startswith("[") or v.endswith("]"):
        return None
    return v


def house_phrase(state, key: str) -> str:
    """The house-default phrase for a message key: the owner's override if set,
    else the built-in default.

    For the three BRACKETED presence keys the override must also be a clean
    phrase — a legacy full template falls back to the default rather than
    double-bracketing. The unbracketed room keys have no such trap (nothing wraps
    them), so their value is taken as written.
    """
    if state is not None:
        value = state.house_setting(key)
        if key in PHRASE_KEYS:
            p = phrase_or_none(value)
            if p is not None:
                return p
        elif value:
            return value
    return HOUSE_DEFAULTS[key]


# ---------------------------------------------------------------------------
# The 1.16.0 house-setting rename
#
# One scheme for every message key, which meant renaming four of them. The remap
# below is applied in a SINGLE pass over the stored dict, before the known-key
# filter — and the single pass is the whole point. `enter_msg` exists on both
# sides of the rename with DIFFERENT meanings: it WAS the world-login phrase and
# it now names the room-arrival phrase. Remapping in one pass sends the stored
# old value to `login_msg` and leaves the new `enter_msg` unset (so it starts at
# its own default). A sequential rename, or a filter run first, would either drop
# live values on the floor or quietly re-point a world-login phrase at room
# arrivals — the only place this migration could mean the wrong thing.
# ---------------------------------------------------------------------------

SETTINGS_VERSION = 2

LEGACY_SETTING_KEYS = {
    "enter_msg": "login_msg",              # world login   (NOT the new room key)
    "leave_msg": "logout_msg",
    "enter_msg_jailed": "jail_logon_msg",
    "jail_reply_notice": "jail_reply_msg",
}


def migrate_house_settings(raw: dict) -> dict:
    """Pre-1.16 house-setting keys -> the current scheme, in one pass.

    Every stored value survives under its new name; keys that did not change
    (jail_msg, broadcast_scope) pass through untouched.
    """
    return {LEGACY_SETTING_KEYS.get(k, k): v for k, v in raw.items()}


# The command table's schema stamp. Bumped only if the SHAPE changes; the
# contents are the operator's, not ours, and are never re-seeded.
COMMANDS_VERSION = 1


class CommandTableError(ValueError):
    """A structurally corrupt command table. Refuses the boot, deliberately.

    Distinguished from a merely STALE value (a rank that was renamed away, or a
    typo): that degrades to arch-only with a warning, because locking one command
    down is a safe reading and closing the house is not. A table that is not even
    a mapping of strings has no safe reading at all.
    """


class StateStore:
    def __init__(self, path, command_seed=None, ladder=None):
        self.path = Path(path)
        self.allow_new_accounts = True
        self.allow_logins = True
        self.settings: dict[str, str] = {}   # .ownerset overrides only
        self.jailed: set[str] = set()        # casefolded account names
        # The authoritative command -> rank-anchor table (v1.28.0). Seeded from
        # the code registry on a fresh or upgrading house, then owned by this
        # file. `command_seed` is passed IN rather than imported, because
        # `commands` imports `session` which imports this module — the same
        # injection Phase B used for the account ladder.
        self._seed = dict(command_seed or {})
        self._ladder = ladder
        self.commands: dict[str, str] = {}
        # ⚠ The LEGACY `commands` block, if this file still has one (a house
        # upgrading from v1.28-v1.36). It is written back verbatim until
        # `permissions.toml` exists, because until then it is the only copy of
        # that operator's assignments — dropping it early would lose them, and
        # `--write-permissions` merges FROM it. The server sets
        # `drop_command_block` once the file is the source, and the next save
        # takes it out for good.
        self.legacy_commands: dict[str, str] = {}
        self.drop_command_block = False
        # Table values that resolve to no rank at all. Counted, never named; the
        # server says the number once. Each is locked to arch until fixed.
        self.unresolvable_commands = 0
        self._load()

    # ---- persistence -------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            # A house with no state.json is a fresh one: seed the table and
            # write it, so the file is the authority from the very first boot.
            if self._seed:
                self.commands = dict(self._seed)
                self._count_unresolvable()
                self._save()
            return
        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Absent/garbage keys fall back to open (True) — never lock the door by
        # accident on a malformed file.
        self.allow_new_accounts = bool(data.get("allow_new_accounts", True))
        self.allow_logins = bool(data.get("allow_logins", True))
        # Rename first, filter second — see migrate_house_settings for why the
        # order is load-bearing. Only known keys are kept afterwards (a retired
        # key in an old file is ignored, never fatal).
        raw = data.get("settings", {})
        migrated = False
        if isinstance(raw, dict):
            if int(data.get("settings_version", 1) or 1) < SETTINGS_VERSION:
                raw = migrate_house_settings(raw)
                migrated = True
            self.settings = {k: str(v) for k, v in raw.items() if k in HOUSE_DEFAULTS}
        # Drop a legacy full-template value stored under a bracketed presence key
        # (pre-1.14), so it self-heals to the default phrase instead of
        # double-bracketing.
        for k in PHRASE_KEYS:
            if k in self.settings and phrase_or_none(self.settings[k]) is None:
                del self.settings[k]
        jailed = data.get("jailed", [])
        if isinstance(jailed, list):
            self.jailed = {str(n).casefold() for n in jailed}
        # ⚠ THE COMMAND TABLE NO LONGER LIVES HERE (v1.37.0). It moved to
        # `permissions.toml`, beside config.toml, because config the operator
        # edits and state the daemon writes must not share a file — this one is
        # rewritten from memory on every `.ownerset`/`.jail`/`.logins`, and
        # for nine versions that silently ate hand edits.
        #
        # What remains is the READ, and only for the MIGRATION: a house upgrading
        # from v1.28-v1.36 still has a `commands` block here, and it is the
        # source `permissions.load()` writes the new file from, so an operator's
        # assignments survive the move exactly. `_save()` no longer writes it,
        # so the block is dropped from the file on the first save after the
        # upgrade and never comes back. The corrupt-table fail-fast stays for as
        # long as the read does: a block that cannot be read must not be
        # migrated into the new file as though it had been.
        if data.get("commands"):
            raw = data["commands"]
            if not isinstance(raw, dict):
                raise CommandTableError(
                    'Cannot start: state.json\'s "commands" table must be an '
                    'object of command -> rank, not %s. Fix it, or delete the '
                    '"commands" block to re-seed from the built-in defaults.'
                    % type(raw).__name__)
            for name, value in raw.items():
                if not isinstance(value, str):
                    raise CommandTableError(
                        'Cannot start: state.json\'s command table gives %r the '
                        'value %r, which is not a rank name. Fix the entry, or '
                        'delete the "commands" block to re-seed from the '
                        'built-in defaults.' % (name, value))
            self.commands = {str(k): v for k, v in raw.items()}
            self.legacy_commands = dict(self.commands)
        elif self._seed:
            self.commands = dict(self._seed)
            migrated = True
        self._count_unresolvable()
        # Persist the migration once, so the rename is done rather than redone on
        # every boot. Guarded by settings_version (and, for the table, by the
        # block simply being absent), so neither can run twice.
        if migrated:
            self._save()

    def set_command_table(self, table: dict) -> None:
        """Install the live command table — at boot, and on every reload.

        ⚠ **The one live path, and it is a single assignment on purpose.** The
        gate calls `command_rank()` on every dispatch and every session shares
        this one store, so replacing the contents of this dict IS the reload:
        a connection already open sees the new permissions on its next typed
        command, with nothing to refresh and no per-session copy to invalidate.

        The dict is copied, so the caller's object cannot be mutated underneath
        the running house afterwards. Nothing is written to `state.json` — the
        table's home is `permissions.toml` (see the note in `_load`).
        """
        self.commands = dict(table)
        self._count_unresolvable()

    def _count_unresolvable(self) -> None:
        """How many values name no rank — a rename orphan, or a typo.

        ⛔ `disabled` is EXCLUDED, and leaving it in was a live bug (found by
        v1.60.0's fresh-install rehearsal). It is the one legal value that is
        not a rank, so `resolve_anchor` returns None for it exactly as it does
        for a typo — and the boot then warned that N commands "name a rank that
        is not in [ranks] order and are locked to 'owner' until fixed". Both
        halves were false: the operator wrote a value the engine documents, and
        the command is ABSENT rather than owner-only (`command_disabled` is
        asked first and never reaches `command_rank`). The shipped skeleton
        disables `.reboot`, so every fresh install met this warning about its
        own untouched file.
        """
        if self._ladder is None:
            return
        self.unresolvable_commands = sum(
            1 for v in self.commands.values()
            if v != DISABLED and self._ladder.resolve_anchor(v) is None)

    # ---- the command table -------------------------------------------

    def command_disabled(self, name) -> bool:
        """Is this command switched OFF in this house? (v1.56.0)

        ⛔ Not "may this caller run it" — whether it EXISTS. `disabled` is the
        one permissions value that is not a rank, and it means the command is
        absent: no gate runs, nothing is listed, and typing it is a typo.

        ⚠ Asked BEFORE `command_rank`, never instead of interpreting its answer:
        a disabled value would fall through that method's unknown-rank fail-safe
        and read as owner-only, which is the one wrong answer — "only the owner"
        is exactly what a disable is not.
        """
        return self.commands.get(name) == DISABLED

    def command_rank(self, name, theme) -> str:
        """The rank a command requires, as a rank NAME on `theme`'s ladder.

        Two fail-safe rules, both pointing the same way — LESS access:

        * a command **not in the table** (new, or removed by hand) -> the ARCH
          rank. Anything unlisted is locked down, never accidentally open.
        * a value that resolves to no rank — a rename orphan, or a typo -> the
          ARCH rank too. The house still boots (the count is logged once); the
          command is simply out of reach until someone fixes it.
        """
        value = self.commands.get(name)
        if value is None:
            return theme.owner_rank
        return theme.resolve_anchor(value) or theme.owner_rank

    def _save(self) -> None:
        payload = {
            "version": 1,
            "settings_version": SETTINGS_VERSION,
            "commands_version": COMMANDS_VERSION,
            "allow_new_accounts": self.allow_new_accounts,
            "allow_logins": self.allow_logins,
            "settings": self.settings,
            "jailed": sorted(self.jailed),
        }
        # ⛔ The `commands` block is written back ONLY while it is still the
        # only copy of an upgrading house's table. Once `permissions.toml`
        # exists the server sets `drop_command_block` and the key disappears
        # here, for good — which is what ends the old clobber: from then on the
        # daemon's own file holds no permissions to overwrite an edit with.
        if self.legacy_commands and not self.drop_command_block:
            payload["commands"] = dict(sorted(self.legacy_commands.items()))
        atomic_write_json(self.path, payload)

    # ---- mutations ---------------------------------------------------

    def set_new_accounts(self, allowed: bool) -> None:
        self.allow_new_accounts = bool(allowed)
        self._save()

    def set_logins(self, allowed: bool) -> None:
        self.allow_logins = bool(allowed)
        self._save()

    # ---- house-wide settings (.ownerset) -------------------------

    def house_setting(self, key: str) -> str:
        """The live value of a house-wide string: the override if set, else the
        built-in default."""
        return self.settings.get(key, HOUSE_DEFAULTS[key])

    def set_house_setting(self, key: str, value: str) -> None:
        self.settings[key] = value
        self._save()

    def reset_house_setting(self, key: str) -> None:
        """Drop an override so the built-in default applies again."""
        if self.settings.pop(key, None) is not None:
            self._save()

    # ---- persistent jail status --------------------------------------

    def is_jailed(self, name: str) -> bool:
        return name.casefold() in self.jailed

    def set_jailed(self, name: str) -> None:
        self.jailed.add(name.casefold())
        self._save()

    def clear_jailed(self, name: str) -> None:
        if name.casefold() in self.jailed:
            self.jailed.discard(name.casefold())
            self._save()
