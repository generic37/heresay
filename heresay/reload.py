# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Live reload — one mechanism, one source per surface.

**What this is for.** The house should be configurable while it is running: the
operator edits a file over SSH and the change takes effect, without a restart and
without anybody being disconnected. Where a command already does that
(`.ownerset`, `.ban`, `.jail`, `.newusers`) nothing here is needed. This is
for the surfaces that have no command and never should: permissions, config
values, and — in a later run — content.

⛔ **No new admin commands.** The WATCHER is the trigger and `.load_report` is the
readout. An operator who edits a file has already said what they want; making
them then type a command to mean it again is ceremony.

**The contract, one source at a time, and the order is the whole design:**

    validate → guard → apply → diff

- **validate** parses the changed file **fully into new objects**, touching
  nothing live. Any error rejects **that source's whole pass**: the old values
  stay, the house stays up, and the failure is reported. This is the runtime
  equivalent of the boot-time fail-fast — at boot a broken file must stop the
  server, at runtime it must not, because **a live house must never be closed by
  a typo.** It is also what makes a half-written file safe: a read that catches a
  save mid-flight fails to parse, is rejected, and the next tick picks up the
  finished file.
- **guard** is for cross-checks that can only be made against the LIVE house —
  an occupied room, a rank word colliding with an account. Neither of the two
  sources here needs one; validation is the whole of their checking. The step
  exists because content (Run 2) cannot do without it.
- **apply** swaps **atomically**: the new object is built in full and assigned in
  one statement, so nothing ever sees a half-applied table. Where sessions hold
  a REFERENCE — the command table, the `Config` — the object is mutated in place
  rather than replaced, which is what makes the change reach connections that are
  already open.
- **diff** reports what CHANGED, not that something did. "ok" is not a report.

**Why so little machinery is needed.** The engine reads most of its
configuration at use time, off objects every session shares: one `Config`
(`server.py`), one `StateStore`, one `World`. The gate asks
`state.command_rank(...)` on every dispatch. So for most values there is nothing
to invalidate and no session to refresh — replacing the contents of a dict IS
the reload. What needs work is the short, explicit list of values that get
COPIED out of `Config` into another object at boot; see `CONFIG_REAPPLY`.

⚠ **The watcher does not watch `state.json`.** The daemon rewrites that file
itself, so watching it would be a feedback loop. It is also why the command
table moved out of it (see permissions.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path

log = logging.getLogger("heresay")


class ReloadResult:
    """What one source's pass did. Rendered by `.load_report`."""

    def __init__(self, source: str, ok: bool = True, changes=(), notes=(),
                 errors=(), deferred: bool = False):
        self.source = source
        self.ok = bool(ok)
        self.changes = list(changes)     # what actually moved
        self.notes = list(notes)         # true, and worth saying, but not a change
        self.errors = list(errors)       # why it was rejected
        # Something was held back for the live house's sake — today only a
        # structural change to an occupied room. The reloader retries a source
        # that says this on every later tick, until it stops saying it.
        self.deferred = bool(deferred)

    @property
    def applied(self) -> bool:
        return self.ok and bool(self.changes)

    def summary(self) -> str:
        if not self.ok:
            return f"{self.source}: REJECTED — {'; '.join(self.errors)}"
        # ⚠ A pass that applied nothing because it was WAITING or REFUSED is not
        # "no change", and saying so was a real reporting bug (v1.41.0): every
        # reboot-gated refusal — an edited agreement, a quoted config value, a
        # `[ranks]` change — is exactly this shape, so the one-line log said
        # "no change" about the very thing it had just declined to do, and the
        # reason survived only on `.load_report`. Notes ARE the report when
        # there is nothing else to report.
        if not self.changes:
            if self.deferred:
                return f"{self.source}: nothing applied yet — {'; '.join(self.notes)}"
            if self.notes:
                return f"{self.source}: not applied — {'; '.join(self.notes)}"
            return f"{self.source}: no change"
        out = f"{self.source}: {', '.join(self.changes)}"
        if self.deferred:
            out += f" ({'; '.join(self.notes)})"
        return out


class ReloadReport:
    """The last pass, held in memory beside the boot load. Never written down."""

    def __init__(self, results, when: float):
        self.results = list(results)
        self.when = float(when)

    def stamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.when))

    @property
    def rejected(self) -> bool:
        return any(not r.ok for r in self.results)


class Watcher:
    """mtime polling over each source's whole FILE SET, one digest per source.

    Deliberately mtime and not inotify: three dependencies is the whole stack
    (`argon2-cffi`, `websockets`, `cryptography`) and a handful of `stat()`s
    every few seconds is not a cost worth a fourth. It also behaves the same on
    every platform the engine runs on, which inotify does not.

    ⚠ **A DIGEST OVER THE SET, not a stamp per file** (v1.39.0). Content sources
    watch directories — `world/rooms/*.txt`, `help/*.txt` — where the change an
    operator makes is as often "a file appeared" or "a file was deleted" as "a
    file was edited. Comparing per-file stamps sees an edit and misses the other
    two; a digest over (name, mtime, size) for the whole set sees all three, and
    reduces the question to one comparison per source.
    """

    def __init__(self, clock=time.time):
        self._seen: dict[str, str] = {}
        self._clock = clock

    def _stamp(self, path) -> str:
        try:
            st = Path(path).stat()
            # Size as well as mtime: a same-second edit that keeps the mtime
            # coarse-equal still moves the size in nearly every real case, and
            # the pair costs one stat either way.
            return f"{st.st_mtime}:{st.st_size}"
        except OSError:
            return "-"

    def digest(self, paths) -> str:
        """One string for the whole set: sorted, so the order paths are listed
        in cannot make an unchanged house look changed."""
        return "|".join(f"{Path(p)}={self._stamp(p)}" for p in sorted(map(str, paths)))

    def prime(self, key: str, paths) -> None:
        """Record a source's current state without reporting it as changed."""
        self._seen[key] = self.digest(paths)

    def changed(self, key: str, paths) -> bool:
        now = self.digest(paths)
        if key not in self._seen:
            self._seen[key] = now
            return False
        if now != self._seen[key]:
            self._seen[key] = now
            return True
        return False


