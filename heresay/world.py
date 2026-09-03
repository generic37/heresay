# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The world: rooms and residents, held entirely in memory.

Room *definitions* come from the ``world/`` directory (disk, operator-editable):
one plain-text file per room plus ``map.txt``. What you edit is what runs —
there is no generator and no intermediate artifact. Who is where, and everything
said, lives only in this process and is never written down.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:  # avoid a runtime import cycle
    from .session import Session

log = logging.getLogger("heresay")

TOPIC_MAX = 60

# The entry room's fading memory (.rev): every line from the last window,
# capped at the newest N. Defaults; the server overrides from config.
REVIEW_WINDOW_SECONDS = 30 * 60.0
REVIEW_MAX_LINES = 100

# Atmospherics: an echo fires after T seconds of quiet, T drawn uniformly from
# this range. Defaults; the server overrides from config. The echo's "speaker"
# in the review buffer is this sentinel — not a real name, so no viewer's mute
# can ever hide it and it can never collide with an account.
ECHO_MIN_SECONDS = 10 * 60.0
ECHO_MAX_SECONDS = 15 * 60.0
ECHO_SPEAKER = "\x00echo"


@dataclass
class Room:
    id: str
    name: str
    description: str
    # The author's own 40-column description, from `<id>.narrow.txt` beside the
    # room file, or "" — the same silently-optional convention as the narrow map
    # and greeting. Empty is the ordinary case: a narrow session AUTO-REFLOWS the
    # wide prose instead (see `_shared.room_description`). A file here is for a
    # room whose layout is deliberate and would flatten — a list, a verse, a
    # scrap of art, mirror writing.
    description_narrow: str = ""
    # DIRECTED: where you can go FROM here. Two files naming each other make a
    # two-way passage; one side only is a one-way passage (legal, and listed at
    # startup so a typo is visible); an empty list is a sealed room.
    exits: list[str] = field(default_factory=list)
    lockable: bool = False
    staff_only: bool = False  # deck: off-map, staff only
    # The line a resident refused entry sees (`staff_only` rooms only). Empty means
    # the built-in neutral default; the deck's own wording lives in its file.
    refusal: str = ""


@dataclass
class Invite:
    """A pending invitation, held on the invited resident. In memory only."""

    inviter: str        # display name
    room_id: str        # the inviter's room at invite time (expires if they move)
    expires_at: float   # monotonic deadline


