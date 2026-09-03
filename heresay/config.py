# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Configuration loading.

The only disk state this project reads at startup is this config file, the
room definitions, and the account file. Everything else lives in memory.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass, fields as dc_fields, field
from pathlib import Path

log = logging.getLogger("heresay")

# The shipped placeholder for the one REQUIRED value with no default. An install
# copies config.example.toml, and the house refuses to boot until this is set to
# a real account name — the same refuse-until-configured posture as entry_room.
OWNER_NAME_PLACEHOLDER = "[owner_name]"

# ⛔ The shipped-placeholder SHAPE, and the whole of what makes an example config
# safe to hand a stranger: every value the installer must choose ships as
# `[some_name]`, and ANY string field still carrying one refuses the boot.
#
# One rule rather than a guard per key, because the failure it prevents is a
# house that OPENS on an unconfigured value — a source_url pointing nowhere, a
# state path under whatever directory the daemon happened to start in — and a
# per-key list is a list somebody must remember to extend when the example grows
# a line. The match is anywhere in the value, not just the whole of it, so a path
# built round one (`[state_dir]/accounts.json`) is caught as readily as a bare
# token. Config values are paths, hosts, account names and URLs; none of them
# legitimately contains a bracketed lowercase word.
PLACEHOLDER_RE = re.compile(r"\[[a-z][a-z0-9_]*\]")


class ConfigError(ValueError):
    """A config value with no safe reading — the house refuses to boot.

    Reserved for the handful of settings where guessing would pick a policy on
    the operator's behalf: the required `owner_name`, and a `landing_level` that
    is not a level or is `owner` (a new account may never land at the supreme
    level). The same posture as `entry_room` (WorldError) and a bad
    `permissions.toml` value (PermissionsError).
    """


def validate_config(cfg: "Config") -> None:
    """Boot-time refusals for the values that must not fall back. Called from the
    server's `_build` before anything is stood up, so a misconfigured house fails
    loudly and immediately rather than opening in a wrong state."""
    from .theme import LEVELS
    if not cfg.owner_name or cfg.owner_name == OWNER_NAME_PLACEHOLDER:
        raise ConfigError(
            "owner_name is not set — the house has no owner. Set "
            "[moderation] owner = \"<the owner account's name>\" in config.toml "
            f"(it ships as the placeholder {OWNER_NAME_PLACEHOLDER!r}).")
    if cfg.landing_level not in LEVELS:
        raise ConfigError(
            f"[moderation] landing_level = {cfg.landing_level!r} is not a level. "
            f"The levels are {list(LEVELS)} (new accounts may land at any but "
            "'owner').")
    if cfg.landing_level == "owner":
        raise ConfigError(
            "[moderation] landing_level = 'owner' is not allowed — a new account "
            "may not land at the owner level. Choose newb, resident or staff.")
    for f in dc_fields(cfg):
        value = getattr(cfg, f.name)
        if isinstance(value, str) and PLACEHOLDER_RE.search(value):
            raise ConfigError(
                f"{f.name} = {value!r} is still the shipped placeholder — this "
                "house has not been configured. Edit config.toml and replace "
                f"every [bracketed] value with a real one (here: {f.name}).")