# ---------------------------------------------------------------------------
# Source 1 — permissions
# ---------------------------------------------------------------------------

def reload_permissions(server) -> ReloadResult:
    """Re-read `permissions.toml` and swap the live command table.

    **validate** — parse the file in full into a NEW dict. A bad value rejects
    the pass; the running table is untouched.

    **apply** — the gate calls `state.command_rank()` on every dispatch and every
    session shares the one `StateStore`, so replacing the contents of that dict
    is the entire live path. Sessions already connected see it on their next
    typed command, with nothing to refresh.
    """
    from . import permissions as perm
    from .commands import command_seed

    path = server.config.permissions_path()
    try:
        # Same call the boot makes, including the legacy fallback: if the file
        # is deleted out from under a running house the table should land where
        # a restart would put it, not somewhere only a reload can reach.
        parsed = perm.load(path, command_seed(),
                           legacy=server.state.legacy_commands,
                           ladder=server.theme)
    except perm.PermissionsError as exc:
        return ReloadResult("permissions", ok=False, errors=[str(exc)])

    old = dict(server.state.commands)
    new = parsed.table
    changes = []
    for name in sorted(set(old) | set(new)):
        was, now = old.get(name), new.get(name)
        if was == now:
            continue
        if was is None:
            changes.append(f".{name} → {now}")
        elif now is None:
            changes.append(f".{name} dropped (was {was})")
        else:
            changes.append(f".{name} {was} → {now}")

    notes = []
    if parsed.added and parsed.from_file:
        notes.append(f"{len(parsed.added)} command(s) not in the file are using "
                     f"their built-in defaults (run --write-permissions to add "
                     f"them)")
    if not parsed.from_file:
        notes.append("no readable permissions.toml — using the built-in defaults")
    if parsed.skipped:
        notes.append(f"{len(parsed.skipped)} line(s) name no such command, "
                     f"ignored: {', '.join('.' + s for s in parsed.skipped)}")

    # Atomic: one assignment of a fully built dict.
    server.state.set_command_table(new)
    return ReloadResult("permissions", changes=changes, notes=notes)


# ---------------------------------------------------------------------------
# Source 1a — forbidden_names.txt (v1.44.0)
# ---------------------------------------------------------------------------

def reload_forbidden(server) -> ReloadResult:
    """Re-read the operator's blocklist and swap the live set.

    **validate** — there is nothing to get wrong. Every non-comment line is a
    name, so this source cannot reject a pass; a missing or unreadable file is
    the empty set. It is on the spine anyway because the spine is how a watched
    file is applied and REPORTED, and "no news" is worth the same three channels
    as any other edit.

    **apply** — one assignment. `_create_account` reads `server.forbidden_names`
    at use time, so nothing has to be refreshed and the very next registration
    attempt sees the new list.

    **diff** — how many were added and removed, plus the grandfather count. ⛔
    COUNTS, never the names: this file is a list of words an operator would
    rather not see, and a reload report that recites them back onto a screen (or
    into `.load_report`, which staff can read) would be the one place in the
    house that says them out loud.
    """
    from . import forbidden as fb

    old = set(getattr(server, "forbidden_names", ()) or ())
    new = fb.load(server.config.forbidden_names_path())

    added, removed = new - old, old - new
    changes = []
    if added:
        changes.append(f"{len(added)} name(s) added")
    if removed:
        changes.append(f"{len(removed)} name(s) removed")

    server.forbidden_names = new                    # atomic: one assignment

    notes = []
    held = server._report_forbidden(new)
    if held:
        notes.append(f"{held} existing account(s) hold a forbidden name "
                     f"(grandfathered; nothing was changed)")
    if changes and not new:
        notes.append("the list is now empty")
    return ReloadResult("forbidden_names", changes=changes, notes=notes)


# ---------------------------------------------------------------------------
# Source 2 — config values
# ---------------------------------------------------------------------------
#
# ⚠ THE FOUR CLASSES, and every world-facing value is in exactly one of them.
# The lists are EXPLICIT and must stay explicit: a classification that is
# computed would drift the first time somebody added a field, and the whole
# point of naming them is that the reader can check the list against the code.