class Resident:
    """A logged-in person. Survives brief disconnects (grace period)."""

    def __init__(self, name: str, description: str, room_id: str,
                 rank: str = "", ladder=None):
        self.name = name
        self.description = description
        self.room_id = room_id
        # The rank NAME from the ladder (v1.27.0). The ladder itself rides along
        # because `is_staff()` is a threshold now and a Resident has no other
        # route to the theme; both are set together by `set_rank` so they can
        # never drift. An empty rank reads as the floor.
        from .theme import DEFAULT_THEME
        self.ladder = ladder if ladder is not None else DEFAULT_THEME
        self.rank = rank or self.ladder.resident_rank
        self.session: Optional["Session"] = None
        # session-scoped, in-memory mute set (casefolded names)
        self.muted: set[str] = set()
        # staff-imposed, session-scoped talker-wide silence
        self.gagged: bool = False
        # Jailed: confined to the jail room, cannot reach outside it. A LIVE mirror
        # of the persistent status in StateStore.jailed — set at login and by
        # .jail/.free, so the command layer can check res.jailed without the store.
        self.jailed: bool = False
        # AFK: session-scoped presence flag (distinct from idle time), + message
        self.afk: bool = False
        self.afk_message: str = ""
        # The resident's own wording for their comings and goings, loaded from
        # the account at login: {"enter_msg"|"exit_msg"|"afk_msg"|"login_msg"|
        # "logout_msg"}. A key that is absent means "use the house default". Kept
        # HERE rather than on the session because the logout announcement
        # outlives it.
        self.messages: dict[str, str] = {}
        # The three display SWITCHES this resident has explicitly chosen —
        # {"dim"|"time"|"helpstyle": value}. Same rule as `messages` above, and
        # the rule is the whole reason they are live (v1.25.0): ONLY keys they
        # actually set are here. An unset key is ABSENT, never mirrored as the
        # current house value, so there is nothing stale to go out of date and
        # the fall-through reads the live `.ownerset` value every time.
        # Held on the Resident rather than the Session so it survives a grace
        # reattach, exactly like `messages`.
        self.switches: dict[str, object] = {}
        # The .ignore noise filter: {category: True} for categories this person
        # has chosen NOT to see. Absent/False means show, which is the default —
        # a new resident hears everything. Lives here, beside `muted`, for the
        # same reason: broadcasting asks the RECIPIENT what they want to see.
        self.ignores: dict[str, bool] = {}
        # sticky target for .tell/.remote: display name of the last person you
        # successfully messaged (shared by both verbs). Session-scoped.
        self.correspondent: Optional[str] = None
        # a pending invitation addressed to this person (or None)
        self.pending_invite: Optional[Invite] = None
        # monotonic timestamp of last input line, for idle display
        self.last_input: float = 0.0
        # set while the connection is gone but within the grace window
        self.grace_task = None
        self.in_grace = False

    @property
    def connected(self) -> bool:
        return self.session is not None

    def set_rank(self, rank: str, ladder=None) -> None:
        """Move this resident on the ladder. The one place rank changes, so the
        rank and the ladder it is read against cannot get out of step."""
        if ladder is not None:
            self.ladder = ladder
        self.rank = rank or self.ladder.resident_rank

    def is_staff(self) -> bool:
        """Staff is a THRESHOLD, not a pair of names: this rank at or above the
        ladder's `staff` rank. A rank inserted below it is not staff; one at or
        above it is — with no code change anywhere. Governs the locked-door and
        staff-only passages, the gag-appeal channel, the staff letterbox notice
        and who `.call_staff` pages."""
        return self.ladder.is_staff_rank(self.rank)

    def sees(self, speaker_name: str) -> bool:
        return speaker_name.casefold() not in self.muted

    def hears_category(self, category: Optional[str]) -> bool:
        """Does this person want to see lines of this `.ignore` category?

        Uncategorised output (category None) is always heard — the filter is
        opt-in per category, so anything not named by one is unaffected. This is
        the counterpart to `sees()`: `.mute` filters a PERSON, `.ignore` filters
        a KIND OF LINE, and both are applied to the recipient at delivery.
        """
        if category is None:
            return True
        return not self.ignores.get(category, False)


