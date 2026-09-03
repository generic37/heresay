# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Theme — the words the world uses for itself, the LEVELS, and the RANK LADDER.

The house's own name (``instance_name`` in **config.toml**, threaded into the lines the engine
generates: ``.version``, the ``.map`` title, the examine rank line, the login
notices — and, since v1.22.0, the browser tab's title) plus the house's
``favicon``.

⚠ The default is deliberately the colourless ``the house``: a house that has not
named itself should read as unnamed rather than borrow a name it does not have.
A shipped ``theme.toml`` names the instance, and one still carrying the install
The house's NAME is not here — it is `config.toml [world] instance_name`.

## LEVELS and RANKS (v1.54.0 — the genericization)

**The engine reasons in LEVELS, never in rank words.** There are exactly four,
always, in this fixed order:

    newb < resident < staff < owner

- ``newb`` — a limited / unverified tier; an optional vetting floor.
- ``resident`` — a sanctioned member (this was the ``floor`` zone).
- ``staff`` — the moderation class. The shared staff mailbox (``STAFF_BOX``),
  staff-only rooms (``staff_only:``) and the staff/security views all key off
  THIS level.
- ``owner`` — the single protected, un-bannable supreme account (this absorbs the
  old ``arch``). Named in config by ``owner_name``.

Each level carries a **configurable singular AND plural display name** — its own
field, because English plurals are irregular (the staff level ships ``staff`` for
both). Generic defaults: ``newb``/``newbs``, ``resident``/``residents``,
``staff``/``staff``, ``owner``/``owners``. A house sets only what differs — a
house whose staff are called wardens sets ``[levels.staff] singular = "warden"``,
plural ``"wardens"``, and its screens read in its own words.

⛔ **A level word is never given an article by the engine.** ``staff`` is a mass
noun, so ``a {staff_word}`` reads "a staff"; every engine string that named a
level was reworded in v1.61.0 into a position that works for a mass noun and a
count noun alike (``the {plural}``, ``{singular} mail``). The default does not
bend to accommodate a sentence, and ``test_level_words.py`` holds the line.

**Ranks are ORDERED DATA** — ``[ranks] order`` is the ladder, lowest first, and a
rank's NAME is both its identity and the word users see for that RUNG (in
``.promote``/``.demote``/``.accounts``/``.examine``). Each rank falls into one of
the four levels, and **a level is the lowest rank assigned to it, inheriting
upward** — the anchors below cut the ladder into the four level bands.

    [ranks]
    order    = ["citizen", "warden", "Steward"]
    resident = "resident"     # optional; the lowest ESTABLISHED rank (was `floor`)
    staff    = "warden"       # the lowest STAFF rank

    [levels.staff]  singular = "warden"    plural = "wardens"
    [levels.owner]  singular = "Steward"   plural = "Stewards"

## ZONES / level bands

Three anchors carve the ladder; the owner caps it:

    [ newb … <resident ]  [ resident … <staff ]  [ staff … <owner ]  [ owner ]
      sign-on               established             staff              supreme

- ``newb`` — the lowest rank, ``order[0]``. DERIVED, never configured. New
  accounts land here by default; a house that wants a probationary rung puts one
  at the bottom of ``order``.
- ``resident`` — the lowest ESTABLISHED rank (the anchor formerly called
  ``floor``). Everything a plain member may do anchors here, so the newb zone is
  defined by what it is below.
- ``staff`` — the lowest staff rank; ``is_staff`` is ≥ this.
- ``owner`` — ``order[-1]``, held by the account config names as ``owner_name``.

Command gating, ``is_staff()``, ``.promote``/``.demote`` and every rank word read
this one structure.