# INFRA — seen, never applied. Rebinding a socket or re-pointing the accounts
# file under a running house is a different (and far riskier) feature than
# configuring a world; `config.toml` + a restart is the right answer and is not
# a gap. A change here is REPORTED so nobody wonders why nothing happened.
CONFIG_INFRA = frozenset({
    "tls_host", "tls_port", "plain_host", "plain_port", "ws_host", "ws_port",
    "trusted_proxy", "cert_file", "key_file",
    "accounts_file", "mail_file", "mail_key_file", "revtell_file", "bans_file",
    "resume_file", "state_file", "events_file", "ban_history_file",
    "permissions_file",
    # ⚠ Bound at boot like every other path (v1.52.4, audit W1). It was missing
    # here, so a live edit moved the field while `ControlServer` went on
    # listening at the old path — and `--shutdown`/`--announce` load the config
    # FRESH, so the CLI would have knocked at the new one. A countdown that
    # reaches nobody is worse than one that is refused.
    "control_socket_file",
    # Which FILE is the blocklist, as opposed to what is IN it (that is live,
    # through its own source). Infra for `permissions_file`'s reasons exactly —
    # the same shape of setting, beside it in the same directory, and it was the
    # one path field classified differently.
    "forbidden_names_file",
    "house_dir", "agreement_file", "greeting_file", "reboot_file", "theme_file",
    "help_dir", "world_dir", "entry_room", "jail_room",
    # ⛔ WHO OWNS THE HOUSE IS NOT A LIVE EDIT (v1.52.4, the maintainer's call). It falls
    # into two traps at once: it is COPIED into the AccountStore at boot (so the
    # store and the field would disagree), and it is read at USE time in a dozen
    # places (so the copy that did move would re-point the top rank inside one
    # poll, unvalidated — the boot's "does the arch account exist?" check has
    # already run). Restart-gated, so the two move together and that check runs
    # again on the way back up.
    "owner_name",
    # Which LEVEL a new account lands in — copied into the AccountStore at boot
    # like `owner_name`, and account-creation policy rather than a live display
    # value, so it moves at a restart with the owner name it sits beside.
    "landing_level",
    # ⛔ SELF-REFERENTIAL (v1.52.4, audit A2). `_reload_task` closes over the
    # interval and the `if reload_poll_seconds > 0` on/off gate is evaluated
    # once, at boot, so a live change can neither retune the loop nor stop it.
    # The watcher reporting a change to its own clock as applied was the reload
    # lying about itself.
    "reload_poll_seconds",
    "base_dir",
})

# PER-SESSION — read in `Session.__init__`, so a change reaches the NEXT
# connection and not the ones already open. Deliberately left alone: re-tuning
# a live session's speech bucket mid-sentence is not desirable even if it were
# easy. Reported as such.
CONFIG_PER_SESSION = frozenset({
    "wrap_columns",
    "speech_burst", "speech_refill_per_sec", "speech_mute_strikes",
    "speech_disconnect_strikes", "speech_mute_seconds", "shout_per_seconds",
    "command_burst", "command_refill_per_sec",
    "mail_send_burst", "mail_send_refill_per_sec",
    "invite_burst", "invite_refill_per_sec",
    "reboot_rate_seconds", "staff_call_seconds",
    # ⚠ The same class by a different route (v1.52.4): these are read when a
    # socket is ACCEPTED, not in `Session.__init__`, but the honest report is
    # identical — the next connection gets them and a socket already open keeps
    # the options the kernel was given. Reported as "new connections only"
    # rather than as a change that landed everywhere.
    "tcp_keepidle", "tcp_keepintvl", "tcp_keepcnt",
})


def _reapply_report_retention(server, value) -> None:
    """Re-copy `report_retention_days` into the live MailStore's per-box table.

    ⚠ The report box ages on its OWN number (v1.50.0), copied into
    `MailStore._box_purge_days` at boot — so moving the Config field alone left
    reports ageing on the boot value while the reload reported the change as
    applied. A hook rather than a lambda because it needs the box key, and the
    command package cannot be imported at module scope from here.

    It genuinely applies: `purge()` asks `purge_days_for(box)` on every pass, so
    the next purge uses the new number and nothing has to be rebuilt.
    """
    from .commands import REPORT_BOX
    server.mail._box_purge_days[REPORT_BOX.casefold()] = int(value)


def _reapply_report_box_cap(server, value) -> None:
    """Re-copy `report_box_cap` into the live MailStore's per-box cap table.

    ⚠ The same shape as the retention hook one field along, and boot-copied for
    the same reason: `MailStore._box_caps` is built once in `__init__`, so
    moving the Config field alone would leave `deliver()` asking the boot value
    while the reload reported the change as applied. `cap_for()` is asked on
    every delivery, so the next one uses the new number.
    """
    from .commands import REPORT_BOX
    server.mail._box_caps[REPORT_BOX.casefold()] = int(value)


# BOOT-COPIED — the value is copied OUT of Config into another object at boot,
# so mutating Config alone would change nothing. Each needs its copy re-applied.
# `(field, apply)` where apply(server, value) does exactly that, and nothing else.
def _reapply_instance_name(server, value) -> None:
    """Re-assign the house's name and PUSH it to every connected browser.

    ⚠ Boot-copied: the server assigns `theme.instance_name` from the config
    once in `_build`, so moving the Config field alone would rename the house
    in the file and nowhere else. And the tab's NAME is the browser's to hold —
    it cannot discover a change — so the identity frames go out too, exactly as
    a live `theme.toml` edit already does.
    """
    _adopt_theme(server, dataclasses.replace(server.theme,
                                             instance_name=str(value)))


