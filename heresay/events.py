# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The engine's operational event log — one line of JSON per fact.

**Why the engine owns this rather than journald.** The facts were already being
written; they were going somewhere the engine could not read back. Surfacing any
of them in-world meant granting the daemon `systemd-journal` — far more access
than one owner-level screen is worth — and a second file that merely *duplicated*
journald would drift the moment someone edited one side. So the engine owns its
operational record: it writes it, trims it, and reads it, and journald keeps what
journald is actually for.

**The split, and it is absolute — no fact lives in both places:**

- ``events.jsonl`` — DOMAIN facts. Who connected, who failed auth, which staff
  did what to whom. Read back by `.staff_actions` today and by whatever views
  come later; every view is a filter over this one store.
- journald / stdout — PROCESS output. The startup banner, the listeners, the
  loaded-world counts, malformed-config warnings, uncaught tracebacks. Things an
  operator reads while the process is misbehaving, which no in-world screen wants.

**Portable by design.** No systemd, no journalctl, no LogNamespace: the engine
runs the same on a VPS, a laptop and a container, and its operational record
comes with it.

⛔ **THE COVENANT RULE, and it is the same one the rest of the codebase lives
under: facts only, NEVER message content.** No speech, emote, think, tell,
shout, mail body, topic, profile, or review line may reach this file — not as a
field, not in an error path, not as a `repr()` of something that happened to be
nearby. Names are allowed (a staff action without its target is not
accountability); words are not. The type table below is CLOSED for exactly this
reason: adding a type is a deliberate act with a covenant question attached, and
a structural test scans every recorded field for content-shaped keys.

**Durability.** Append + flush on every event, and an **fsync on the
accountability-grade types** (``DURABLE_TYPES`` — see the table below) so they
are on disk before the command that caused them returns. The others are cheap
facts, often arriving in bursts; a flush is enough, and fsyncing every refused
connection on a busy door would be a lot of disk for very little.

A torn final line (a crash mid-append, a full disk) is **skipped on read**, never
raised: a log that refuses to be read because its last byte is missing has failed
at the one job it has.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Iterable, Optional

from .atomicwrite import atomic_write

log = logging.getLogger("heresay")

DAY_SECONDS = 86400

# The CLOSED set of event types. Each names its facts; every field is a fact
# about *who/where/what*, never about what was said.
#
#   connection       {dir, ip}                  a socket arrived, resumed, or went away
#   login            {account, ip}              WHO arrived from WHERE (see below)
#   auth_fail        {ip, phase}                a credential was refused
#   login_timeout    {ip}                       the pre-auth clock ran out
#   refused          {ip, reason}               entry denied BEFORE any credential
#   flood_disconnect {ip}                       dropped for sustained flooding
#   takeover         {account, from_ip}         a live session was displaced
#   account_security {event, +account OR ip}    a credential or the terms changed
#   account_deleted  {name}                     a resident deleted their own account
#   staff_call       {account}                  a resident called for staff
#   staff_mail       {account}                  a resident wrote to the staff box
#   report_filed     {}                         a report was filed — NOTHING else
#   staff_action    {actor, action, target}    a staff power was used
#   auth_ceiling     {phase, +threshold/window/cooldown on "tripped"}
#                                               the server-wide door posture changed
#
# Adding one is deliberate: it belongs in this table, in the docs, and in the
# covenant tests' understanding of what a fact looks like.
#
# `staff_mail` (v1.35.0) is the twelfth, and it is `staff_call`'s other half.
# The two are the same fact — a resident reached for staff — arriving by the two
# doors the house offers, and `.staff_calls` shows them side by side because
# "who has been contacting us?" is one question. It is recorded on DELIVERY, not
# on the attempt: a letterbox that was full took no note, and a tally that
# counted it would say a contact was made that was not. Facts only, as ever —
# the account, never the note.
#
# ⛔ `report_filed` (v1.57.0) IS THE EMPTIEST TYPE IN THE TABLE, AND THAT IS THE
# WHOLE OF ITS DESIGN. `{ts, type}` and NOTHING else: no reporter, no subject,
# no text, not a field that could hold one.
#
# A report has exactly three things in it — who filed, who it is about, and the
# words — and v1.50.0 recorded NONE of them precisely because the middle one is
# what a log must not casually accumulate: an unproven accusation in the clear
# is the fact this house does not keep. That reasoning is untouched. What this
# type carries is the ONE remaining fact, which is not about any person at all:
# *something is waiting in the report box.*
#
# ⚡ It exists so an EXTERNAL watcher can nudge the reader (see
# `deploy/install/heresay-report-watch.sh`). The alternative was teaching the
# daemon to reach the network — an outbound dependency in a process whose whole
# security posture is that it has none — so the engine writes a fact and
# something outside it decides whether that fact is worth a buzz. Exactly the
# shape of `auth_ceiling` and its watcher, for exactly the same reason.
#
# ⚠ On DELIVERY, never on the attempt — `staff_mail`'s rule, and the same
# argument: a full report box stores nothing, and a nudge for a report that was
# never filed is a nudge at nothing.
#
# ⚠ NOT in DURABLE_TYPES, deliberately. The record that matters is the report
# itself, and `MailStore.deliver()` fsyncs its box before it returns — so the
# accusation is on the disk either way. This event is a doorbell; losing one to
# a power cut costs a buzz, and there is nothing in it anybody could later need
# to prove.
EVENT_TYPES = (
    "connection",
    "login",
    "auth_fail",
    "login_timeout",
    "refused",
    "flood_disconnect",
    "takeover",
    "account_security",
    "account_deleted",
    "staff_call",
    "staff_mail",
    "report_filed",
    "staff_action",
    "bad_frame",
    "auth_ceiling",
)