Missing ``theme.toml`` → the built-in GENERIC defaults below, never fatal (like
``reboot.txt``, unlike ``world/``). A malformed ``[ranks]`` is likewise never
fatal: it falls back and logs one fact, because a typo in a words file must not
close the house. The ONE exception is enforced elsewhere, in the server: a rank
name that collides with an existing ACCOUNT name refuses the boot, because a rank
word is a staff-mail identity and the two cannot share a name.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("heresay.theme")

# The four LEVELS — fixed in number and order, the words the ENGINE reasons in.
LEVELS = ("newb", "resident", "staff", "owner")

# The built-in GENERIC ladder: what ships when there is no theme.toml, and what
# a malformed [ranks] falls back to. Deliberately generic — a house names its own
# ranks, and their words, in its own house/theme.toml.
DEFAULT_ORDER = ("resident", "staff", "owner")
DEFAULT_STAFF = "staff"

# Each level's default (singular, plural) DISPLAY names. A house overrides only
# what differs, per level, in [levels.<id>].
#
# ⛔ The staff level is `staff` / `staff` (v1.61.0). `staffer` is gone: it was a
# word nobody uses for one member of staff, and it read against itself the moment
# a page said "a staffer" beside "the staff". ⚠ The cost of the honest word is
# that `staff` is a MASS noun — "a staff" is not a person — which is why no
# engine string may put an article in front of a level word; see the reworded
# lines in `commands/` and `test_level_words.py`, which holds the rule.
DEFAULT_LEVEL_DISPLAY = {
    "newb": ("newb", "newbs"),
    "resident": ("resident", "residents"),
    "staff": ("staff", "staff"),
    "owner": ("owner", "owners"),
}

# A ladder needs at least a floor and an owner for "staff", "the rank below owner"
# and the promote cap to mean anything.
MIN_RANKS = 2

# Reserved rank names (a rung may not be NAMED one of these). These are the
# pure-ROLE words that would make a `permissions.toml` value ambiguous between
# the role and the rung, and that are never a legitimate ladder rung. ⚠ The level
# ids `resident`/`staff`/`owner` are deliberately NOT here: a house legitimately
# names its anchor rank after its level (the shipped default ladder has a rank
# named "resident" at order[0]), and `resolve_anchor` resolves the role word
# first, so `gag = "resident"` means the resident-level anchor either way.
#
# ⚠ `newb` is the only one left. `floor`/`arch`/`wizard` were the pre-v1.54 role
# words and left in v1.60.0 WITH the aliases that made them ambiguous: nothing
# reads any of them now, so reserving them would be reserving three words this
# engine has no use for — the operator's call, not ours (the same argument that
# retired `RESERVED_NAMES` in v1.44.0).
ANCHOR_NAMES = ("newb",)

# ⛔ And one name that is not merely reserved but BOOT-FATAL (v1.56.0): a rank
# may not be called `disabled`, because `disabled` is the permissions value that
# means "this command does not exist here".
#
# ⚠ Why this one refuses the boot where an ANCHOR_NAME is dropped with a
# warning. An anchor collision makes a permissions value ambiguous between a
# role and a rung, and the role wins — harmlessly, because both readings grant
# roughly the same access. A `disabled` collision makes a value ambiguous
# between "this rank and above" and "no such command", and those are opposite
# answers. Guessing either way is a SILENT permission change, and the gate is
# invisible by design, so nobody would find out. Refuse and say so.
#
# Casefolded, following the anchor rule: a rank called `Disabled` is one the
# parser could technically tell apart and no reader could.
DISABLED_RANK = "disabled"

# The anchors an operator SETS (newb is `order[0]`, owner is `order[-1]`, neither
# a setting). In ladder order, which is also the order they are validated in.
# ⚠ `resident` was called `floor` before v1.54.0; `floor` is still accepted as a
# deprecated alias key on load.
ANCHOR_KEYS = ("resident", "staff")