CONFIG_REAPPLY = (
    ("instance_name", _reapply_instance_name),
    ("report_retention_days", _reapply_report_retention),
    ("report_box_cap", _reapply_report_box_cap),
    ("rev_window_minutes",
     lambda s, v: setattr(s.world, "review_window", v * 60.0)),
    ("rev_max_lines",
     lambda s, v: setattr(s.world, "review_max", v)),
    ("echo_min_minutes",
     lambda s, v: setattr(s.world, "echo_min", v * 60.0)),
    ("echo_max_minutes",
     lambda s, v: setattr(s.world, "echo_max", v * 60.0)),
    ("event_retention_days",
     lambda s, v: setattr(s.events, "retention_days", float(v))),
    ("ban_history_days",
     lambda s, v: setattr(s.ban_history, "retention_days", float(v))),
    ("revtell_count",
     lambda s, v: setattr(s.revtells, "cap", v)),
    ("mailbox_cap",
     lambda s, v: setattr(s.mail, "cap", v)),
    ("mail_purge_days",
     lambda s, v: (setattr(s.mail, "purge_days", v),
                   setattr(s.revtells, "purge_days", v))),
    ("max_conns_per_ip",
     lambda s, v: setattr(s.churn, "max_concurrent", v)),
    ("churn_window_seconds",
     lambda s, v: setattr(s.churn, "window", v)),
    ("churn_max_in_window",
     lambda s, v: setattr(s.churn, "max_in_window", v)),
    ("auth_backoff_base",
     lambda s, v: setattr(s.auth_backoff, "base", v)),
    ("auth_backoff_cap",
     lambda s, v: setattr(s.auth_backoff, "cap", v)),
    ("registration_burst",
     lambda s, v: setattr(s.reg_limiter, "burst", float(v))),
    ("registration_refill_per_sec",
     lambda s, v: setattr(s.reg_limiter, "refill", float(v))),
    ("global_auth_fail_window_seconds",
     lambda s, v: setattr(s.global_ceiling, "window", v)),
    ("global_auth_fail_threshold",
     lambda s, v: setattr(s.global_ceiling, "threshold", v)),
    ("global_auth_cooldown_seconds",
     lambda s, v: setattr(s.global_ceiling, "cooldown", v)),
    ("global_auth_enabled",
     lambda s, v: setattr(s.global_ceiling, "enabled", bool(v))),
)

CONFIG_REAPPLY_FIELDS = frozenset(f for f, _ in CONFIG_REAPPLY)

# ⛔ REBOOT-GATED: the three values `agreement.txt` QUOTES. Not applied live —
# not the value, not the agreement — and this is the fifth config class, added in
# v1.40.0 to fix what v1.37.0 got wrong.
#
# v1.37.0 recomputed the agreement's text and hash on the spot when one of these
# moved, and called that "effective without a restart". It was worse than that:
# from that instant new logins consented to the NEW version while everyone
# already in-world stayed on the OLD one, and the house sat in a MIXED-AGREEMENT
# STATE that drained only as each person happened to relog — hours, or days.
# **You cannot have different people under different agreements.** That is not a
# rollout detail; it is what a consent record is FOR.
#
# So a change here is refused live, exactly like a `[ranks]` change, and for a
# closely related reason: both are things every account is read against, and both
# have to move for everybody at once. On the next boot the value and the
# agreement re-resolve TOGETHER — which is also the only way the document can
# stay true, since applying the value live and the text at reboot would leave the
# agreement saying "30 minutes" while the buffer really kept 60.
#
# A restart is a clean cutover by construction: it drops everyone, and they
# reconnect and re-consent to the same version.
AGREEMENT_QUOTED = ("rev_window_minutes", "revtell_count", "event_retention_days")

# LIVE USE-TIME READS — the fall-through, written down (v1.52.4).
#
# ⛔ These fields are read off the SHARED `Config` at the moment they are used,
# by code that holds no copy of them, so moving the field IS the change and the
# ordinary "field: was → now" report is the truth. That has always been the
# default behaviour of `reload_config`; what was missing is that it was a
# fall-through — an unclassified field landed here by omission rather than by
# anybody deciding it belonged.
#
# ⚠ THE POINT OF THE LIST IS THE GUARD OVER IT
# (`test_every_config_field_is_classified`): every `Config` field must be in
# exactly one of the five, so a field added next year cannot quietly join the
# fall-through and be reported as applied when nothing reads it. Adding a field
# here is a claim, and the claim is checkable: grep the field and confirm every
# reader reaches it through `config.<field>` at use time. If ANY reader copies
# it at boot, it belongs in CONFIG_REAPPLY with a hook; if the copy cannot be
# re-applied, it belongs in CONFIG_INFRA.
CONFIG_USE_TIME = frozenset({
    # moderation: read per command run (`_ban_hours`, `.ban`/`.ban_ip`)
    "ban_default_hours", "ban_ip_default_hours", "ban_max_hours",
    # the login clocks: read when the window opens (`_login`, `_agreement_window`)
    "auth_phase_seconds", "agreement_seconds",
    # presence: read when a session detaches (`_grace_expire`)
    "disconnect_grace_seconds",
    # the editors' caps and the personal mute list: read on entry to the editor
    "profile_max_lines", "mail_max_lines", "mute_cap",
    # the two view windows: read by `.security` / `.staff_calls` as they render
    "security_window_hours", "staff_calls_window_hours",
    # invitations: stamped onto the Invite as it is made
    "invite_ttl_seconds",
    # the capture files' own clock: read by `capture.prune` on every pass, off
    # `self.config` — nothing copies it, so moving the field IS the change
    "report_capture_days",
    # `.reboot`: passed to the player at the moment it is invoked
    "reboot_pause_budget",
    # the countdown's marks and its three notice templates: read by control.py
    # per request, which is why `--announce` needs no restart to reword
    "restart_checkpoints", "restart_notice", "shutdown_notice",
    "shutdown_cancelled_notice",
    # `.source` — the AGPL §13 offer, read as it prints
    "source_url",
})