class World:
    def __init__(self, rooms: dict[str, Room], ascii_map: str, start_room: str,
                 jail_room: Optional[str] = None,
                 one_way: Optional[list[tuple[str, str]]] = None,
                 ascii_map_narrow: str = ""):
        self.rooms = rooms
        self.ascii_map = ascii_map
        # The portrait-phone map, or "" — see map_for().
        self.ascii_map_narrow = ascii_map_narrow
        # Both are named in `config.toml [world]` (v1.17.0), validated against
        # the loaded rooms before we get here: which room is the front door is a
        # property of the deployment, so the same house/ can be run with a
        # different entry without editing a room file. There may be no jail at
        # all — then jail_room is None and the jailer's verbs say so plainly.
        self.start_room = start_room
        self.jail_room = jail_room
        # Passages declared from one side only. Not an error — a one-way door is
        # a legitimate thing to author — but the server names them at startup so
        # a missing line reads as a fact instead of a mystery.
        self.one_way: list[tuple[str, str]] = list(one_way or [])
        self.staff_only_rooms = {rid for rid, r in rooms.items() if r.staff_only}
        # residents keyed by casefolded name
        self.residents: dict[str, Resident] = {}
        # in-memory room lock state (restart empties the world, so nothing to persist)
        self.locked_rooms: set[str] = set()
        # Per-room topic, set by anyone present. Pure room-lifetime memory: it
        # evaporates when the last person leaves, and again on restart. Never
        # stored, never logged.
        self.topics: dict[str, str] = {}
        # The entry room's fading public memory: every rendered say/emote (and
        # atmospheric echo) from the last `review_window` seconds, capped at the
        # newest `review_max`. Held as (timestamp, speaker, line, category) so
        # BOTH of a viewer's filters apply at view time — their mutes (by
        # speaker) and their `.ignore`s (by category) — and expiry is pruned
        # lazily. `category` is None for ordinary speech and the `.ignore` key
        # for anything ignorable ("atmospherics" today). ONLY the entry
        # room has one. In memory only — never on disk, never logged, empty again
        # on restart. It does NOT clear when the room empties (an arrival seeing
        # the room was alive a minute ago is the point) — but it does fade, so an
        # idle room's memory genuinely empties over time.
        self.review: list[tuple[float, str, str, Optional[str]]] = []
        self.review_window: float = REVIEW_WINDOW_SECONDS
        self.review_max: int = REVIEW_MAX_LINES

        # Atmospherics — the entry room murmurs after a stretch of quiet. Lines
        # are loaded from world/echoes.txt (empty = the feature is silent). State
        # is entry-room-scoped and RAM-only: the reference moment the current
        # quiet started, the interval drawn for the next echo, and the last line
        # fired (so we never repeat immediately). The rng is injectable for tests.
        self.echoes: list[str] = []
        self.echo_min: float = ECHO_MIN_SECONDS
        self.echo_max: float = ECHO_MAX_SECONDS
        self._echo_rng = random.Random()
        self._echo_quiet_since: Optional[float] = None
        self._echo_interval: Optional[float] = None
        self._echo_last: Optional[str] = None

    # ---- residents ---------------------------------------------------

    def get_resident(self, name: str) -> Optional[Resident]:
        return self.residents.get(name.casefold())

    def add_resident(self, res: Resident) -> None:
        self.residents[res.name.casefold()] = res

    def remove_resident(self, res: Resident) -> None:
        self.residents.pop(res.name.casefold(), None)

    def resolve_resident(self, query: str) -> tuple[Optional[Resident], bool]:
        """Resolve a typed name to a connected resident. **EXACT ONLY.**

        The live-people half of the same rule as `AccountStore.resolve`
        (v1.52.0) — see there for why abbreviation went. This is the resolver
        behind `.mute`, `.unmute`, `.tell`, `.remote`, `.gag`, `.jail` and
        `.boot`, which is most of the list where getting the wrong person
        matters.

        Case-insensitive, never abbreviated. ⚠ `ambiguous` is always False now
        and the tuple is kept for the callers' sake; see `AccountStore.resolve`.

        ⚠ Unchanged: someone present but NOT connected does not resolve. The old
        prefix scan filtered on `connected` too, so a disconnected exact match
        was never findable here either.
        """
        q = query.strip().casefold()
        if not q:
            return None, False
        res = self.residents.get(q)
        return (res, False) if res is not None and res.connected else (None, False)

    def occupants(self, room_id: str, *, connected_only: bool = True) -> list[Resident]:
        out = []
        for r in self.residents.values():
            if r.room_id != room_id:
                continue
            if connected_only and not r.connected:
                continue
            out.append(r)
        return out

    def all_connected(self) -> list[Resident]:
        return [r for r in self.residents.values() if r.connected]

    # ---- rooms -------------------------------------------------------

    def map_for(self, narrow: bool) -> str:
        """The map art for this session's width, falling back to the wide one.

        The fallback is the whole point of the `.narrow.txt` convention: a house
        that has not drawn a narrow map still shows a map, just a wide one.
        """
        if narrow and self.ascii_map_narrow:
            return self.ascii_map_narrow
        return self.ascii_map

    def room(self, room_id: str) -> Optional[Room]:
        return self.rooms.get(room_id)

    def adjacent(self, a: str, b: str) -> bool:
        """Resident movement graph: can a resident .go from a to b?

        Exactly what a's file says, and nothing else. There is no hub rule any
        more — a hub room is an ordinary room whose file happens to name
        twenty-three destinations — and no implicit exclusions: the jail and the
        staff room are unreachable because nothing lists them, which is a fact
        about the world's files rather than a rule hidden in the code.
        """
        if a == b or a not in self.rooms or b not in self.rooms:
            return False
        return b in self.rooms[a].exits

    def exits_of(self, room_id: str) -> list[str]:
        """Room ids a resident could .go to from here — what .look shows.

        The room's own list, in the order its file writes it.
        """
        if room_id not in self.rooms:
            return []
        return list(self.rooms[room_id].exits)

    # ---- locks (in-memory) -------------------------------------------

    def is_locked(self, room_id: str) -> bool:
        return room_id in self.locked_rooms

    def lock(self, room_id: str) -> None:
        self.locked_rooms.add(room_id)

    def unlock(self, room_id: str) -> None:
        self.locked_rooms.discard(room_id)

    def unlock_if_empty(self, room_id: str) -> None:
        """Locks lift automatically when the room empties."""
        if room_id in self.locked_rooms and not self.occupants(room_id):
            self.locked_rooms.discard(room_id)

    # ---- topics (room-lifetime memory) --------------------------------

    def topic_of(self, room_id: str) -> str:
        return self.topics.get(room_id, "")

    def set_topic(self, room_id: str, text: str) -> None:
        self.topics[room_id] = text

    def clear_topic_if_empty(self, room_id: str) -> None:
        """A topic belongs to the gathering, not the room: when the last person
        leaves, it goes with them. (Same lifetime rule as a lock.)"""
        if room_id in self.topics and not self.occupants(room_id):
            del self.topics[room_id]

    def room_emptied(self, room_id: str) -> None:
        """Everything that lapses when the last occupant leaves a room."""
        self.unlock_if_empty(room_id)
        self.clear_topic_if_empty(room_id)

    # ---- the entry room's fading public memory ------------------------

    def _prune_review(self, now: float) -> None:
        """Drop lines older than the window, then keep only the newest `max`.
        Lazy — called on every append and every read, so nothing has to run on
        a timer."""
        cutoff = now - self.review_window
        kept = [e for e in self.review if e[0] >= cutoff]
        if len(kept) > self.review_max:
            kept = kept[-self.review_max:]
        self.review = kept

    def record_review(self, room_id: str, speaker: str, line: str,
                      now: float, category: Optional[str] = None) -> None:
        """Remember one rendered say/emote/echo line — ONLY in the entry room.

        Thinks, shouts, tells and announcements are never buffered. Nothing
        here is ever written to disk or to a log. Expiry is pruned lazily here.

        ``category`` is the `.ignore` key this line would be filtered by when
        delivered LIVE, or None for ordinary speech (which no category hides).
        It is recorded so a replay can make the same decision — see
        `review_lines`. Buffering it costs one small string per line and is what
        keeps the buffer whole while letting each viewer read their own version.
        """
        if room_id != self.start_room:
            return
        self.review.append((now, speaker, line, category))
        self._prune_review(now)

    def review_lines(self, viewer: "Resident", now: float) -> list[str]:
        """The buffer, oldest first, faded to the window, with the viewer's muted
        speakers AND their ignored categories dropped.

        All three filters happen HERE, at view time, and that is the design: the
        buffer stays whole and each viewer reads their own version of it — two
        people with different mutes and ignores get two different `.rev`s from
        one list, and the fade is measured against the reading moment.

        **`.rev` replays the room as this viewer would have experienced it
        live** (v1.29.4). Before, an atmospheric suppressed by `.ignore
        atmospherics` still turned up in the replay — the filter was applied at
        delivery and the buffer remembered what delivery had dropped. Playing
        back a line someone has asked not to be shown is the same failure
        whether it happens now or thirty minutes later.
        """
        self._prune_review(now)
        ignores = viewer.ignores or {}
        return [line for _ts, speaker, line, category in self.review
                if viewer.sees(speaker) and not (category and ignores.get(category))]

    # ---- atmospherics (the entry room murmurs) ------------------------

    def _draw_interval(self) -> float:
        lo, hi = self.echo_min, self.echo_max
        return self._echo_rng.uniform(lo, hi) if hi > lo else lo

    def note_entry_speech(self, room_id: str, now: float) -> None:
        """A resident spoke (say/emote) — if it was in the entry room, the quiet
        clock restarts and the next interval is redrawn. Any other verb, or any
        other room, leaves the echo gate alone."""
        if room_id != self.start_room:
            return
        self._echo_quiet_since = now
        self._echo_interval = self._draw_interval()

    def _pick_echo(self) -> Optional[str]:
        """One line at random, never the same one twice running."""
        if not self.echoes:
            return None
        if len(self.echoes) == 1:
            return self.echoes[0]
        choices = [e for e in self.echoes if e != self._echo_last]
        return self._echo_rng.choice(choices or self.echoes)

    def maybe_echo(self, now: float) -> Optional[str]:
        """The atmospherics tick. Returns a line to broadcast (already recorded
        into the review buffer), or None.

        Fires only while the entry room is OCCUPIED and only after the drawn
        quiet interval has elapsed. In an empty room it fires nothing and holds
        the clock at `now`, so the quiet never accumulates with no one there —
        the interval is measured against occupied silence, and an arrival does
        not walk into an immediate echo.
        """
        if not self.echoes:
            return None
        occupied = bool(self.occupants(self.start_room))
        if not occupied:
            self._echo_quiet_since = now        # clock paused while empty
            return None
        if self._echo_quiet_since is None or self._echo_interval is None:
            self._echo_quiet_since = now
            self._echo_interval = self._draw_interval()
            return None
        if now - self._echo_quiet_since < self._echo_interval:
            return None
        line = self._pick_echo()
        if line is None:
            return None
        self._echo_last = line
        # Redraw and restart the quiet clock from the fire moment — continued
        # silence brings another echo one interval later. The echo does NOT count
        # as speech (it does not reset via note_entry_speech), it just paces the
        # next one. It enters the review buffer like any say/emote — tagged with
        # the category it is DELIVERED under, so a viewer who has ignored the
        # room's murmurs does not meet them again thirty minutes later in .rev.
        self._echo_quiet_since = now
        self._echo_interval = self._draw_interval()
        self.record_review(self.start_room, ECHO_SPEAKER, line, now,
                           category="atmospherics")
        return line

    def resolve_room(self, query: str) -> tuple[Optional[str], bool]:
        """Resolve a user-typed room reference.

        Returns (room_id, ambiguous). room_id is None when nothing matches.
        Matching: exact id, then exact name (case-insensitive), then unambiguous
        prefix of the name.
        """
        q = query.strip().lower()
        if not q:
            return None, False
        # Resolve any room by id or name; whether one may actually GO there
        # (jail off-map, deck staff-only, locks, staff direct travel) is
        # decided by the .go handler, not here.
        rooms = self.rooms
        if q in rooms:
            return q, False
        # exact name match
        for rid, room in rooms.items():
            if room.name.lower() == q:
                return rid, False
        # prefix match on name
        hits = [rid for rid, room in rooms.items() if room.name.lower().startswith(q)]
        if len(hits) == 1:
            return hits[0], False
        if len(hits) > 1:
            return None, True
        return None, False

    # ---- broadcasting ------------------------------------------------

    def broadcast_room(
        self,
        room_id: str,
        speaker_name: Optional[str],
        text: str,
        *,
        exclude: Iterable[str] = (),
        dim: bool = True,
        category: Optional[str] = None,
        always: bool = False,
    ) -> None:
        """Send ``text`` to every connected occupant of a room.

        If ``speaker_name`` is given, recipients who have muted the speaker do
        not receive it. ``exclude`` is a set of names to skip. ``dim`` marks
        system/announcement text (vs. speech) for per-recipient ANSI colour.
        ``category`` names an `.ignore` category, so recipients who have chosen
        not to see that kind of line are skipped; ``always=True`` overrides the
        filter for a line that must be seen regardless (see cmd_go's staff
        arrival into a locked room).
        """
        excl = {n.casefold() for n in exclude}
        for r in self.occupants(room_id):
            if r.name.casefold() in excl:
                continue
            if speaker_name is not None and not r.sees(speaker_name):
                continue
            if not always and not r.hears_category(category):
                continue
            if r.session is not None:
                r.session.send(text, dim=dim)

    def broadcast_all(
        self,
        speaker_name: Optional[str],
        text: str,
        *,
        exclude: Iterable[str] = (),
        dim: bool = True,
        category: Optional[str] = None,
        always: bool = False,
    ) -> None:
        """Send to every connected resident anywhere (for .shout)."""
        excl = {n.casefold() for n in exclude}
        for r in self.all_connected():
            if r.name.casefold() in excl:
                continue
            if speaker_name is not None and not r.sees(speaker_name):
                continue
            if not always and not r.hears_category(category):
                continue
            if r.session is not None:
                r.session.send(text, dim=dim)