class ThemeError(ValueError):
    """A `[ranks]` block with no safe reading — the ONE fatal case in this file.

    ⚠ Everything else here falls back and logs: a typo in a words file must not
    close the house. The anchors are the exception, and only because of what they
    DO: `resident` decides who counts as established, so a house running with a
    misread anchor is a house quietly granting or withholding authority. That is
    not a display detail, and "fall back to something" would be picking a policy
    on the operator's behalf.

    Ordering is the same case. `resident <= staff` is not a style rule — an
    inverted pair makes the level bands overlap, and the collapse in `accounts.py`
    would then land people in a tier the operator never described.
    """


@dataclass(frozen=True)
class Theme:
    # The world's own name — what the engine calls this instance in the lines it
    # generates (`.version`, the `.map` title, the examine line, the login
    # notices). A retheme to another setting changes it here; the default keeps
    # every out-of-the-box string reading exactly as before.
    # ⛔ NOT read from theme.toml (v1.60.0). The house's NAME is instance
    # CONFIGURATION, not a theme word — it names the deployment rather than
    # describing its vocabulary — so it lives in `config.toml` and the server
    # assigns it here once, immediately after the theme loads. This field stays
    # because eight readers legitimately ask the theme for the house's name;
    # what changed is where the value comes from, and there is exactly one
    # source. ⚠ There is NO fallback read: a `theme.toml` still carrying the key
    # is warned about and IGNORED, never honoured.
    instance_name: str = "the house"
    # The house's tab icon: a path RELATIVE TO house_dir (an .svg or .png), or
    # empty for none. It lives here rather than in config.toml because it is
    # BRANDING — it belongs beside the instance's name, in the file the house
    # owns, and it deploys with house/ so adding one needs no /etc edit.
    favicon: str = ""
    # THE LADDER, lowest rank first. Validated at load; never empty.
    order: tuple[str, ...] = DEFAULT_ORDER
    # ---- the anchors -------------------------------------------------
    # `resident` names a rank in `order`; with `staff` it cuts the ladder into
    # level bands. Empty means "not set", which resolves to the default in the
    # property below rather than being written in here — so a Theme built by hand
    # in a test gets the same answer a house with no `[ranks] resident` gets.
    #
    # ⚠ There is NO `newb`/`owner` field: newb is `order[0]`, owner is
    # `order[-1]`, both derived.
    resident: str = ""
    # The lowest rank that counts as staff. If it does not name a rank in `order`
    # the house is NOT left without an answer: staff becomes the top rank alone
    # (see `staff_rank`) — restrictive, and never a startup error.
    staff: str = DEFAULT_STAFF
    # The four levels' (singular, plural) DISPLAY names. Absent entries fall back
    # to DEFAULT_LEVEL_DISPLAY at read time via `level_singular`/`level_plural`,
    # so a Theme built by hand in a test needs to name only what it cares about.
    level_display: dict = field(default_factory=dict)

    # name -> position, built once from `order` in __post_init__.
    _index: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        # frozen dataclass: build the lookup through object.__setattr__.
        object.__setattr__(self, "_index",
                           {name: i for i, name in enumerate(self.order)})

    # ---- the ladder ---------------------------------------------------

    def rank_index(self, name) -> int | None:
        """This rank's position, or None if it is not a rank at all."""
        return self._index.get(name)

    def index_or_floor(self, name) -> int:
        """Position, with an unknown rank read as the RESIDENT floor.

        The fail-safe direction, and the one used by every gate: a stored rank
        the ladder no longer knows must never be read as more authority than the
        lowest established rung. (`AccountStore` counts these at load and the
        server says so once — a rank RENAME would otherwise demote people
        silently.)
        """
        i = self._index.get(name)
        return 0 if i is None else i

    @property
    def owner_rank(self) -> str:
        """The top of the ladder. The owner ACCOUNT is still named in config
        (``owner_name``); this is only the rank that owner holds."""
        return self.order[-1]

    @property
    def grantable_rank(self) -> str:
        """The highest rank `.promote` may reach: the one just below owner.

        Owner is granted by config, never by a command — so on a two-rank ladder
        this is the floor, and `.promote` can grant nothing.
        """
        return self.order[-2] if len(self.order) >= 2 else self.order[0]

    @property
    def staff_rank(self) -> str:
        """The lowest rank that is staff — `staff` if it names a rank, else the
        TOP rank alone. Never an error: a house whose `staff` key is a typo gets
        the restrictive reading, not a refusal to start."""
        return self.staff if self.staff in self._index else self.owner_rank

    @property
    def staff_index(self) -> int:
        return self._index[self.staff_rank]

    def is_staff_rank(self, name) -> bool:
        return self.index_or_floor(name) >= self.staff_index

    # ---- the level bands (the zones) ---------------------------------

    @property
    def newb_rank(self) -> str:
        """Where a NEW ACCOUNT starts by default: **the bottom of the ladder,
        always**. DERIVED (``order[0]``), not configured — nothing can sit below
        the bottom, so the below-newb orphan cannot exist."""
        return self.order[0]

    @property
    def resident_rank(self) -> str:
        """The lowest ESTABLISHED rank — what "a plain resident" means, and what
        the `resident` permission anchor resolves to.

        ⚠ Unset -> `order[0]`, i.e. the same rank as `newb`, which collapses the
        newb band to nothing. That is the default ON PURPOSE: a house that has
        not asked for a probationary rung must not acquire one.
        """
        return self.resident if self.resident in self._index else self.order[0]

    @property
    def newb_index(self) -> int:
        return self._index[self.newb_rank]

    @property
    def resident_index(self) -> int:
        return self._index[self.resident_rank]

    def level_of(self, name) -> str:
        """The LEVEL a rank falls into — one of ``LEVELS``.

        ⚠ A rank the ladder no longer knows resolves to **newb**, the safest
        answer — not the resident floor. "We cannot place you" must mean the
        least authority.
        """
        i = self._index.get(name)
        if i is None:
            return "newb"
        if i == len(self.order) - 1:
            return "owner"
        if i >= self.staff_index:
            return "staff"
        if i >= self.resident_index:
            return "resident"
        return "newb"

    def level_anchor(self, name) -> str:
        """The anchor RANK of the level a rank falls in — the rank at the bottom
        of its band. This is what the reorg collapse lands people on.

        ⚠ There is deliberately no `owner` case here: the owner ACCOUNT is exempt
        from the collapse by NAME (before this is ever called), so the whole
        staff-and-above band anchors to the staff rank — exactly as the pre-v1.54
        `zone_anchor` did. `level_of` DOES distinguish the owner rank, for
        display; the two differ only at the top rung, which only the owner holds.
        """
        i = self._index.get(name)
        if i is None:
            return self.newb_rank
        if i >= self.staff_index:
            return self.staff_rank
        if i >= self.resident_index:
            return self.resident_rank
        return self.newb_rank

    def level_rank(self, level: str) -> str:
        """The anchor RANK a LEVEL id resolves to — where a new account lands
        when the house sets `landing_level` to this level. An unknown level id
        falls back to the newb floor, the safe direction."""
        return {
            "newb": self.newb_rank,
            "resident": self.resident_rank,
            "staff": self.staff_rank,
            "owner": self.owner_rank,
        }.get(level, self.newb_rank)

    # ---- level DISPLAY names -----------------------------------------

    def level_singular(self, level: str) -> str:
        """The singular display name for a level. ``level`` is one of ``LEVELS``.

        Precedence: an explicit ``[levels.<id>] singular`` wins; otherwise the
        level's own RANK WORD (the house's word for the rung that anchors it), so
        a warden house reads ``warden`` and a marshal house reads
        ``marshal`` with no extra config — the pre-v1.54 behaviour, where the staff
        anchor doubled as the display word. The generic default
        (``DEFAULT_LEVEL_DISPLAY``) is the last resort, and is what ``newb`` uses,
        because the newb band's anchor coincides with the resident rank when there
        is no probationary rung — so ``newb`` would otherwise read ``resident``.

        ⚠ Deviation from the genericization spec, flagged deliberately: it said
        the level display is "not inherited from a rank" and defaults to a generic
        word. Defaulting to the rank word instead preserves every existing
        house's wording and keeps the engine's emitted strings identical across
        the rename, at the cost that a house wanting a display word that differs
        from its staff rank's name must set ``[levels.staff] singular``. The
        plural is where the two genuinely diverge, and that stays explicit —
        except for the engine's own default words, whose plurals it knows (see
        ``level_plural``).
        """
        pair = self.level_display.get(level)
        if pair and pair[0]:
            return pair[0]
        if level == "newb":
            return DEFAULT_LEVEL_DISPLAY["newb"][0]
        return self.level_rank(level)

    def level_plural(self, level: str) -> str:
        """The plural display name — explicit config, else a naive plural of the
        singular (``warden`` -> ``wardens``, ``marshal`` -> ``marshals``), which is
        the pre-v1.54 ``staff_plural`` default. A level whose plural is irregular
        sets it in ``[levels.<id>] plural``.

        ⚠ **The engine knows the plurals of its OWN default words** (v1.61.0):
        where the resolved singular is the generic default for that level, the
        generic plural is returned rather than the naive one. That is the whole
        of what makes the shipped ``staff`` level read as ``staff`` and not
        ``staffs`` — a house whose staff RANK is called ``staff`` (which is now
        the shipped ladder) inherits it with no ``[levels]`` block at all. It
        cannot reach any other word: an operator's ``warden`` is not a default,
        so it still pluralises naively, and an explicit ``plural`` still wins
        over everything."""
        pair = self.level_display.get(level)
        if pair and len(pair) > 1 and pair[1]:
            return pair[1]
        if level == "newb":
            return DEFAULT_LEVEL_DISPLAY["newb"][1]
        singular = self.level_singular(level)
        default_singular, default_plural = DEFAULT_LEVEL_DISPLAY.get(
            level, ("", ""))
        if singular == default_singular:
            return default_plural
        return singular + "s"

    # The two levels the engine names most, as convenience properties. These are
    # DISPLAY words (what a screen prints), distinct from the anchor RANK names
    # (`staff_rank`, `owner_rank`) the ladder logic uses.
    @property
    def staff_word(self) -> str:
        return self.level_singular("staff")

    @property
    def staff_plural(self) -> str:
        return self.level_plural("staff")

    @property
    def owner_word(self) -> str:
        return self.level_singular("owner")

    @property
    def owner_plural(self) -> str:
        return self.level_plural("owner")

    def resolve_anchor(self, value):
        """A command table value -> a rank NAME, or None if it resolves to nothing.

        The role words `newb`/`resident`/`staff`/`owner` are resolved live, so a
        house that renames its ranks keeps every permission. Anything else must
        be a rank NAME on this ladder (`ban_ip = "High Warden"` is legal and
        means that rank and everyone above). A value that is neither a role nor a
        rank returns None, an ERROR the caller refuses — see `permissions.parse`.

        ⛔ The pre-v1.54 role words `floor`/`arch`/`wizard` resolve to NOTHING
        since v1.60.0, which makes them errors the caller refuses rather than
        silently-accepted synonyms.
        """
        if value == "newb":
            return self.newb_rank
        if value == "resident":
            return self.resident_rank
        if value == "staff":
            return self.staff_rank
        if value == "owner":
            return self.owner_rank
        return value if value in self._index else None

    def rank(self, name) -> str:
        """The word for a rank RUNG. A rank's name IS its display word, so this is
        identity for anything on the ladder and the resident floor's name for
        anything else — the same fail-safe reading as `index_or_floor`."""
        return name if name in self._index else self.resident_rank

    # ---- the reorg fingerprint ----------------------------------------

    def rank_fingerprint(self) -> str:
        """A stable digest of the LADDER SHAPE: the order plus the two set anchors.

        What it is for: `accounts.py` collapses everyone into their level band
        when the shape changes, and must NOT touch a single rank when it has not.
        So the fingerprint covers exactly the things a collapse depends on and
        nothing else — rename the instance, change the favicon or a level's
        display words and no one's rank moves.

        The owner is `order[-1]` and newb is `order[0]`, so both are covered by
        `order`. The anchors are included RESOLVED rather than as written, so
        `resident = "resident"` and an absent `resident` on a ladder starting
        with `resident` are the same shape — which they are, and a spurious
        collapse is exactly what this exists to prevent.
        """
        import hashlib
        parts = list(self.order) + [self.resident_rank, self.staff_rank]
        raw = "\n".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