def _config_fields(cfg) -> list:
    return sorted(getattr(cfg, "__dataclass_fields__", {}))


def reload_config(server) -> ReloadResult:
    """Re-read `config.toml` and apply what can be applied.

    Everything else is already live by construction: `session.config` IS the
    server's one `Config`, and the values read at use time — the ban defaults,
    the view windows, the editor caps, the login clocks — need only the field to
    move.
    """
    from .config import load_config

    path = server.config_path
    if not path:
        return ReloadResult("config", ok=False,
                            errors=["no config file to re-read"])
    try:
        fresh = load_config(path)
    except Exception as exc:                     # tomllib, OSError, ValueError
        return ReloadResult("config", ok=False,
                            errors=[f"{path}: {type(exc).__name__}: {exc}"])

    live = server.config
    changes, notes = [], []
    infra_seen, per_session, gated = [], [], []

    for field in _config_fields(fresh):
        was, now = getattr(live, field, None), getattr(fresh, field, None)
        if was == now:
            continue
        if field in CONFIG_INFRA:
            # SEEN, NOT APPLIED. The live Config is deliberately left holding the
            # old value: it is what the running listeners and open stores were
            # built from, and a Config that disagreed with the process would be
            # a lie told to every later reader.
            infra_seen.append(field)
            continue
        if field in AGREEMENT_QUOTED:
            # ⛔ REBOOT-GATED. The field is NOT set either — same reasoning as
            # infra, and sharper: applying the value while the agreement it is
            # quoted in stays frozen would make the document say one number
            # while the house enforced another. They move together, at a boot,
            # or not at all.
            gated.append(f"{field} {was} → {now}")
            continue
        setattr(live, field, now)                # the shared object: live at once
        if field in CONFIG_PER_SESSION:
            per_session.append(f"{field} {was} → {now}")
            continue
        changes.append(f"{field} {was} → {now}")

    # Re-apply the boot-copied values. Explicit list, run only for what moved.
    for field, apply in CONFIG_REAPPLY:
        if any(c.startswith(field + " ") for c in changes):
            try:
                apply(server, getattr(live, field))
            except Exception as exc:             # a store that is not attached
                notes.append(f"could not re-apply {field}: {exc}")

    if gated:
        notes.append("⛔ RESTART TO APPLY — quoted in the agreement: "
                     + ", ".join(gated)
                     + ". Neither the value nor the agreement has changed. On the "
                       "next boot both move together and everyone re-consents on "
                       "reconnect — one version, one cutover")
    if per_session:
        notes.append("new connections only (per-session): " + ", ".join(per_session))
    if infra_seen:
        notes.append("seen but NOT applied (restart to change): "
                     + ", ".join(sorted(infra_seen)))
    return ReloadResult("config", changes=changes, notes=notes)


# ---------------------------------------------------------------------------
# Content — the world, and the authored files around it (v1.39.0, Run 2)
#
# ⛔ THE RULE THAT SHAPES ALL OF IT: **never swap the `World` object; graft into
# it.** A `World` holds the authored rooms AND the live house — `residents`,
# `locked_rooms`, `topics`, the entry room's fading `review` buffer. Replacing
# it would empty every room, drop every lock, forget every topic and wipe the
# room's memory, all to change a paragraph of prose. So the new rooms are moved
# into the live object and everything else is left exactly where it is.
#
# This is also where the spine's **guard** step earns its keep. Run 1's two
# sources needed none: a permissions table and a config value are true or false
# on their own. A room is not — whether a change may be applied depends on who
# is standing in it.
# ---------------------------------------------------------------------------

def _structural(old, new) -> str:
    """Why this room's change cannot be applied to somebody standing in it.

    Returns a short reason, or "" for a change that is safe to apply under an
    occupant's feet.

    ⚠ The line is drawn at **what could strand or eject a person**:

      * the room is GONE — they would be nowhere;
      * `staff_only` turned ON — they are suddenly somewhere they may not be;
      * an exit REMOVED or retargeted — the door they were about to use is not
        the door they were looking at.

    Everything else is prose: the name, the description (wide or narrow), the
    refusal line, `lockable` turned on, an exit ADDED. None of those can move
    anybody or take anything away, so they apply immediately — which matters,
    because prose is what an author actually iterates on, and having to empty a
    room to fix a typo would make the whole feature useless in the room people
    are most likely to be in.
    """
    if new is None:
        return "removed"
    if new.staff_only and not old.staff_only:
        return "became staff-only"
    lost = [e for e in old.exits if e not in new.exits]
    if lost:
        return "exit(s) removed: " + ", ".join(lost)
    return ""