# `auth_ceiling.phase` — the two edges of the server-wide failed-auth ceiling
# (v1.53.0). ONE type with a phase, not two types, for the same reason
# `refused` is one type with a reason: "the door's posture changed" is one
# question, and which way it changed is the answer rather than a different
# question. `auth_fail` already carries a `phase`, so the field name is the one
# a reader has met before.
#
# ⛔ THE COVENANT QUESTION, answered before the type was added: it carries NO IP
# AND NO NAME, and cannot. The ceiling is SERVER-WIDE — it counts failures
# across every source precisely because no single peer is responsible — so there
# is no address this event is *about*, and attaching the one that happened to
# tip it over would name somebody the fact does not concern. What it carries
# instead is the three numbers that explain why it fired, so the record is
# legible years later without knowing what the config said that day.
#
# ⚠ EDGE-TRIGGERED, and that is the whole of its volume control:
# `note_failure()`/`note_cleared()` return True exactly once per transition and
# the records live inside those guards, so a sustained run writes one of these
# and thousands of `auth_fail`s — not one per attempt.
AUTH_CEILING_PHASES = ("tripped", "cleared")

# `bad_frame.reason` — why a frame was judged hostile and the door shut.
#
#   control_byte     a split line inside a frame began with the \x01 control
#                    prefix: an attempt at the out-of-band channel
#   too_many_lines   one frame carrying more lines than MAX_FRAME_LINES
#   too_large        kilobytes buffered on a socket door producing nothing
#                    readable (the `_raw` cap)
BAD_FRAME_REASONS = ("control_byte", "too_many_lines", "too_large")

# ⛔⛔ `bad_frame` IS THE ONE TYPE THAT CARRIES A FRAGMENT OF WHAT WAS SENT, AND
# IT IS A DELIBERATE, NARROW EXCEPTION TO "EVENTS ARE FACTS ONLY".
#
# The maintainer, 2026-08-04: *"I don't give a damn about the covenant if someone is trying
# to hack the system, that's for legitimate users."* The covenant is a promise to
# people using the house, and it is kept in full for them. It is not a promise to
# somebody probing the control channel.
#
# ⚠ WHAT KEEPS IT NARROW, and this is the part that must not erode: the event
# fires ONLY on a frame the engine has ALREADY judged malformed and is closing
# the connection over. There is no path from anything a resident legitimately
# said to a `sample` field — not a long message, not an odd character, not a
# flood. A test speaks a canary through every content verb and asserts no
# `bad_frame` is written at all. If that test ever needs relaxing, the exception
# has stopped being narrow and this reasoning no longer covers it.
#
# ⚠ THE SAMPLE IS `repr()`-ESCAPED AND CAPPED, BY THE STORE rather than by the
# caller (see `record`). Raw attacker bytes written into a file somebody later
# `cat`s, greps or displays in a view is how a payload gets its second shot —
# the same reason the room-header error stopped quoting operator text. Escaped,
# it is inert and still perfectly readable when diagnosing.
SAMPLE_MAX = 200

# ---- the documented sub-enums --------------------------------------------
#
# Two types carry a `reason`/`event`/`dir` word rather than splitting into a
# family of near-identical types. The words are listed here so they are as
# closed as the type set itself: a reader can enumerate what it might meet, and
# a typo is a value nobody filters on.