# ---------------------------------------------------------------------------
# Loading the world from `world/` — one file per room, plus map.txt
#
# The format is deliberately the smallest thing that can be edited by hand in a
# terminal: header lines `key: value`, ONE blank line, then the description
# verbatim to the end of the file (leading spaces and all — a mirror-written
# room's reversed text depends on it). What you edit is what runs.

ROOM_SUFFIX = ".txt"
MAP_FILE = "map.txt"

# The narrow (portrait-phone) variant convention: `<name>.narrow.txt` beside
# `<name>.txt`. SILENTLY OPTIONAL everywhere it is used — absent simply means
# the wide file is served to everyone, so this is safe to add to any existing
# house without touching it. Never a startup error (unlike agreement.txt).
#
# Used by the greeting, the map, and (v1.26.0) any ROOM. The room case differs in
# what "absent" means: the greeting and the map fall back to the wide ART, which
# is all you can do with a drawing, but room prose is prose, so it is REFLOWED to
# the session's width instead. A room file therefore only needs a narrow variant
# when its layout is deliberate — see `_shared.room_description`.
NARROW_SUFFIX = ".narrow.txt"


def narrow_variant(path):
    """``world/map.txt`` -> ``world/map.narrow.txt``. Existence is not checked."""
    from pathlib import Path
    path = Path(path)
    return path.with_name(path.name[: -len(".txt")] + NARROW_SUFFIX) \
        if path.name.endswith(".txt") else path.with_name(path.name + NARROW_SUFFIX)