def reload_world(server) -> ReloadResult:
    """Re-read `house/world/` and graft it onto the live World.

    validate -> guard (occupants, the named rooms) -> graft -> diff.
    """
    from .world import WorldError, load_world, one_way_passages
    from .server import _load_echoes

    cfg = server.config
    world_dir = cfg.world_path()
    live = server.world
    try:
        # ⚠ Parsed with the LIVE entry/jail rooms, not the config's, so this
        # cannot be the thing that changes them: `entry_room`/`jail_room` are
        # infra as far as the config source is concerned (a restart), and a
        # world reload must not smuggle a change to them in sideways. What it
        # DOES check is that the rooms they name still exist — which is
        # `load_world`'s own validation, so a world that removed the front door
        # is rejected here with its own message.
        fresh = load_world(world_dir, live.start_room, live.jail_room or "")
    except WorldError as exc:
        return ReloadResult("world", ok=False, errors=[str(exc)])

    # ---- guard: rooms somebody is standing in -------------------------
    deferred, merged = [], dict(fresh.rooms)
    for rid, old in live.rooms.items():
        reason = _structural(old, fresh.rooms.get(rid))
        if not reason:
            continue
        here = live.occupants(rid, connected_only=False)
        if not here:
            continue
        deferred.append(f"{rid} unchanged ({reason}; occupied by {len(here)})")
        merged[rid] = old                     # keep the room they are in

    if deferred:
        # ⚠ A kept room's exits were validated against the OLD set, so the merge
        # has to be re-validated: keeping `cottage` is no good if the room its
        # door leads to has just been deleted. If the mixture does not hold
        # together, the whole pass waits rather than leaving a door that opens
        # on nothing — a partly-applied world is the one outcome worse than a
        # late one.
        broken = [f"{rid} -> {e}" for rid, r in merged.items()
                  for e in r.exits if e not in merged]
        if broken:
            return ReloadResult("world", ok=False, errors=[
                "deferred (occupied) rooms would leave exits pointing nowhere: "
                + ", ".join(sorted(broken))
                + " — the whole world change waits until those rooms empty"])

    for named, what in ((live.start_room, "entry_room"),
                        (live.jail_room, "jail_room")):
        if named and named not in merged:
            return ReloadResult("world", ok=False, errors=[
                f"the new world has no room {named!r}, which is the house's "
                f"{what} — rejected"])

    # ---- diff (computed before the graft, while both sets are in hand) -
    added = sorted(set(merged) - set(live.rooms))
    gone = sorted(set(live.rooms) - set(merged))
    changed = sorted(rid for rid in set(merged) & set(live.rooms)
                     if merged[rid] != live.rooms[rid])
    changes = []
    if changed:
        changes.append(f"{len(changed)} room(s) changed: " + ", ".join(changed))
    if added:
        changes.append(f"{len(added)} added: " + ", ".join(added))
    if gone:
        changes.append(f"{len(gone)} removed: " + ", ".join(gone))

    echoes = _load_echoes(world_dir / "echoes.txt")
    if echoes != live.echoes:
        changes.append(f"echoes: {len(live.echoes)} -> {len(echoes)}")
    if fresh.ascii_map != live.ascii_map or \
            fresh.ascii_map_narrow != live.ascii_map_narrow:
        changes.append("map redrawn")

    # ---- apply: THE GRAFT ---------------------------------------------
    #
    # In place, field by field, and only the authored ones. `residents`,
    # `locked_rooms`, `topics` and `review` are never touched — they are the
    # live house, and this is a change of scenery, not of cast.
    live.rooms.clear()
    live.rooms.update(merged)
    live.ascii_map = fresh.ascii_map
    live.ascii_map_narrow = fresh.ascii_map_narrow
    live.one_way = one_way_passages(merged)
    live.staff_only_rooms = {rid for rid, r in merged.items() if r.staff_only}
    live.echoes = echoes

    notes = []
    if deferred:
        # Reported, and retried on every later tick until the room empties —
        # see `HouseReloader.check_once`. (`world.room_emptied()` is the moment
        # it becomes possible, but nothing has to be wired into it: the retry
        # costs one parse per poll and only while something is waiting.)
        notes.append("waiting for these to empty: " + "; ".join(deferred))
    return ReloadResult("world", changes=changes, notes=notes,
                        deferred=bool(deferred))


# ---------------------------------------------------------------------------
# The authored files around the world
# ---------------------------------------------------------------------------

def reload_greeting(server) -> ReloadResult:
    """The login banner. Read at login, so replacing the attribute IS live."""
    from .server import _load_greeting
    from .world import read_narrow

    path = server.config.greeting_path()
    was, was_narrow = server.greeting, server.greeting_narrow
    server.greeting = _load_greeting(path)
    server.greeting_narrow = read_narrow(path)
    changed = (server.greeting != was) or (server.greeting_narrow != was_narrow)
    return ReloadResult("greeting",
                        changes=["banner redrawn"] if changed else [])


def reload_help(server) -> ReloadResult:
    """The `.help <topic>` pages.

    ⚠ **Mutated IN PLACE** (`clear()` + `update()`), not replaced. Every open
    session was handed this very dict at construction (`session.py`:
    `self.help_topics = help_topics or {}`), so replacing the server's attribute
    would reach new connections only — and the person who most wants a corrected
    help page is the one already asking for it.
    """
    from .server import _load_help_topics

    fresh = _load_help_topics(server.config.help_path())
    live = server.help_topics
    added = sorted(set(fresh) - set(live))
    gone = sorted(set(live) - set(fresh))
    edited = sorted(t for t in set(fresh) & set(live) if fresh[t] != live[t])
    live.clear()
    live.update(fresh)
    changes = []
    for label, names in (("edited", edited), ("added", added), ("removed", gone)):
        if names:
            changes.append(f"{label}: " + ", ".join(names))
    return ReloadResult("help", changes=changes)