DEFAULT_THEME = Theme()


def _clean_order(raw, path) -> tuple[str, ...]:
    """`order` from the file, or the built-in ladder. Never raises, never fatal.

    Anything wrong here is a words file with a typo in it, and the house has to
    open anyway — so each problem falls back and says so once, in facts.
    """
    if raw is None:
        return ()          # "absent", distinct from "present but unusable"
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        log.warning("%s: [ranks] order must be a list of names — using the "
                    "built-in ladder %s", path, list(DEFAULT_ORDER))
        return DEFAULT_ORDER
    names, seen = [], set()
    for n in (s.strip() for s in raw):
        if not n:
            continue
        if n.casefold() in ANCHOR_NAMES:
            log.warning("%s: [ranks] order may not contain %r — it is a reserved "
                        "role anchor used by the command table; dropping it",
                        path, n)
            continue
        if n in seen:
            log.warning("%s: [ranks] order lists %r twice — keeping the first",
                        path, n)
            continue
        seen.add(n)
        names.append(n)
    if len(names) < MIN_RANKS:
        log.warning("%s: [ranks] order needs at least %d ranks (got %d) — using "
                    "the built-in ladder %s", path, MIN_RANKS, len(names),
                    list(DEFAULT_ORDER))
        return DEFAULT_ORDER
    return tuple(names)