def read_narrow(path) -> str:
    """The narrow variant of ``path``, or "" if there isn't one."""
    alt = narrow_variant(path)
    return alt.read_text(encoding="utf-8").rstrip("\n") if alt.is_file() else ""
ROOMS_SUBDIR = "rooms"

# Header keys, and how each is read. Anything else is a typo, and a typo that
# silently sealed a room would be worse than a refusal to start.
FLAG_KEYS = ("lockable", "staff_only")
TEXT_KEYS = ("name", "exits", "refusal")

# RETIRED headers (v1.17.0). `entry: yes` / `jail: yes` moved into
# `config.toml [world]` — which room is the front door is a property of the
# deployment, not of the room, and a room file asserting it cannot be reused in
# another house without editing it.
#
# They are accepted and IGNORED with a warning rather than being fatal: an
# operator's world directory is content, and a deploy that refuses to start over
# a line in a room file is a rename that closes the house. The warning is what
# stops the ignoring from being silent.
RETIRED_HEADER_KEYS = ("entry", "jail")

HEADER_KEYS = TEXT_KEYS + FLAG_KEYS + RETIRED_HEADER_KEYS


class WorldError(ValueError):
    """A world that cannot be loaded. The message names the file."""


def _flag(value: str, path: Path, key: str) -> bool:
    v = value.strip().lower()
    if v in ("yes", "true", "on"):
        return True
    if v in ("no", "false", "off"):
        return False
    raise WorldError(f"{path}: {key} must be yes or no (got {value.strip()!r})")