def reload_reboot(server) -> ReloadResult:
    """The `.reboot` script. Parsed TWICE, at both widths, exactly as at boot —
    the parse pre-wraps every line, so one parse cannot serve both."""
    from . import reboot, session as session_mod

    text = reboot.load_script(server.config.reboot_path())
    was = server.reboot_steps
    server.reboot_steps = (reboot.parse(text, width=server.config.wrap_columns)
                           if text is not None else None)
    server.reboot_steps_narrow = (
        reboot.parse(text, width=session_mod.NARROW_COLUMNS)
        if text is not None else None)
    changed = server.reboot_steps != was
    return ReloadResult("reboot", changes=["script reloaded"] if changed else [])


def reload_theme(server) -> ReloadResult:
    """The house's WORDS — and, refused, its ranks.

    ⛔ **A `[ranks]` change is reboot-only.** The ladder is the shape every
    stored rank is read against, and changing it triggers the v1.38.0 reorg
    collapse — a migration that rewrites `accounts.json` and demotes people.
    That cannot happen under a live house: residents are standing in rooms
    holding ranks, sessions have resolved their gates, and half of it would
    apply to whoever happened to be connected. So the ranks are compared by
    fingerprint and, if they moved, the ladder is KEPT and the operator is told
    to restart. The rest of the file — the instance name, the staff plural, the
    favicon — applies immediately.
    """
    from . import theme as theme_mod

    path = server.config.theme_path()
    live = server.theme
    try:
        fresh = theme_mod.load_theme(path)
    except theme_mod.ThemeError as exc:
        return ReloadResult("theme", ok=False, errors=[str(exc)])

    notes, changes = [], []
    if fresh.rank_fingerprint() != live.rank_fingerprint():
        notes.append("⚠ [ranks] changed — NOT applied: the ladder is what every "
                     "stored rank is read against, and changing it re-places "
                     "every account (v1.38.0). RESTART to apply rank changes; "
                     "the rest of theme.toml has been applied")
        # Keep the live ladder, take the display words. (`newb`/`owner` are
        # derived from `order`, so putting `order` back puts them back too; the
        # level DISPLAY names in `level_display` are taken from `fresh` and apply
        # live, which is the whole point.)
        fresh = dataclasses.replace(fresh, order=live.order,
                                    resident=live.resident, staff=live.staff)
    # ⛔ The house's NAME is not in this file since v1.60.0 — it is
    # `config.toml [world] instance_name`, with its own reload hook. `fresh`
    # therefore carries the Theme DEFAULT, and taking it would rename the house
    # to "the house" on any theme.toml save. Carry the live one across.
    fresh = dataclasses.replace(fresh, instance_name=live.instance_name)
    if fresh.favicon != live.favicon:
        changes.append(f"favicon {live.favicon!r} -> {fresh.favicon!r}")
    if fresh.level_display != live.level_display:
        changes.append("level display names updated")
    if fresh == live and not notes:
        return ReloadResult("theme")

    server.theme = fresh
    if fresh.favicon != live.favicon:
        server.favicon_url = _reload_favicon(server)
        changes.append("favicon reloaded")

    _adopt_theme(server, fresh)
    return ReloadResult("theme", changes=changes, notes=notes)


def _adopt_theme(server, fresh) -> None:
    """Swap the live theme and hand it to everyone standing in the house.

    ⚠ The theme is handed to each Session at construction, so an open session
    holds the OLD object; and `Theme` is frozen, so every change here is a
    replacement rather than a field poke. Hand every connected one the new
    theme, and push the two bits the BROWSER holds — the tab's name and its
    icon — which it cannot discover for itself. `push_raise_bounds_all` is the
    precedent: a house-wide setting has to reach everyone, not just the next
    arrival.

    ⚠ ONE helper for both callers, because `instance_name` now arrives from
    `config.toml` and `theme.toml` still carries the words: two near-identical
    fan-outs is how one of them comes to forget `res.ladder`.
    """
    server.theme = fresh
    from .commands.settings import push_house_identity_all
    for res in server.world.all_connected():
        if res.session is not None:
            res.session.theme = fresh
            res.session.favicon_url = server.favicon_url
        res.ladder = fresh
    push_house_identity_all(server.world)


def _reload_favicon(server) -> str:
    from .server import _load_favicon
    return _load_favicon(server.config, server.theme.favicon)


def reload_agreement(server) -> ReloadResult:
    """The house's terms — ⛔ REBOOT-GATED. Seen, reported, never applied live.

    ⚠ **v1.39.0 applied this live and that was wrong.** It re-resolved and
    re-hashed on the spot, so from that instant new logins consented to the NEW
    version while everyone already in-world stayed on the OLD one — a
    MIXED-AGREEMENT STATE that drained only as each person happened to relog.
    **You cannot have different people under different agreements.** A consent
    record whose subject depends on when you last reconnected is not one.

    So the agreement is frozen at boot (`resolve_agreement` → `agreement_version`,
    once, in `TalkerServer._build`) and NOTHING recomputes it while the process
    runs. A change here is reported and waits.

    **A restart is a clean cutover by construction**: it drops everyone, and they
    reconnect and re-consent to the same version, together. That is worth a
    restart in a way that almost nothing else here is.

    The file is still WATCHED — an operator who edits it should be told the house
    is now behind its own terms, rather than discovering it at the next
    unrelated reboot.
    """
    from .server import _load_agreement, agreement_version, resolve_agreement

    try:
        resolved = resolve_agreement(
            _load_agreement(server.config.agreement_path()), server.config,
            server.theme)
    except (ValueError, OSError) as exc:
        return ReloadResult("agreement", ok=False, errors=[str(exc)])

    if agreement_version(resolved) == server.agreement_hash:
        return ReloadResult("agreement")
    # ⛔ Reported, never applied. Note the version is NOT recomputed onto the
    # server — it is computed here, compared, and thrown away.
    return ReloadResult("agreement", notes=[
        "⛔ agreement.txt CHANGED — RESTART TO APPLY. The running house is still "
        "on the version everyone consented to; a live change would leave new "
        "logins on the new terms and everyone already here on the old ones. "
        "Restarting drops everyone and they re-consent together, to one version"])