def _load_levels(data, path) -> dict:
    """Read the `[levels.<id>]` singular/plural display names.

    Never fatal: an unknown level id is warned and ignored, a missing singular or
    plural falls back to the generic default at read time. A house sets only what
    differs from generic.
    """
    raw = data.get("levels", {})
    if not isinstance(raw, dict):
        log.warning("%s: [levels] must be a table — using generic level names",
                    path)
        return {}
    out = {}
    for lvl, entry in raw.items():
        if lvl not in LEVELS:
            log.warning("%s: [levels] has unknown level %r — the levels are %s; "
                        "ignoring it", path, lvl, list(LEVELS))
            continue
        if not isinstance(entry, dict):
            log.warning("%s: [levels.%s] must be a table with singular/plural — "
                        "ignoring it", path, lvl)
            continue
        sing = str(entry.get("singular", "")).strip()
        plur = str(entry.get("plural", "")).strip()
        out[lvl] = (sing, plur)
    return out


def load_theme(path) -> Theme:
    """Load the rank ladder and the level display words; missing file → generic
    defaults. ⛔ NOT the house's name — that is `config.toml [world]
    instance_name` since v1.60.0, and the server assigns it after this returns."""
    p = Path(path)
    if not p.exists():
        return Theme()
    with p.open("rb") as fh:
        data = tomllib.load(fh)
    _warn_moved_instance_name(data, p)
    r = data.get("ranks", {})
    if not isinstance(r, dict):
        r = {}
    d = Theme()

    order = _clean_order(r.get("order"), p) or DEFAULT_ORDER

    # `staff` defaults to the rank just below owner, which on a three-rank ladder
    # is order[-2] — so a house that sets only `order` still gets a sensible staff
    # line without having to say it twice.
    default_staff = order[-2] if len(order) >= 2 else order[0]
    staff = str(r.get("staff", default_staff)).strip() or default_staff
    if staff not in order:
        log.warning("%s: [ranks] staff = %r is not in order %s — only the top "
                    "rank (%r) will count as staff", p, staff, list(order),
                    order[-1])

    # ⚠ A `[ranks] newb` key is RETIRED (since v1.41.0) — the newb rank is the
    # bottom of `order` and is not configurable. Warned about and ignored, never
    # fatal: a house that already wrote one must not be closed by a line it was
    # once told to write. (`[levels.newb]`, the DISPLAY name, is a different key
    # and is honoured — see `_load_levels`.)
    if r.get("newb") is not None:
        log.warning("%s: [ranks] newb is retired and ignored — the newb rank is "
                    "the bottom of `order` (%r) and is no longer configurable",
                    p, order[0])

    # ---- the resident anchor, and the ONE fatal case in this file --------
    #
    # ⚠ `resident` is validated STRICTLY, unlike everything above. A typo in
    # `instance_name` is cosmetic; a typo in `resident` decides who counts as
    # established, and there is no "restrictive" direction to fall back to the way
    # `staff` has one. Refusing is the only reading that does not pick a policy on
    # the operator's behalf. It defaults to `order[0]` when ABSENT —
    # present-but-wrong is the error, absent is not.
    #
    # ⛔ The pre-v1.54 spelling `floor` was DELETED in v1.60.0 with the rest of
    # the back-compat layer. A file still using it has an unknown key, which
    # `[ranks]` ignores like any other — so the anchor falls back to `order[0]`
    # rather than being read. Nothing else in the engine knows the word.
    resident_raw = r.get("resident")
    if resident_raw is None:
        resident = ""            # unset -> the property's default
    else:
        name = str(resident_raw).strip()
        if name not in order:
            raise ThemeError(
                f"{p}: [ranks] resident = {name!r} is not a rank in order "
                f"{list(order)}. Fix the name (or remove the line — it defaults "
                f"to {order[0]!r}).")
        resident = name

    level_display = _load_levels(data, p)

    theme = Theme(
        favicon=str(data.get("favicon", d.favicon)),
        order=order,
        resident=resident,
        staff=staff,
        level_display=level_display,
    )
    # ⚠ ORDERING, still fatal: `newb <= resident` is AUTOMATIC now that newb is
    # `order[0]`, so that whole class of inverted-anchor mistakes is gone by
    # construction. An inverted resident/staff would still overlap the bands and
    # land people in a tier the operator never described.
    # ⛔ Boot-fatal, and deliberately not a fallback: see DISABLED_RANK above.
    clash = [n for n in order if n.casefold() == DISABLED_RANK]
    if clash:
        raise ThemeError(
            f"{p}: [ranks] order names a rank {clash[0]!r}, which collides with "
            f"the permissions value {DISABLED_RANK!r} — that value means a "
            f"command does not exist in this house, so a rank of the same name "
            f"would make every permissions line ambiguous. Rename the rank.")
    if not (theme.resident_index <= theme.staff_index):
        raise ThemeError(
            f"{p}: [ranks] anchors are out of order — resident="
            f"{theme.resident_rank!r} is above staff={theme.staff_rank!r} in "
            f"{list(order)}. They must run resident <= staff up the ladder.")
    return theme


# ⚠ `instance_name` MOVED to `config.toml` in v1.60.0. A theme file still
# carrying the key is warned about and ignored — never read, never a fallback.
def _warn_moved_instance_name(data: dict, path) -> None:
    if "instance_name" in data:
        log.warning("%s: instance_name has MOVED to config.toml (v1.60.0) and "
                    "is IGNORED here — set [world] instance_name there and "
                    "delete this line", path)