@dataclass
class Config:
    # Listeners
    tls_host: str = "0.0.0.0"
    tls_port: int = 2323
    plain_host: str = "127.0.0.1"
    plain_port: int = 2324
    ws_host: str = "127.0.0.1"
    ws_port: int = 8765
    # Trust X-Forwarded-For only when the socket peer is this address (Caddy).
    # Empty string = trust no proxy (use the socket peer IP verbatim).
    trusted_proxy: str = ""

    # Certificate material for the TLS door (a cert and a key, nothing more —
    # which is why the config section is [cert] rather than [tls]).
    cert_file: str = "certs/cert.pem"
    key_file: str = "certs/key.pem"

    # Disk state (the only files that persist)
    accounts_file: str = "accounts.json"
    mail_file: str = "mailboxes.json"
    mail_key_file: str = "mail.key"
    revtell_file: str = "revtells.json"   # encrypted .revtell buffers (same key)
    bans_file: str = "bans.json"
    # Resume tokens ("remember me", web door only). Same class of data as bans —
    # operational facts about who may come in, never content. Defaults BESIDE
    # bans.json, in the daemon's state dir, for the same reason revtells do.
    resume_file: str = "resume.json"
    state_file: str = "state.json"          # operational toggles (private mode)
    # Who may run what (v1.37.0). Beside config.toml — CONFIG the operator edits,
    # not STATE the daemon writes, and the two must not share a file: state.json
    # is rewritten from memory on every `.ownerset`/`.jail`/`.logins`, which
    # silently ate hand edits for nine versions. ⚠ Deliberately NOT under
    # house_dir: deploy.sh rsyncs that with --delete --delete-excluded, so a file
    # there is overwritten by every push (and an exclude would DELETE it).
    permissions_file: str = "permissions.toml"
    # Names the operator will not have registered — their own list, beside
    # permissions.toml for exactly the same reasons: it is CONFIG they edit, the
    # daemon only ever reads it, and /etc is the one place a deploy cannot
    # reach. Absent is the empty list; see forbidden.py.
    forbidden_names_file: str = "forbidden_names.txt"
    # The local control socket (v1.46.0) — how `deploy.sh` and `--shutdown` get
    # a line to every connected session. STATE, not config: the daemon creates
    # it, so it belongs in the state dir beside the files the daemon writes.
    # ⛔ A socket file, never a port; see control.py.
    control_socket_file: str = "control.sock"
    # The engine's operational event log (one JSON object per line). Same class
    # of data as bans and resume tokens — facts about who came in and what staff
    # did, never a word anyone said — so it defaults BESIDE them in the state
    # dir, for the same reason. See events.py.
    events_file: str = "events.jsonl"
    # Every ban ever placed, kept longer than the ban itself so "has this
    # happened before?" survives the ban expiring. Holds the address (the ONE
    # place that does) so recidivism can be matched internally; see banhistory.py.
    ban_history_file: str = "ban_history.json"

    # ---- authored content ------------------------------------------------
    # ONE root, `house_dir`, and every content path is a name RELATIVE TO IT.
    # Set house_dir and you are done: a deployment says
    # `house_dir = "/opt/heresay/house"` and every file below is found.
    #
    # This replaced an "anchor" scheme where agreement_file carried the path and
    # everything else defaulted to a bare name beside it. That worked, but it made
    # one content file secretly structural — moving the agreement moved the world.
    # A root is the thing an operator actually has in mind.
    #
    # The per-file keys stay as OPTIONAL overrides for renaming or nesting
    # (`agreement_file = "covenant.txt"`, `world_dir = "worlds/summer"`). They are
    # joined under house_dir; an ABSOLUTE override still wins outright, so an
    # existing config full of absolute content paths keeps working untouched.
    house_dir: str = "house"                # the content root (config-relative)
    agreement_file: str = "agreement.txt"   # shown at account creation
    greeting_file: str = "greeting.txt"     # login banner (optional; built-in fallback)
    reboot_file: str = "reboot.txt"         # .reboot boot-theater script (optional)
    theme_file: str = "theme.toml"          # rank display names (optional)
    help_dir: str = "help"                  # .help <topic> pages (optional)
    world_dir: str = "world"                # the authored world: map.txt + rooms/

    # ---- the world -------------------------------------------------------
    # Which room arrivals land in, and which room `.jail` sends people to. Both
    # are named HERE rather than in a room header (v1.17.0): the entry room is a
    # property of the deployment, not of the room — the same `house/` can be run
    # with a different front door, and a room file that says "I am the entry"
    # cannot be reused without editing it.
    #
    # entry_room is REQUIRED and must name a room that exists; the server refuses
    # to start otherwise. There is no fallback world any more — a house with no
    # rooms, or a front door that names nothing, is a mistake to fix, not a void
    # to stand in. jail_room is optional; unset means `.jail` says so plainly.
    # ⛔ THE HOUSE'S NAME (v1.60.0 — moved out of `house/theme.toml`). It names
    # the DEPLOYMENT rather than describing its vocabulary, so it is
    # configuration and not theme — and the move is what makes a fresh install
    # ONE file to edit. ⛔ There is NO fallback read of the old location: a
    # `theme.toml` still carrying the key is warned about and ignored.
    #
    # ⚠ The DEFAULT is colourless and the REFUSAL lives in the shipped file —
    # the same shape this had as a theme field, deliberately preserved. The
    # skeleton `config.toml.example` says `[instance_name]`, which
    # `validate_config`'s placeholder sweep refuses to boot on, so no fresh
    # install can reach this value by accident; deleting the key is the only
    # way to it, and that is a house whose owner declined to name it rather
    # than one nobody finished configuring. Making the FIELD itself refuse
    # would make a cosmetic value mandatory in every construction of `Config`,
    # which is a cost paid by every caller for a check the file already makes.
    instance_name: str = "the house"
    entry_room: str = ""
    jail_room: str = ""

    # ⛔ AGPL §13: where the corresponding source can be had (v1.50.0).
    #
    # The licence obliges an operator running a MODIFIED Heresay as a network
    # service to offer its users the source. The repo has always carried the
    # licence; what was missing was any way for a user standing IN the house to
    # be told where to get it — `.version` names the engine and stops.
    #
    # ⚠ Empty is not a default so much as an admission: `.source` then says the
    # URL is not configured rather than inventing one. A PUBLIC instance must
    # set it. It is deliberately not defaulted to this project's own repository,
    # because a modified instance pointing at the unmodified upstream would be
    # worse than saying nothing — it would be a wrong answer wearing the shape
    # of a right one.
    source_url: str = ""

    # Moderation
    # ⛔ The OWNER account is named here and is the owner at login regardless of
    # the stored rank field. There is NO default — it ships as the placeholder
    # `owner_name = "[owner_name]"` and the house REFUSES TO BOOT until it is set
    # (server._require_owner_name), the same refuse-until-configured posture as
    # `entry_room`. Empty is the in-memory sentinel for "unset". (Was `arch_name`,
    # default `"Generic"`, before v1.54.0.)
    owner_name: str = ""
    # Which LEVEL a NEW account lands in — `newb`, `resident` or `staff` (never
    # `owner`; that is a boot-fatal config error). Resolves to that level's anchor
    # rank at creation. Default `newb` = the bottom of the ladder, exactly the
    # pre-v1.54 behaviour (accounts landed at `order[0]`).
    landing_level: str = "newb"
    ban_default_hours: float = 24.0
    # `.ban_ip` gets its own default rather than sharing `.ban`'s: banning an
    # ADDRESS is the blunter instrument (it can catch a household, a campus, a
    # phone carrier's NAT), so an operator should be able to keep it short
    # without shortening account bans too.
    ban_ip_default_hours: float = 24.0
    ban_max_hours: float = 24.0 * 365   # a settable ceiling (1 year)
    # How long the ban HISTORY is kept. 0 = forever, and that is the shipped
    # default — the opposite of the event log's, deliberately: the event log
    # answers "what is happening" (a question about now), this answers "has this
    # happened before" (a question whose value is in the years).
    ban_history_days: float = 0.0

    # ---- the graceful countdown (v1.46.0) --------------------------------
    # When `deploy.sh` warns the house before a restart: the seconds-remaining
    # marks, largest first. Announced at each; the restart lands after the last.
    restart_checkpoints: tuple = (120, 60, 10)
    # ⚠ Both templates take `{instance}` and `{t}` ("2 minutes", "10 seconds").
    # NEVER a hardcoded house name — the v1.45.0 wording guard scans these, and
    # a themed house must say its own name in its own warning.
    restart_notice: str = "{instance} is restarting in {t} — reconnect in a moment."
    shutdown_notice: str = "{instance} is going down in {t}."
    shutdown_cancelled_notice: str = "{instance} is not going down after all."

    # Line handling
    wrap_columns: int = 78

    # The entry room's fading memory (.rev): every say/emote/echo from the last
    # rev_window_minutes, capped at the newest rev_max_lines. In RAM only.
    rev_window_minutes: float = 30.0
    rev_max_lines: int = 100
    # A DIFFERENT number, and worth keeping straight: how many of your own
    # tell/remote lines .revtell holds (per account, encrypted, on disk). The two
    # above bound one ROOM's public murmur; this bounds one PERSON's private
    # buffer. The agreement quotes this one as {revtell_count}, so changing it
    # re-consents everyone — see resolve_agreement.
    revtell_count: int = 20

    # How long the engine's operational event log keeps a fact (events.jsonl).
    # ONE window for every event type — a per-type table is something an operator
    # would have to reason about, and the honest answer to "how long do we keep
    # operational facts?" is a single number. 0 or less means keep everything.
    event_retention_days: float = 10.0

    # The two event-log VIEWS have their own windows (v1.35.0), because "how long
    # do we keep this?" and "how far back does this screen look?" are different
    # questions: retention is a policy about the file, a window is a question
    # someone is asking today. Both are hours, both default to a day, and both
    # are CAPPED at the retention window — a screen cannot show what the store no
    # longer holds, so a larger setting simply means "everything retained".
    # Zero or less means the same thing, which is why neither can be misconfigured
    # into an empty screen. See `commands.view_window_hours`.
    security_window_hours: float = 24.0        # .security
    staff_calls_window_hours: float = 24.0     # .staff_calls

    # How often the engine looks to see whether a watched config file changed
    # (v1.37.0). A stat() over two files, so the cost of a short interval is
    # nothing; what it buys is "edit the file and it has happened" rather than
    # "edit the file and wait". 0 or less switches the watcher off entirely —
    # the house then behaves exactly as it did before live reload existed.
    reload_poll_seconds: float = 3.0

    # Atmospherics — the entry room murmurs after a stretch of quiet. An echo
    # fires after T minutes with no resident say/emote (T drawn uniformly from
    # [echo_min_minutes, echo_max_minutes]), only while occupied. Lines come from
    # world/echoes.txt; a missing/empty file makes the feature silent.
    echo_min_minutes: float = 10.0
    echo_max_minutes: float = 15.0

    # Rate limiting (see ratelimit.py for semantics)
    speech_burst: int = 5
    speech_refill_per_sec: float = 1.0
    speech_mute_strikes: int = 5
    speech_disconnect_strikes: int = 15
    speech_mute_seconds: float = 10.0
    shout_per_seconds: float = 30.0
    max_conns_per_ip: int = 3
    churn_window_seconds: float = 10.0
    churn_max_in_window: int = 5
    auth_backoff_base: float = 1.0
    auth_backoff_cap: float = 30.0
    # Pre-auth phase deadline (anti-slowloris): a connection has this many seconds
    # to finish logging in or creating an account before it's closed. Authenticated
    # sessions are never reaped by this (the deliberate no-idle-reaping policy).
    # This is the CREDENTIAL clock: the name prompt, the password prompts, the
    # takeover question. It does NOT run while the agreement is on screen — see
    # agreement_seconds.
    auth_phase_seconds: float = 60.0
    # Reading the house rules is the one part of the pre-auth phase that is not a
    # prompt: eight clauses of prose, on a phone, for someone who has never seen
    # them. Under the credential clock a careful reader was dropped mid-agreement,
    # so it gets its own budget — generous, but every bit as finite. The credential
    # clock is PAUSED for the duration and resumes with exactly what it had left,
    # so slow reading is never an excuse to dawdle at a password prompt.
    agreement_seconds: float = 300.0
    # Per-session command governor: a generous token bucket over ALL dotted
    # commands so no logged-in client can spam handler/disk work. Humans never hit
    # it; a spammer is capped at command_refill_per_sec sustained.
    command_burst: int = 20
    command_refill_per_sec: float = 5.0
    # Per-IP account-registration limit (anti-flood): a small burst, then a slow
    # refill, bounding how fast accounts.json can grow from one address.
    registration_burst: int = 3
    registration_refill_per_sec: float = 1.0 / 120.0   # 3, then 1 per 2 minutes
    # Cap on a session's personal .mute list (bounds per-session memory growth).
    mute_cap: int = 100
    # Server-WIDE failed-auth ceiling (blunts distributed/botnet guessing that
    # per-IP backoff can't see). When >threshold failed auths — logins AND
    # recovery attempts — occur within the rolling window, the door enters a
    # cooldown: new auth attempts are slowed and new-account creation is paused.
    # Authenticated sessions are untouched. A deliberate self-DoS tradeoff, so it
    # is switchable off and tuned generously. 50/60s is well above any legitimate
    # rate (real users rarely mistype en masse) yet trips on a real guessing run.
    global_auth_fail_window_seconds: float = 60.0
    global_auth_fail_threshold: int = 50
    global_auth_cooldown_seconds: float = 120.0
    global_auth_enabled: bool = True

    # Presence. The window a dropped session is held before its departure is
    # announced — long enough that a phone losing Wi-Fi, a tab discarded by a
    # backgrounded browser, or a walk through a tunnel is not a leave/enter for
    # the whole house to read. Raised 60 -> 120 in v1.24.0 alongside remember-me:
    # the two together are what make "glance at a text and come back" seamless.
    disconnect_grace_seconds: float = 120.0

    # TCP keepalives on the socket doors (TLS + plaintext). These let the kernel
    # notice a half-open peer (sleep/Wi-Fi/NAT death with no FIN) and drop the
    # socket, which starts the normal disconnect → 60s grace path so a ghost
    # session evaporates on its own. With the defaults a dead peer is detected in
    # ~keepidle + keepcnt*keepintvl ≈ 200s. This is NOT idle reaping: it acts on
    # dead sockets, never on an idle-but-connected human (see CLAUDE.md).
    tcp_keepidle: int = 120     # seconds of quiet before the first probe
    tcp_keepintvl: int = 20     # seconds between probes
    tcp_keepcnt: int = 4        # failed probes before the socket is declared dead

    # Mail + profiles
    mailbox_cap: int = 50          # messages per mailbox
    mail_purge_days: int = 180     # messages older than this are deleted
    mail_send_burst: int = 3       # per-session mail-send token bucket
    mail_send_refill_per_sec: float = 0.05   # 1 per 20 seconds
    profile_max_lines: int = 20
    mail_max_lines: int = 40
    # ⚠ REPORTS AGE ON THEIR OWN NUMBER, not mail's 180 (v1.50.0). They share
    # the mail store, the key and the purge loop — but not the retention, and
    # since v1.58.0 not the cap either.
    #
    # ⛔ 100 → 10 IN v1.58.0, AND THE REASON IS THE CAPTURE FILE. A report is
    # written to disk in full at the instant of filing (see capture.py), so the
    # copy in the box is no longer the record — it is the working copy the
    # reader opens with `.read_report`. Ten days matches
    # `event_retention_days`, which is the honest window for anything the
    # engine holds about people: the house goes back to keeping nothing
    # durable, and the operator's disk keeps the record.
    report_retention_days: int = 10
    # ⛔ THE REPORT BOX HAS ITS OWN CAP (v1.58.0). Inheriting `mailbox_cap`'s 50
    # was an ACCIDENT of sharing the store, and it meant fifty junk reports
    # could wedge the report channel shut — the 51st is refused outright, and a
    # resident with something real to say is told the box is full. A thousand
    # is not a number anybody reaches by hand.
    # ⚠ NO FULL-BOX NOTIFICATION, deliberately (the maintainer's call): the operator is
    # nudged on every report already, and would see a flood long before 1000.
    report_box_cap: int = 1000
    # How long a capture file lives on disk. 375 days — a year of coverage
    # counted from the EVENT rather than from the filing, since the capture
    # already reaches ten days back. `0` keeps everything, like
    # `event_retention_days`.
    report_capture_days: int = 375

    # .reboot boot theater (a personal, invoker-only sequence — see reboot.py)
    reboot_rate_seconds: float = 60.0    # own bucket: 1 per this many seconds/session
    reboot_pause_budget: float = 240.0   # total @pause/@progress time, clamped to not stall

    # .call_staff (residents call staff): own stern bucket, 1 per this many
    # seconds.
    staff_call_seconds: float = 300.0   # 5 minutes

    # Invitations
    invite_burst: int = 3
    invite_refill_per_sec: float = 1.0 / 30.0   # 1 per 30 seconds
    invite_ttl_seconds: float = 300.0            # 5 minutes

    # Where the config itself lives (for resolving relative paths)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.base_dir / p)

    def revtell_path(self) -> Path:
        """Where the .revtell buffers live.

        An explicit ``[storage] revtell_file`` always wins. Left at its default,
        it sits **beside the mailboxes** rather than beside the config file: it
        shares mail's key, lifetime and sensitivity, so an operator upgrading an
        existing deployment without touching their config gets it in their state
        directory (/var/lib/heresay/) — not next to config.toml in /etc, where the
        daemon has no business writing and usually no permission to.
        """
        if self.revtell_file != Config.revtell_file:      # explicitly configured
            return self.resolve(self.revtell_file)
        return self.resolve(self.mail_file).parent / Config.revtell_file

    def resume_path(self) -> Path:
        """Where resume tokens live — beside bans.json by default.

        An explicit ``[storage] resume_file`` wins; otherwise it lands next to
        the other operational store rather than beside config.toml, so an
        existing deployment gets it in its state directory without a config edit
        (the same trap revtell_file was fixed for).
        """
        if self.resume_file != Config.resume_file:        # explicitly configured
            return self.resolve(self.resume_file)
        return self.resolve(self.bans_file).parent / Config.resume_file

    def ban_history_path(self) -> Path:
        """Beside bans.json by default — same class of data, same state dir."""
        if self.ban_history_file != Config.ban_history_file:
            return self.resolve(self.ban_history_file)
        return self.resolve(self.bans_file).parent / Config.ban_history_file

    def events_path(self) -> Path:
        """Where the event log lives — beside bans.json by default.

        Same rule, and the same trap avoided, as `resume_path`: an explicit
        ``[storage] events_file`` wins, otherwise it lands in the daemon's state
        directory rather than next to config.toml in /etc, where the daemon has
        no business writing.
        """
        if self.events_file != Config.events_file:        # explicitly configured
            return self.resolve(self.events_file)
        return self.resolve(self.bans_file).parent / Config.events_file

    def report_capture_path(self) -> Path:
        """Where the capture files live — a directory in the STATE dir.

        ⛔ DERIVED, not configured, and that is deliberate: it is not a knob an
        operator has any reason to turn, and every path field added is another
        thing `reload.py`'s classification guard must reason about and another
        thing to misconfigure. Same rule as `events_path`/`resume_path` — it
        lands beside the files the daemon writes, never next to `config.toml`
        in /etc where the daemon has no business writing.

        ⚠ It is INSIDE the state dir on purpose, so the nightly tarball carries
        it without a line being added to the backup script. The capture IS the
        record; a record excluded from the backup is a record for as long as
        the disk lasts.
        """
        return self.resolve(self.bans_file).parent / "reports"

    def control_socket_path(self) -> Path:
        """The local control socket — in the STATE dir, beside the files the
        daemon writes, because the daemon is what creates it. Same rule as
        `events_path`, and the opposite of `permissions_path`'s."""
        if self.control_socket_file != Config.control_socket_file:
            return self.resolve(self.control_socket_file)
        return self.resolve(self.bans_file).parent / Config.control_socket_file

    def permissions_path(self) -> Path:
        """Who may run what — BESIDE config.toml, not in the state dir.

        The opposite rule from `events_path`/`resume_path` above, and for the
        opposite reason: those are state the daemon writes, so they belong where
        the daemon writes. This is CONFIG the operator edits, so it belongs where
        the operator edits — next to config.toml, in /etc on a real deployment,
        which is also the one place a deploy cannot reach.
        """
        return self.resolve(self.permissions_file)

    def forbidden_names_path(self) -> Path:
        """The operator's blocklist — beside config.toml, for permissions'
        reasons exactly: config they edit, never state the daemon writes."""
        return self.resolve(self.forbidden_names_file)

    def house_path(self) -> Path:
        """The authored-content root. Everything below hangs off this."""
        return self.resolve(self.house_dir)

    def under_house(self, rel: str) -> Path:
        """A content path: ``rel`` joined under ``house_dir``.

        An ABSOLUTE ``rel`` wins outright (``Path.__truediv__`` says so), which is
        what lets a config written before house_dir existed — one carrying
        absolute paths for every content file — keep working with nothing changed
        but the addition of house_dir itself.
        """
        return self.house_path() / rel

    def help_path(self) -> Path:
        """Where the ``.help <topic>`` pages live."""
        return self.under_house(self.help_dir)

    def world_path(self) -> Path:
        """Where the authored world lives (``map.txt`` + ``rooms/``)."""
        return self.under_house(self.world_dir)

    def agreement_path(self) -> Path:
        """Where the agreement text lives. Fatal if missing — see server.py."""
        return self.under_house(self.agreement_file)

    def greeting_path(self) -> Path:
        """Where the login banner lives (``greeting.txt``)."""
        return self.under_house(self.greeting_file)

    def reboot_path(self) -> Path:
        """Where the ``.reboot`` script lives (``reboot.txt``)."""
        return self.under_house(self.reboot_file)

    def theme_path(self) -> Path:
        """Where the rank/instance words live (``theme.toml``)."""
        return self.under_house(self.theme_file)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    cfg = Config(base_dir=path.resolve().parent)
    if not path.exists():
        return cfg
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    listeners = data.get("listeners", {})
    cfg.tls_host = listeners.get("tls_host", cfg.tls_host)
    cfg.tls_port = int(listeners.get("tls_port", cfg.tls_port))
    cfg.plain_host = listeners.get("plain_host", cfg.plain_host)
    cfg.plain_port = int(listeners.get("plain_port", cfg.plain_port))
    cfg.ws_host = listeners.get("ws_host", cfg.ws_host)
    cfg.ws_port = int(listeners.get("ws_port", cfg.ws_port))
    cfg.trusted_proxy = listeners.get("trusted_proxy", cfg.trusted_proxy)

    # [cert], not [tls] (renamed in v1.17.0): the section holds a certificate and
    # a key and nothing else, and calling it [tls] implied it configured the TLS
    # door itself — which lives in [listeners]. No fallback to the old spelling:
    # an operator's config has to be edited for entry_room anyway, so a silent
    # fallback would only let a half-finished edit look finished.
    cert = data.get("cert", {})
    cfg.cert_file = cert.get("cert_file", cfg.cert_file)
    cfg.key_file = cert.get("key_file", cfg.key_file)

    storage = data.get("storage", {})
    cfg.house_dir = storage.get("house_dir", cfg.house_dir)
    cfg.accounts_file = storage.get("accounts_file", cfg.accounts_file)
    cfg.mail_file = storage.get("mail_file", cfg.mail_file)
    cfg.mail_key_file = storage.get("mail_key_file", cfg.mail_key_file)
    cfg.revtell_file = storage.get("revtell_file", cfg.revtell_file)
    cfg.bans_file = storage.get("bans_file", cfg.bans_file)
    cfg.resume_file = storage.get("resume_file", cfg.resume_file)
    cfg.state_file = storage.get("state_file", cfg.state_file)
    cfg.permissions_file = storage.get("permissions_file", cfg.permissions_file)
    cfg.forbidden_names_file = storage.get("forbidden_names_file",
                                           cfg.forbidden_names_file)
    cfg.control_socket_file = storage.get("control_socket_file",
                                          cfg.control_socket_file)
    cfg.events_file = storage.get("events_file", cfg.events_file)
    cfg.ban_history_file = storage.get("ban_history_file", cfg.ban_history_file)
    cfg.agreement_file = storage.get("agreement_file", cfg.agreement_file)
    cfg.greeting_file = storage.get("greeting_file", cfg.greeting_file)
    cfg.reboot_file = storage.get("reboot_file", cfg.reboot_file)
    cfg.theme_file = storage.get("theme_file", cfg.theme_file)
    cfg.help_dir = storage.get("help_dir", cfg.help_dir)
    cfg.world_dir = storage.get("world_dir", cfg.world_dir)

    mod = data.get("moderation", {})
    cfg.owner_name = mod.get("owner", cfg.owner_name)
    cfg.landing_level = str(mod.get("landing_level", cfg.landing_level))
    cfg.ban_default_hours = float(mod.get("ban_default_hours", cfg.ban_default_hours))
    cfg.ban_ip_default_hours = float(
        mod.get("ban_ip_default_hours", cfg.ban_ip_default_hours))
    cfg.ban_history_days = float(mod.get("ban_history_days", cfg.ban_history_days))
    cfg.ban_max_hours = float(mod.get("ban_max_hours", cfg.ban_max_hours))

    mail = data.get("mail", {})
    cfg.mailbox_cap = int(mail.get("mailbox_cap", cfg.mailbox_cap))
    cfg.mail_purge_days = int(mail.get("purge_days", cfg.mail_purge_days))
    cfg.mail_send_burst = int(mail.get("send_burst", cfg.mail_send_burst))
    cfg.mail_send_refill_per_sec = float(
        mail.get("send_refill_per_sec", cfg.mail_send_refill_per_sec)
    )
    cfg.profile_max_lines = int(mail.get("profile_max_lines", cfg.profile_max_lines))
    cfg.mail_max_lines = int(mail.get("mail_max_lines", cfg.mail_max_lines))
    cfg.report_retention_days = int(
        mail.get("report_retention_days", cfg.report_retention_days))
    cfg.report_box_cap = int(mail.get("report_box_cap", cfg.report_box_cap))
    cfg.report_capture_days = int(
        mail.get("report_capture_days", cfg.report_capture_days))

    reboot = data.get("reboot", {})
    cfg.reboot_rate_seconds = float(reboot.get("rate_seconds", cfg.reboot_rate_seconds))
    cfg.reboot_pause_budget = float(reboot.get("pause_budget", cfg.reboot_pause_budget))

    staff_sec = data.get("staff", {})
    cfg.staff_call_seconds = float(
        staff_sec.get("call_seconds", cfg.staff_call_seconds))

    invite = data.get("invite", {})
    cfg.invite_burst = int(invite.get("burst", cfg.invite_burst))
    cfg.invite_refill_per_sec = float(invite.get("refill_per_sec", cfg.invite_refill_per_sec))
    cfg.invite_ttl_seconds = float(invite.get("ttl_seconds", cfg.invite_ttl_seconds))

    # [world]. `entry_room` and `jail_room` are read again as of v1.17.0 — they
    # were the original scheme, moved into room headers in v1.1.0, and are back
    # here because which room is the front door is a property of the DEPLOYMENT.
    #
    # NOTE for anyone reading an old config: pre-1.1 configs spelled the entry
    # room `start_room`. That spelling is NOT revived — `entry_room` is the name
    # everywhere else in the codebase (`entry: yes`, "the entry room"), and
    # quietly honouring a key that has been ignored for sixteen minor versions
    # would be worse than failing loudly. Obsolete [storage] rooms_file and
    # defaults_file are likewise ignored, never fatal.
    # Top-level, beside instance-wide identity rather than under a section: it
    # is a fact about this deployment, not a knob on a subsystem.
    cfg.source_url = str(data.get("source_url", cfg.source_url)).strip()

    world = data.get("world", {})
    cfg.instance_name = str(world.get("instance_name", cfg.instance_name))
    cfg.entry_room = world.get("entry_room", cfg.entry_room)
    cfg.jail_room = world.get("jail_room", cfg.jail_room)
    cfg.wrap_columns = int(world.get("wrap_columns", cfg.wrap_columns))
    marks = world.get("restart_checkpoints")
    if marks is not None:
        # Coerced and filtered rather than trusted: a countdown built from a
        # mis-typed list must still count DOWN. See control.restart_schedule.
        try:
            cfg.restart_checkpoints = tuple(int(m) for m in marks)
        except (TypeError, ValueError):
            log.warning("config: [world] restart_checkpoints is not a list of "
                        "whole seconds — using %s", list(cfg.restart_checkpoints))
    cfg.restart_notice = str(world.get("restart_notice", cfg.restart_notice))
    cfg.shutdown_notice = str(world.get("shutdown_notice", cfg.shutdown_notice))
    cfg.shutdown_cancelled_notice = str(
        world.get("shutdown_cancelled_notice", cfg.shutdown_cancelled_notice))
    cfg.rev_window_minutes = float(world.get("rev_window_minutes", cfg.rev_window_minutes))
    cfg.rev_max_lines = int(world.get("rev_max_lines", cfg.rev_max_lines))
    cfg.revtell_count = int(world.get("revtell_count", cfg.revtell_count))
    cfg.event_retention_days = float(
        world.get("event_retention_days", cfg.event_retention_days))
    cfg.security_window_hours = float(
        world.get("security_window_hours", cfg.security_window_hours))
    cfg.staff_calls_window_hours = float(
        world.get("staff_calls_window_hours", cfg.staff_calls_window_hours))
    cfg.reload_poll_seconds = float(
        world.get("reload_poll_seconds", cfg.reload_poll_seconds))
    cfg.echo_min_minutes = float(world.get("echo_min_minutes", cfg.echo_min_minutes))
    cfg.echo_max_minutes = float(world.get("echo_max_minutes", cfg.echo_max_minutes))
    cfg.disconnect_grace_seconds = float(
        world.get("disconnect_grace_seconds", cfg.disconnect_grace_seconds)
    )

    keepalive = data.get("keepalive", {})
    cfg.tcp_keepidle = int(keepalive.get("tcp_keepidle", cfg.tcp_keepidle))
    cfg.tcp_keepintvl = int(keepalive.get("tcp_keepintvl", cfg.tcp_keepintvl))
    cfg.tcp_keepcnt = int(keepalive.get("tcp_keepcnt", cfg.tcp_keepcnt))

    rl = data.get("ratelimit", {})
    cfg.speech_burst = int(rl.get("speech_burst", cfg.speech_burst))
    cfg.speech_refill_per_sec = float(
        rl.get("speech_refill_per_sec", cfg.speech_refill_per_sec)
    )
    cfg.speech_mute_strikes = int(rl.get("speech_mute_strikes", cfg.speech_mute_strikes))
    cfg.speech_disconnect_strikes = int(
        rl.get("speech_disconnect_strikes", cfg.speech_disconnect_strikes)
    )
    cfg.speech_mute_seconds = float(rl.get("speech_mute_seconds", cfg.speech_mute_seconds))
    cfg.shout_per_seconds = float(rl.get("shout_per_seconds", cfg.shout_per_seconds))
    cfg.max_conns_per_ip = int(rl.get("max_conns_per_ip", cfg.max_conns_per_ip))
    cfg.churn_window_seconds = float(rl.get("churn_window_seconds", cfg.churn_window_seconds))
    cfg.churn_max_in_window = int(rl.get("churn_max_in_window", cfg.churn_max_in_window))
    cfg.auth_backoff_base = float(rl.get("auth_backoff_base", cfg.auth_backoff_base))
    cfg.auth_backoff_cap = float(rl.get("auth_backoff_cap", cfg.auth_backoff_cap))
    cfg.auth_phase_seconds = float(rl.get("auth_phase_seconds", cfg.auth_phase_seconds))
    cfg.agreement_seconds = float(rl.get("agreement_seconds", cfg.agreement_seconds))
    cfg.command_burst = int(rl.get("command_burst", cfg.command_burst))
    cfg.command_refill_per_sec = float(
        rl.get("command_refill_per_sec", cfg.command_refill_per_sec)
    )
    cfg.registration_burst = int(rl.get("registration_burst", cfg.registration_burst))
    cfg.registration_refill_per_sec = float(
        rl.get("registration_refill_per_sec", cfg.registration_refill_per_sec)
    )
    cfg.mute_cap = int(rl.get("mute_cap", cfg.mute_cap))
    cfg.global_auth_fail_window_seconds = float(
        rl.get("global_auth_fail_window_seconds", cfg.global_auth_fail_window_seconds)
    )
    cfg.global_auth_fail_threshold = int(
        rl.get("global_auth_fail_threshold", cfg.global_auth_fail_threshold)
    )
    cfg.global_auth_cooldown_seconds = float(
        rl.get("global_auth_cooldown_seconds", cfg.global_auth_cooldown_seconds)
    )
    cfg.global_auth_enabled = bool(
        rl.get("global_auth_enabled", cfg.global_auth_enabled)
    )

    return cfg