# ---------------------------------------------------------------------------
# The reloader
# ---------------------------------------------------------------------------

class Source:
    """One watched surface: a name, the files that drive it, and its reload."""

    def __init__(self, name: str, paths, run):
        self.name = name
        self._paths = paths            # callable(server) -> [paths]
        self.run = run

    def paths(self, server) -> list:
        return [p for p in self._paths(server) if p]


def _world_paths(s):
    """Every file the world is built from — the rooms directory's contents (so a
    room APPEARING or being DELETED counts, not just an edit), the map and its
    narrow variant, and the echoes."""
    d = s.config.world_path()
    return (sorted(str(p) for p in (d / "rooms").glob("*.txt"))
            + [d / "map.txt", d / "map.narrow.txt", d / "echoes.txt"])


SOURCES = (
    Source("permissions", lambda s: [s.config.permissions_path()],
           reload_permissions),
    # Its own source, not part of "permissions": different file, different
    # owner's decision, and a diff that reads quite differently. They only
    # happen to live in the same directory.
    Source("forbidden_names", lambda s: [s.config.forbidden_names_path()],
           reload_forbidden),
    Source("config", lambda s: [s.config_path], reload_config),
    # ---- content (v1.39.0) ----
    Source("world", _world_paths, reload_world),
    Source("greeting", lambda s: [s.config.greeting_path(),
                                  s.config.greeting_path().with_suffix(
                                      ".narrow.txt")],
           reload_greeting),
    Source("help", lambda s: sorted(str(p) for p in
                                    s.config.help_path().glob("*.txt")),
           reload_help),
    Source("reboot", lambda s: [s.config.reboot_path()], reload_reboot),
    # theme.toml drives the words AND the favicon, so one source owns both —
    # they arrive in the same edit and there is nothing to gain by pretending
    # otherwise.
    Source("theme", lambda s: [s.config.theme_path()], reload_theme),
    Source("agreement", lambda s: [s.config.agreement_path()], reload_agreement),
)


class HouseReloader:
    """The watch loop, and the one entry point everything else uses.

    `check_once()` is the whole mechanism and is synchronous, so tests drive it
    directly rather than waiting on a clock; `loop()` is the thin asyncio task
    around it, a sibling of `_purge_loop` and `_echo_loop`.
    """

    def __init__(self, server, sources=SOURCES, clock=time.time):
        self.server = server
        self.sources = tuple(sources)
        self.watcher = Watcher(clock=clock)
        self._clock = clock
        # Deferred sources — ones that held something back for the live house's
        # sake and want another go. Retried on every tick while they say so.
        self._deferred = set()
        for src in self.sources:
            self.watcher.prime(src.name, src.paths(server))

    def check_once(self, force=()) -> list:
        """Run any source whose files changed. Returns the results, newest pass.

        `force` names sources to run regardless — for a test, or a future
        explicit trigger. Nothing in the shipped engine passes it.
        """
        results = []
        for src in self.sources:
            touched = self.watcher.changed(src.name, src.paths(self.server))
            # ⚠ A source that DEFERRED runs again even with nothing changed on
            # disk: what it is waiting for is not a file, it is a room emptying.
            # One parse per poll, and only while something is actually waiting.
            if not touched and src.name not in force \
                    and src.name not in self._deferred:
                continue
            try:
                result = src.run(self.server)
                if result.deferred:
                    self._deferred.add(src.name)
                else:
                    self._deferred.discard(src.name)
                # A retry that changed nothing and is still waiting is not news;
                # reporting it every few seconds would bury the real report.
                if touched or src.name in force or result.changes \
                        or not result.ok:
                    results.append(result)
            except Exception as exc:
                # A source that throws must not take the house with it. The
                # report is the fact; the traceback goes to the process log.
                log.exception("reload: %s raised", src.name)
                results.append(ReloadResult(
                    src.name, ok=False,
                    errors=[f"{type(exc).__name__}: {exc}"]))
        if results:
            self.publish(results)
        return results

    def publish(self, results) -> None:
        """Report a pass three ways: the process log, `.load_report`, and a fact.

        The event is a `staff_action` (the stored type; it READS as a staff
        action everywhere it is shown) because that is what it IS — a change to
        who may do what, and to the house's settings, made deliberately. The
        actor is `config`: the operator acted through the filesystem rather than
        through a command, and naming the mechanism is more honest than naming
        nobody. No content, as ever; the diff lines are field names and rank
        words.
        """
        report = ReloadReport(results, self._clock())
        lr = getattr(self.server, "load_report", None)
        if lr is not None:
            lr.last_reload = report
        events = getattr(self.server, "events", None)
        for r in results:
            log.info("reload: %s", r.summary())
            if events is not None:
                events.record("staff_action", actor="config",
                              action="reload" if r.ok else "reload_rejected",
                              target=r.source)

    async def loop(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    self.check_once()
                except Exception:                # never let the task die
                    log.exception("reload: watch pass failed")
        except asyncio.CancelledError:
            pass