def parse_room_file(path: Path) -> Room:
    """Parse one room file.

    The room id is the filename stem; the description is everything after the
    first blank line, byte for byte, with trailing newlines trimmed. A retired
    `entry:`/`jail:` header is warned about and ignored (see RETIRED_HEADER_KEYS).

    `<id>.narrow.txt` beside it, if present, is the author's own 40-column
    description — **prose only, no header block**, since everything in the header
    is a property of the room and not of the width. Absent is the ordinary case.
    """
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    head: list[str] = []
    body_at = len(lines)
    for i, line in enumerate(lines):
        if not line.strip():
            body_at = i + 1
            break
        head.append(line)
    description = "\n".join(lines[body_at:]).rstrip("\n")

    name = ""
    refusal = ""
    exits: list[str] = []
    flags = {k: False for k in FLAG_KEYS}
    for lineno, line in enumerate(head, start=1):
        if ":" not in line:
            # ⚠ The file and the LINE NUMBER, never the line's text (v1.41.0).
            # Quoting it put an operator-authored line onto `.load_report` and
            # into the process log — not a covenant breach (it is the operator's
            # own content, not a resident's words) but the wrong side of a line
            # this codebase otherwise keeps crisply. A number says where to look
            # just as well.
            raise WorldError(f"{path}: line {lineno} of the header is not "
                             f"'key: value'")
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key not in HEADER_KEYS:
            raise WorldError(
                f"{path}: unknown header key {key!r} "
                f"(known: {', '.join(HEADER_KEYS)})")
        if key == "name":
            name = value.strip()
        elif key == "refusal":
            refusal = value.strip()
        elif key == "exits":
            # Repeated `exits:` lines MERGE, so a long list can be written
            # across several readable lines.
            exits += [e.strip() for e in value.split(",") if e.strip()]
        elif key in RETIRED_HEADER_KEYS:
            # Ignored, but never silently: the operator's room file still claims
            # something the engine no longer reads, and they should know where it
            # went. Facts only — the file, the key, the replacement.
            log.warning("%s: '%s:' is retired and ignored — the %s room is named "
                        "by [world] %s_room in config.toml", path, key, key, key)
        else:
            flags[key] = _flag(value, path, key)

    if not name:
        raise WorldError(f"{path}: no 'name:' in the header")
    # No inert keys: a refusal line only means something on a room a resident can
    # be refused from. On any other room it is a mistake, not a silent no-op.
    if refusal and not flags["staff_only"]:
        raise WorldError(
            f"{path}: 'refusal:' is only for a staff_only room "
            f"(this room is not one)")

    seen: set[str] = set()
    ordered = [e for e in exits if not (e in seen or seen.add(e))]
    return Room(id=path.stem, name=name, description=description,
                description_narrow=read_narrow(path), exits=ordered,
                lockable=flags["lockable"], staff_only=flags["staff_only"],
                refusal=refusal)