# `connection.dir` — how a session began or ended.
#
# "resume" is a login by remembered token (v1.24.0's silent reconnect). It is
# deliberately a DIRECTION rather than a type of its own: a resume is a kind of
# arrival, and a view asking "who has been connecting?" wants it in the same
# list. It keeps its own word because the distinction — password at a prompt vs.
# a 30-day token — is exactly what an operator reading the log would want.
CONNECTION_DIRS = ("open", "close", "resume")

# `refused.reason` — why entry was denied BEFORE any credential was offered.
# One type rather than six, because "who was turned away, and why" is one
# question; the reason is the answer, not a different question.
#
#   ip_banned      the source IP is banned          (server._handle)
#   conn_cap       per-IP concurrency / churn cap   (server._handle)
#   banned_name    the ACCOUNT name is banned       (post-auth, _enter)
#   reserved_name  tried to register a name the house reserves — a rank on the
#                  ladder, or one the operator's blocklist names
#   owner_name     tried to register the OWNER account's name from a network
#                  door (it may only be claimed over loopback)
#   rate_limited   per-IP registration flood limit
REFUSAL_REASONS = ("ip_banned", "conn_cap", "banned_name", "reserved_name",
                   "owner_name", "rate_limited")

# `account_security.event` — something happened TO an account that was not
# ordinary use of it.
#
#   password_reset_recovery     a recovery code was burned to set a new password
#   recovery_codes_regenerated  a fresh code set was issued (the old one voided)
#   reconsent                   agreed to a revised agreement at login
#
# ⛔ **The first two carry `account`; `reconsent` carries `ip` and NO NAME.**
# That asymmetry is the point of the type, not an inconsistency in it:
#
#   - A password reset or a fresh code set is a CREDENTIAL changing hands. A
#     record of that without the account it belongs to is no record at all — it
#     is exactly what someone robbed of their account needs to find.
#   - Re-consenting is not a credential event and needs no name to be useful.
#     What the fact is FOR is "the revision is landing" — a count over time.
#     `accounts.json` holds each account's `agreed`/`agreed_hash`, which is the
#     CURRENT state of one person's consent; a log would accumulate a record of
#     who was at the door and when. Those are different things to hold about a
#     person, and the smaller one is enough. (v1.30.1 named it and v1.30.2 took
#     the name back out, before either shipped.)
#
# The name says "security" and consent is not quite that — a slight stretch,
# taken deliberately rather than minting a near-empty type for one event.
ACCOUNT_SECURITY_EVENTS = ("password_reset_recovery", "recovery_codes_regenerated",
                           "reconsent")

# The `account_security` events that name an account. `reconsent` is absent on
# purpose (see above) — named here so the rule is data a test can read rather
# than a paragraph someone has to remember.
ACCOUNT_NAMED_SECURITY_EVENTS = ("password_reset_recovery",
                                 "recovery_codes_regenerated")

# ---- `login`, the one event that ties an account to an address ------------
#
# Every other type holds one or the other: connections carry an ip and no name,
# staff actions carry names and no ip. `login` deliberately holds BOTH, because
# `.ban_ip` has to answer "what address is this person on?" without the engine
# keeping a durable `last_ip` on the account — which would be a permanent
# account↔address link, and the covenant's answer to permanent is no.
#
# ⚠ What bounds it is the ORDINARY retention window (`event_retention_days`),
# the same one every other event fades on. So the tie exists for as long as the
# operational record does and not one day longer: ban someone who has not been
# seen in the window and the engine says so rather than reaching for a stored
# address. Clause 3 of the agreement discloses exactly this, in advance.
#
# It is read by exactly two things — resolving a ban target's address, and
# counting the collateral of an IP ban — and never as a browsable "where does
# X connect from" lookup. That restraint is in the CALLERS, not here; there is
# no query surface to abuse because none was built.

# The types worth an fsync: losing one to a power cut would lose the record of
# something a person may later need to prove.
#
#   staff_action     a moderation decision — the store's original purpose
#   account_security  a credential changed hands; the one record of that
#   takeover          a session was displaced, which is what a stolen token
#                     looks like from the outside
#
# The others are deliberately NOT here: a `refused` from a conn cap or a
# registration limiter can arrive in bursts, and fsyncing each would be a great
# deal of disk for a fact whose value is statistical rather than individual.
# `login` is in that group too — it fires on EVERY successful auth, including
# every silent token resume, so it is the most routine event the store has. It
# is a lookup aid with a ten-day life, not something anyone must later prove,
# and losing the last one to a power cut costs a `.ban_ip` its shortcut and
# nothing else. Every event is still flushed, so only a machine-level crash can
# lose one.
DURABLE_TYPES = ("staff_action", "account_security", "takeover")


def escape_sample(text) -> str:
    """A short, INERT excerpt of something hostile.

    `repr()` first, then cap — that order matters: capping raw bytes could cut a
    multi-byte character in half, and `repr()` of a truncated escape is uglier
    than a truncated `repr()`. Capping the ESCAPED form also means the cap is a
    bound on what actually lands in the file, which is the thing being bounded.
    """
    out = repr(text if isinstance(text, str) else str(text))
    if len(out) > SAMPLE_MAX:
        out = out[:SAMPLE_MAX] + "…"
    return out


class EventStore:
    """Append-only JSONL, trimmed to a rolling window.

    Trimming is in-process and happens on append and at startup — the volume is
    low enough (a handful of lines per connection) that rewriting the file when
    something has actually aged out costs nothing, and it means the window is
    honoured without a timer, a cron, or a logrotate config an operator has to
    remember to install.

    A wall clock is injected so the retention tests are deterministic, exactly as
    the ban and mail stores do it.
    """

    def __init__(self, path, retention_days: float = 10.0, wall_clock=time.time,
                 enabled: bool = True):
        self.path = Path(path)
        self.retention_days = float(retention_days)
        self._wall = wall_clock
        # A store can be switched off entirely (unit tests that build a bare
        # server and never want a file). Off means every write is a no-op and
        # every read is empty — never an error, so no caller needs to check.
        self.enabled = bool(enabled)
        if self.enabled:
            self._ensure_file()
            self.trim()          # the startup pass

    # ---- writing -----------------------------------------------------

    def _ensure_file(self) -> None:
        """Create it 0600 if absent, and tighten it if it somehow is not.

        The file names IPs and account names, so it gets the same permissions as
        the account store: owner only, from the moment it exists rather than
        after the first write.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        else:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass                       # a read-only mount; not worth failing over

    def _needs_newline(self) -> bool:
        """True if the file ends mid-line — i.e. the previous append was torn.

        Reads one byte, not the file: this runs on every event.
        """
        try:
            size = self.path.stat().st_size
            if size == 0:
                return False
            with self.path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                return fh.read(1) != b"\n"
        except OSError:
            return False

    def record(self, type: str, **facts) -> None:
        """Write one event. Never raises — a failed write must not take a session
        down with it, and the thing we would want to report is the thing that is
        not working.

        ``type`` must be in `EVENT_TYPES`; an unknown one is dropped with a
        warning rather than written, so a typo cannot quietly invent a category
        that no view will ever show and no covenant test knows about.
        """
        if not self.enabled:
            return
        if type not in EVENT_TYPES:
            log.warning("event: unknown type %r dropped", type)   # the name only
            return
        entry = {"ts": float(self._wall()), "type": type}
        entry.update(facts)
        # ⛔ The escape is applied HERE, so no caller can write a raw sample —
        # a guarantee by construction rather than by every future call site
        # remembering. `repr()` renders control bytes, newlines and escapes as
        # printable text, which is what makes the record safe to `cat` and safe
        # to put on a screen.
        if "sample" in entry:
            entry["sample"] = escape_sample(entry["sample"])
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                # If the last write was torn (a crash, a full disk), the file does
                # not end in a newline — and appending straight onto it would fuse
                # the fragment to THIS event, losing a good record to an old
                # accident. Start a fresh line instead: the torn fragment stays a
                # torn fragment, which read() already knows how to skip.
                if self._needs_newline():
                    fh.write("\n")
                fh.write(line + "\n")
                fh.flush()
                if type in DURABLE_TYPES:
                    os.fsync(fh.fileno())
        except OSError:
            # No content is in scope here, so there is nothing to leak by saying
            # so — but say it once, not per event, or a full disk becomes a
            # second full disk.
            log.warning("event: could not append to %s", self.path)
            return
        self.trim()

    # ---- reading -----------------------------------------------------

    def read(self, types: Optional[Iterable[str]] = None,
             newest_first: bool = False, limit: Optional[int] = None) -> list[dict]:
        """The shared reader every view is built on.

        `.staff_actions` is nothing but ``read(types=("staff_action",),
        newest_first=True)``; a connections view or an admin panel would be the
        same call with a different filter. Keeping one reader is what stops the
        second view from inventing its own parsing, its own torn-line handling and
        its own idea of what "recent" means.

        A **torn final line** — a crash or a full disk mid-append — is skipped,
        not raised. So is any line that is not an object: the file is read as a
        best-effort record, because a record that will not open is no record.
        """
        if not self.enabled or not self.path.exists():
            return []
        want = set(types) if types is not None else None
        out: list[dict] = []
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log.warning("event: could not read %s", self.path)
            return []
        lines = raw.split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                # Tolerated only where a torn write can actually be: the end.
                # A broken line in the MIDDLE is corruption of a different kind
                # and is worth one warning, still not an exception.
                if i < len(lines) - 1:
                    log.warning("event: skipped an unparseable line in %s", self.path)
                continue
            if not isinstance(entry, dict) or "ts" not in entry:
                continue
            if want is not None and entry.get("type") not in want:
                continue
            out.append((entry.get("ts", 0.0), i, entry))
        # File position breaks a timestamp tie. Two actions inside the same clock
        # tick are ordinary (a script, a fast pair of commands, a coarse clock),
        # and "whichever the sort happened to leave first" is not an answer a
        # moderation log may give: what was appended later happened later.
        out.sort(key=lambda t: (t[0], t[1]), reverse=newest_first)
        rows = [e for _ts, _i, e in out]
        return rows[:limit] if limit else rows

    # ---- retention ---------------------------------------------------

    def trim(self) -> int:
        """Drop entries older than the window. Returns how many went.

        ONE policy for every type, deliberately: a per-type retention table is a
        thing an operator has to reason about, and the honest answer to "how long
        do we keep operational facts?" is a single number.

        Rewrites only when something actually aged out, so the common case is a
        read and no write at all. Zero or a negative window means keep
        everything — an operator who wants the log to stop trimming should be able
        to say so without editing code.
        """
        if not self.enabled or self.retention_days <= 0 or not self.path.exists():
            return 0
        cutoff = float(self._wall()) - self.retention_days * DAY_SECONDS
        kept: list[str] = []
        dropped = 0
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return 0
        lines = raw.split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                # A torn LAST line is kept as-is: it is what a half-finished
                # append looks like, and dropping it here would make trim() a
                # quiet data-loss path. Read tolerates it; that is enough.
                if i == len(lines) - 1:
                    kept.append(line)
                else:
                    dropped += 1
                continue
            if float(entry.get("ts", 0.0)) < cutoff:
                dropped += 1
            else:
                kept.append(line)
        if not dropped:
            return 0
        # Was an un-fsynced `write_text` + chmod + replace; now the shared helper
        # (CODE_AUDIT.md #1), which ADDS an fsync. Safe: this rewrite runs only
        # when lines actually age out — not per append — so the fsync is rare.
        # The OSError tolerance stays: a trim that cannot write must not crash the
        # append that triggered it (the helper raises; we warn and move on).
        try:
            atomic_write(self.path, "".join(l + "\n" for l in kept))
        except OSError:
            log.warning("event: could not trim %s", self.path)
            return 0
        return dropped

    # ---- the two `login` lookups, and ONLY these two -------------------
    #
    # ⚠ The whole read surface over `login` is these two methods, and that is
    # deliberate. Both answer a question `.ban_ip` must ask to act at all:
    # "what address is this person on?" and "who else would this ban catch?".
    # Neither is, and neither may become, a browsable "where does X connect
    # from" — there is no method for that because none was written, which is a
    # stronger guarantee than a rule saying nobody should call one.

    def last_login_ip(self, account: str) -> Optional[str]:
        """The address this account last authenticated from, or None.

        None means "not seen inside the retention window" — the honest answer,
        and the reason there is no durable `last_ip` on the account. A ban
        against someone who has not been here in ten days simply cannot reach an
        address, and the caller says so rather than guessing.
        """
        key = account.casefold()
        for e in self.read(types=("login",), newest_first=True):
            if str(e.get("account", "")).casefold() == key:
                ip = e.get("ip")
                return str(ip) if ip else None
        return None

    def accounts_on_ip(self, ip: str) -> list[str]:
        """Every account seen authenticating from ``ip`` in the window.

        For the collateral count an IP ban shows before it acts: banning an
        address can catch a household, a shared flat, a campus or a carrier's
        NAT, and the operator should see the size of that before saying yes.
        Display names, de-duplicated, in first-seen order so the list is stable
        between two calls a second apart.
        """
        seen: dict[str, str] = {}
        for e in self.read(types=("login",)):
            if str(e.get("ip", "")) == ip:
                name = str(e.get("account", ""))
                seen.setdefault(name.casefold(), name)
        return list(seen.values())

    def count(self) -> int:
        """How many events are held. For tests and for a future status line."""
        return len(self.read())