def one_way_passages(rooms: dict[str, Room]) -> list[tuple[str, str]]:
    """Every passage declared from one side only, in reading order."""
    return [(rid, dest) for rid in sorted(rooms)
            for dest in rooms[rid].exits
            if rid not in rooms[dest].exits]


def load_world(world_dir: str | Path, entry_room: str = "",
               jail_room: str = "") -> World:
    """Build the world from ``world_dir``: ``map.txt`` + ``rooms/*.txt``.

    ``entry_room`` and ``jail_room`` come from ``config.toml [world]`` (v1.17.0).

    Fatal (the server refuses to start, and the message names the file or the
    key): no room files at all, an unknown exit target, a duplicate room id, a
    missing name, a bad flag value, a header line that is not `key: value`, an
    unknown header key, an unset ``entry_room``, and an ``entry_room`` or
    ``jail_room`` naming a room that does not exist.

    **There is no fallback world.** A house with no rooms, or a front door that
    names nothing, used to boot into a built-in "void" — a server standing in an
    empty room rather than a server refusing to start. That was the wrong trade:
    it turned a config mistake into a house that looked open and was not, and it
    cost a special-cased room id in the engine. A world that cannot be built is
    now a startup error, said plainly, once.

    Not fatal, on purpose: a one-way passage (listed at startup), a sealed room
    (a room nothing lists is exactly that), and a world with no jail.
    """
    world_dir = Path(world_dir)
    rooms_dir = world_dir / ROOMS_SUBDIR
    all_txt = sorted(rooms_dir.glob(f"*{ROOM_SUFFIX}")) if rooms_dir.is_dir() else []
    # `*.narrow.txt` matches `*.txt`, so the narrow variants MUST be filtered out
    # here or each would be parsed as a room of its own — id `foo.narrow`, no
    # `name:` header, and a house that refuses to start. They are content for a
    # room, read by parse_room_file, never rooms themselves.
    files = [p for p in all_txt if not p.name.endswith(NARROW_SUFFIX)]
    if not files:
        raise WorldError(
            f"{rooms_dir}: no room files — a world needs at least one room "
            f"(write rooms/<id>.txt and name it in [world] entry_room)")

    map_file = world_dir / MAP_FILE
    ascii_map = (map_file.read_text(encoding="utf-8").rstrip("\n")
                 if map_file.is_file() else "")
    # map.narrow.txt if the house drew one; "" means "no narrow map", and a
    # narrow session simply gets the wide art, which is the honest fallback.
    ascii_map_narrow = read_narrow(map_file)

    rooms: dict[str, Room] = {}
    by_id: dict[str, Path] = {}
    for path in files:
        room = parse_room_file(path)
        # Ids are the stems, so two files can only collide by capitalization —
        # which on a case-insensitive filesystem would be one room wearing two
        # names. Refuse rather than pick.
        key = room.id.casefold()
        if key in by_id:
            raise WorldError(f"{path}: duplicate room id {room.id!r} "
                             f"(already defined by {by_id[key]})")
        by_id[key] = path
        rooms[room.id] = room

    # A `<id>.narrow.txt` whose `<id>.txt` does not exist is silently doing
    # nothing, which is what a typo'd filename looks like. Not fatal — the whole
    # convention is optional — but named at startup for the same reason one-way
    # passages are: facts only, room ids, never a word of room text.
    orphans = sorted(p.name for p in all_txt
                     if p.name.endswith(NARROW_SUFFIX)
                     and not (rooms_dir /
                              (p.name[: -len(NARROW_SUFFIX)] + ROOM_SUFFIX)).is_file())
    if orphans:
        log.warning("%s: narrow variant with no room file, ignored: %s",
                    rooms_dir, ", ".join(orphans))

    for room in rooms.values():
        for exit_id in room.exits:
            if exit_id not in rooms:
                raise WorldError(f"{rooms_dir / (room.id + ROOM_SUFFIX)}: "
                                 f"exit to unknown room {exit_id!r}")

    entry = (entry_room or "").strip()
    if not entry:
        raise WorldError(
            "[world] entry_room is not set in config.toml — it names the room "
            "arrivals land in, and there is no default")
    if entry not in rooms:
        raise WorldError(
            f"[world] entry_room {entry!r} is not a room in {rooms_dir} "
            f"(known: {', '.join(sorted(rooms))})")

    jail = (jail_room or "").strip() or None
    if jail is not None and jail not in rooms:
        raise WorldError(
            f"[world] jail_room {jail!r} is not a room in {rooms_dir} "
            f"(known: {', '.join(sorted(rooms))})")

    return World(rooms=rooms, ascii_map=ascii_map,
                 start_room=entry, jail_room=jail,
                 one_way=one_way_passages(rooms),
                 ascii_map_narrow=ascii_map_narrow)
