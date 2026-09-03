# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The daemon: listeners, login orchestration, and presence lifecycle.

Operational logging is the ONLY logging: connection open/close with IP, auth
failures, and server errors — to stdout/journal. Never message content, never
command arguments. Grep the tree: no log call here takes user text.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import functools
import hashlib
import ipaddress
import logging
import re
import socket
import ssl
import time
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from . import __version__
from . import commands, passwords, reboot, recovery, resume as resume_mod, theme as theme_mod
from .accounts import AccountStore, valid_name
from .banhistory import BanHistoryStore
from . import capture
from .bans import BanStore
from .config import Config, validate_config
from . import control as control_mod
from .events import EventStore
from . import forbidden
from .loadreport import LoadReport
from .mail import MailStore
from . import permissions
from . import reload as reload_mod
from .revtell import RevTellStore
from .ratelimit import AuthBackoff, ConnectionChurn, GlobalAuthCeiling, PerIPRateLimiter
from .state import (StateStore, house_phrase, jail_presence_line, phrase_or_none,
                    presence_line)
from . import session as session_mod
from .session import Session
from .transport import SocketTransport, WebSocketTransport
from .world import Resident, World, load_world, read_narrow

log = logging.getLogger("heresay")

def reserved_names(theme, operator_names=()) -> set[str]:
    """Every name nobody may register: every RANK, plus the operator's list.

    ⚠ **A hardcoded pair of reserved words is GONE (v1.44.0).** The engine used
    to reserve two literals of its own, on top of the ranks — the historical
    staff-mail identities, kept as a floor under the rank rule. They said nothing
    the ladder did not already say on a house that used them as rank names, and
    on a house with another vocabulary they reserved two words it had no use for,
    which is the operator's call and not the engine's. Confirmed by test: a house
    whose LADDER names those words still refuses them with no
    `forbidden_names.txt` at all, and a house with another vocabulary gets them
    back unless it asks for them.

    The RANK half is built in and non-removable (a rank word is an identity
    here); the operator's half is `forbidden_names.txt` and is purely additive.
    """
    return forbidden.forbidden(theme, operator_names)

# Password policy (NIST-style: length bounds + a breach blocklist, no composition
# rules). 8 is the floor; 128 is a generous ceiling so passphrases fit.
PASSWORD_MIN = 8
PASSWORD_MAX = 128
# Fixed delay added to each NEW auth attempt while the server-wide auth-failure
# ceiling is in cooldown — slows a distributed run for everyone (a deliberate,
# switchable-off, generously-tuned self-DoS). Never applied to authenticated
# sessions.
AUTH_COOLDOWN_DELAY = 2.0
# ⛔ The largest inbound WebSocket frame the door will accept (v1.49.0, audit
# A2). The engine reads at most `transport.MAX_LINE_LENGTH` (1024 CHARACTERS)
# off any frame, so the library's 1 MiB default meant ~1000x was received,
# decompressed and buffered only to be discarded. 8 KB is the smallest bound
# that is comfortably safe: 1024 characters of four-byte UTF-8 is 4096 bytes,
# and a control frame has to fit beside that. See the `ws_serve` call.
WS_MAX_FRAME = 8192
COMMON_PASSWORD_MSG = (
    "That password is too common — it shows up on lists attackers try first.\n"
    "Pick something less predictable; a few random words strung together works well."
)


def _password_problem(pw: str) -> str | None:
    """Validate a NEW password (creation, .passwd change, recovery reset — all
    three). Order: length floor, length ceiling, then the common-password
    blocklist, so a short common password reports its length rather than its
    commonness. Returns a user-facing message, or None if the password is fine.
    The attempted password is never echoed and list membership is never revealed
    beyond this rejection."""
    if len(pw) < PASSWORD_MIN:
        return "Password too short (need at least 8 characters)."
    if len(pw) > PASSWORD_MAX:
        return "Password too long (maximum 128 characters)."
    if passwords.is_common(pw):
        return COMMON_PASSWORD_MSG
    return None


def _load_agreement(path) -> str:
    """Load the account-creation agreement text, failing fast if it's missing
    (like a broken room file — the operator must fix it before the server runs)."""
    p = Path(path)
    if not p.exists():
        raise ValueError(f"agreement file not found: {p}")
    return p.read_text(encoding="utf-8").rstrip("\n")


# Config-driven values the agreement may name, as {token} -> how to render it
# from the Config. The ONE place a new one is added; everything else — the
# substitution, the version, both widths — follows from being in this table.
#
# `:g` rather than str() because these are floats: `rev_window_minutes = 30.0`
# must read "30 minutes", not "30.0 minutes", while a genuine 22.5 survives.
# Each renders from (config, theme). Two sources because the agreement quotes
# two kinds of thing: numbers the DEPLOYMENT sets, and words the HOUSE calls
# itself by. Both belong here for the same reason — a document that states a
# figure the server does not enforce, or names a rank the house does not use,
# is worse than one that states neither.
AGREEMENT_PLACEHOLDERS = {
    "rev_window_minutes": lambda cfg, theme: f"{cfg.rev_window_minutes:g}",
    # A DIFFERENT number from the one above, and clause 1 names both: that is
    # one ROOM's public murmur, this is one PERSON's private buffer.
    "revtell_count": lambda cfg, theme: f"{cfg.revtell_count:d}",
    # Clause 3, the connection-recording disclosure (v1.31.1). How long the
    # event log keeps a fact — so changing retention re-consents, which is
    # right: the promise about how long you are remembered IS the disclosure.
    "event_retention_days": lambda cfg, theme: f"{cfg.event_retention_days:g}",
    # ...and who may read it, in the house's own word for that rank. A retheme
    # says "Steward" here without touching the agreement text.
    "owner": lambda cfg, theme: theme.owner_word,
}

_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def resolve_agreement(text: str, config, theme=None) -> str:
    """Substitute the config-driven values the agreement names.

    The agreement states a retention period, and a document that states one
    number while the server enforces another is worse than one that states
    none. So `agreement.txt` carries the TOKEN — `{rev_window_minutes}` — and
    the running config supplies the number. Single source of truth, and it
    cannot drift, because there is only one place the number exists.

    Run **once at startup, before both the hash and every render**, which is
    what makes the guarantee whole: the version is the sha256 of the RESOLVED
    text, so changing `rev_window_minutes` in config.toml changes what the
    agreement says, changes its version, and re-consents everyone — exactly as
    editing the prose does. Nothing can quietly re-point what people agreed to.

    ⛔ **Display-and-version time only. The FILE is never written**, so the token
    stays a token on disk and the house can be re-tuned by editing config alone.

    An unrecognised `{token}` is left exactly as written and named once in the
    log — a typo would otherwise reach a resident's screen with nobody the
    wiser. Not fatal: an operator's prose is allowed to contain a brace, and
    refusing to boot over one would be a house closed by a punctuation mark.
    """
    theme = theme or theme_mod.DEFAULT_THEME
    for name, render in AGREEMENT_PLACEHOLDERS.items():
        text = text.replace("{" + name + "}", render(config, theme))
    unknown = sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(text)}
                     - set(AGREEMENT_PLACEHOLDERS))
    if unknown:
        log.warning("agreement: unknown placeholder(s) left as written: %s",
                    ", ".join(unknown))       # names only, never the prose
    return text


def agreement_version(text: str) -> str:
    """The agreement's version IS its content: sha256 of the text, hex.

    Deliberately content-addressed rather than a hand-bumped number, because a
    hand-bumped number can be forgotten. The cost, accepted knowingly by the
    operator: ANY edit — a typo fix, a stray space — is a new version, and
    everyone re-consents once. Computed once at startup.

    Fed the **resolved** text (see `resolve_agreement`), so a config value the
    agreement quotes counts as content: change `rev_window_minutes` and this
    moves. Fed the resolved but UNWRAPPED text, so it is width-independent — a
    phone and a terminal read the same version of the same words.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# What a resident sees when the text has changed under them.
REVISED_NOTICE = "The agreement has been revised — please read and agree to continue:"
AGREE_PROMPT = "Do you agree? (y/n) "
DECLINED_TEMPLATE = "Fair enough. {instance} will be here if you change your mind."


def declined_msg(theme) -> str:
    """The line shown when someone declines the agreement — in the world's own
    name (theme.instance_name)."""
    return DECLINED_TEMPLATE.format(instance=theme.instance_name)


# ⛔ There is deliberately NO module-level DECLINED_MSG constant. There was one
# until v1.55.0 — `declined_msg(DEFAULT_THEME)`, kept "for the out-of-the-box
# text" — and it was correct only while the engine's default instance name
# happened to equal the reference house's own. It is a line that NAMES THE
# HOUSE, so there is no such thing as its value outside a house: every caller
# has a theme, and `declined_msg(theme)` is one call. A constant that is right
# for exactly one deployment is a trap wearing the shape of a convenience.


# What a browser will accept in an <link rel="icon" href="data:...">, by suffix.
# Deliberately a small allowlist: an operator pointing this at a .txt would
# otherwise ship a data: URL no browser can draw.
FAVICON_TYPES = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".gif": "image/gif",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
# A ceiling, because this rides a WebSocket frame to every web session on every
# connect. A favicon is a handful of kilobytes; anything past this is a mistake
# worth naming rather than silently streaming.
FAVICON_MAX_BYTES = 64 * 1024


def _load_favicon(config, rel: str, report=None) -> str:
    """The house favicon as a `data:` URL, or "" for none.

    Read once at startup, like the greeting and the help pages, and NEVER fatal:
    a missing, oversized, unreadable or unknown-typed file logs one fact and
    leaves the tab with its blank icon. Facts only — the path and the reason,
    never the bytes.
    """
    if not rel:
        if report is not None:
            report.fact("favicon: none set")
        return ""
    path = config.under_house(rel)
    suffix = path.suffix.lower()
    mime = FAVICON_TYPES.get(suffix)
    if mime is None:
        log.warning("favicon: unsupported type %r (%s) — using the blank icon",
                    suffix, path)
        return ""
    try:
        raw = path.read_bytes()
    except OSError:
        log.warning("favicon: could not read %s — using the blank icon", path)
        return ""
    if not raw:
        log.warning("favicon: %s is empty — using the blank icon", path)
        return ""
    if len(raw) > FAVICON_MAX_BYTES:
        log.warning("favicon: %s is %d bytes (max %d) — using the blank icon",
                    path, len(raw), FAVICON_MAX_BYTES)
        return ""
    url = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
    log.info("favicon: %d bytes loaded from %s", len(raw), path)   # a fact
    if report is not None:
        report.fact(f"favicon: {len(raw)} bytes")
    return url


def _load_help_topics(path) -> dict[str, str]:
    """Load the ``.help <topic>`` pages from the help/ directory, once at startup.

    One ``<topic>.txt`` per page; the stem, sanitized the same way a typed topic
    is, becomes the topic name — so dropping a new file in help/ adds a topic
    with no code change. A missing directory is never fatal (unlike
    agreement.txt): there are simply no topics, and .help says so.
    """
    d = Path(path)
    if not d.is_dir():
        return {}
    topics = {}
    for f in sorted(d.glob("*.txt")):
        name = commands.help_topic_key(f.stem)
        if not name:
            continue
        try:
            topics[name] = f.read_text(encoding="utf-8").rstrip("\n")
        except OSError:
            log.warning("could not read help page: %s", f.name)   # a fact, not text
    return topics


class _SessionSink:
    """Adapts a Session to the reboot player's raw-frame sink.

    The player emits already-formed frames (visible lines, BELs, ``\\x01sound``
    control frames); we write them straight to the transport, bypassing
    ``Session.send()`` — both its wrapping/decoration and its reboot-suppression
    gate, which is exactly what lets the boot theater's own output through while
    everything else to this screen is dropped.
    """

    def __init__(self, session):
        self._s = session

    def write(self, text: str) -> None:
        self._s.transport.send_text(text)

    async def flush(self) -> None:
        await self._s.drain()


def _is_loopback(ip: str) -> bool:
    """True only for a real loopback peer (127.0.0.0/8, ::1).

    An unknown/absent address ("?") is treated as NOT loopback, so the arch-name
    reservation fails safe (refuses) rather than open.
    """
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def _valid_ip(s: str) -> bool:
    """True if ``s`` parses as an IP address — used to validate an X-Forwarded-For
    value before it becomes a ban / rate-limit key."""
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


# The built-in banner, used only when greeting.txt is absent. The koan line now
# lives inside greeting.txt (the shipped file ends with it); this fallback keeps
# a server with no greeting file looking exactly as it did before greeting.txt
# existed.
BUILTIN_GREETING = "    . . . Heresay . . .\nYou had to be there."


def _load_echoes(path) -> list[str]:
    """The atmospheric lines for the entry room, one per line of world/echoes.txt.

    Operator content like the greeting: blank lines are dropped, a missing or
    empty file simply makes the feature silent (never fatal). Never logged.
    """
    p = Path(path)
    if not p.is_file():
        return []
    return [ln.strip() for ln in p.read_text(encoding="utf-8").split("\n") if ln.strip()]


def _load_greeting(path) -> str:
    """The login banner: greeting.txt verbatim, or the built-in two-liner.

    Operator-editable and loaded once at startup, like agreement.txt — but a
    missing file is NOT fatal here (unlike the agreement): the server simply
    shows the built-in banner. Trailing newlines are trimmed so send_raw treats
    it as one clean unit; every other byte, leading spaces included, is the
    operator's and is printed as written.
    """
    p = Path(path)
    if not p.exists():
        return BUILTIN_GREETING
    return p.read_text(encoding="utf-8").rstrip("\n")


class TalkerServer:
    def greeting_for(self, session) -> str:
        """The banner for this session's width — narrow art if the house drew
        some and the session is narrow, the ordinary banner otherwise."""
        if session.is_narrow and self.greeting_narrow:
            return self.greeting_narrow
        return self.greeting

    def agreement_for(self, session) -> str:
        """The house rules, laid out for this session's width.

        REFLOWED AT EVERY WIDTH since v1.29.3, where 78 used to be the file byte
        for byte. The reason is the placeholder: the authored line breaks were
        set around `{rev_window_minutes}` — twenty-one characters — and what
        renders there is "30", or "45", or "22.5". Every break in that paragraph
        was therefore computed for a string nobody ever reads, and keeping them
        would put a hole in the middle of clause 1 that changes size with the
        config. Line breaks the author could not have known are not authorship,
        so the engine sets the paragraph and the layout is right for any value.

        The clauses survive it because they are separated by blank lines, which
        is what `reflow` keeps; what they lose is the three-space hanging indent
        under each number. Same trade the phone already made in v1.29.1, now
        taken at both widths for one behaviour instead of two.

        ⛔ **Display-time only. The FILE is never touched.** The substitution
        happens once at startup (`resolve_agreement`) and the hash is taken of
        that resolved text, so wrapping is the only thing that happens here —
        and wrapping cannot move the version. A test hashes across both widths.

        ⚠ ONE function does the laying-out, and since v1.36.1 it has two callers:
        this (account creation and re-consent) and `.agreement`, which lets
        anyone re-read the terms without being asked to agree to anything. They
        must be byte-identical — showing somebody a differently-set copy of what
        they signed would be a small lie — so the rendering lives in
        `commands.render_agreement` and neither caller has its own.
        """
        return commands.render_agreement(self.agreement_text, session)

    # ⛔ THERE IS NO `refresh_agreement`, AND THAT ABSENCE IS THE FEATURE.
    #
    # v1.37.0 had one, and v1.40.0 deleted it. It re-resolved the text and
    # re-took the version while the process ran, which meant that from that
    # instant new logins consented to the NEW version while everyone already
    # in-world stayed on the OLD one — a MIXED-AGREEMENT STATE that drained only
    # as each person happened to relog. **You cannot have different people under
    # different agreements**; a consent record whose subject depends on when you
    # last reconnected is not a consent record.
    #
    # So `agreement_text` and `agreement_hash` are set ONCE, in `_build`, and
    # nothing writes them again. The reload watcher still notices an edit and
    # says "restart to apply" (reload.py); the restart is the cutover, because it
    # drops everyone and they come back to one version together.
    #
    # ⚠ Do not re-introduce it. If a future feature seems to need it, what it
    # actually needs is a reboot.

    def login_banner_for(self, session) -> str:
        """The banner, plus the engine's version, for the login screen.

        The version is read from the constant at every login, never written into
        `greeting.txt` — a number an operator has to remember to edit is a number
        that goes stale, and this one is the answer to "what am I connected to?"

        It rides the banner's OWN output unit rather than being sent after it, so
        it sits directly under `You had to be there.` with no blank between; the
        blank line before the name prompt is the prompt's, as it always was.
        `Heresay` here is the ENGINE's name and is deliberately not themed — the
        art above it has already said whose house this is.
        """
        return f"{self.greeting_for(session)}\nHeresay version {__version__}"

    def __init__(self, config: Config, clock=time.monotonic, config_path=None):
        # Where config.toml itself lives, so the live re-read has something to
        # re-read. `__main__` passes the path it loaded from; a test building a
        # Config by hand passes nothing, and the config source is then simply
        # not watched. Never guessed: guessing would make a stray file in the
        # CWD authoritative over a running house.
        self.config_path = str(config_path) if config_path else None
        self.version = __version__
        # Built FIRST, so its timestamp is the moment the load began, and so the
        # capture is in place before the first file is opened. `_build` is the
        # old __init__ body, unchanged and merely renamed: the whole of the load
        # happens inside `capturing()`, which is what makes the warning buffer
        # complete without a single loader knowing it exists. See loadreport.py.
        self.load_report = LoadReport()
        with self.load_report.capturing():
            self._build(config, clock)

    def _build(self, config: Config, clock) -> None:
        self.config = config
        self.clock = clock
        # ⛔ Refuse the boot on the values that must not fall back — a missing
        # owner_name, or a landing_level that is not a level / is `owner`. Done
        # first, before anything is stood up, so a misconfigured house fails
        # loudly rather than opening wrong. (The [ranks] misordered-anchor
        # refusal lives in load_theme, reached just below.)
        validate_config(config)
        world_dir = config.world_path()
        # entry_room/jail_room come from [world]; load_world validates them
        # against the rooms it just read and refuses to start on a mismatch.
        self.world: World = load_world(world_dir, config.entry_room, config.jail_room)
        # Facts, at startup, in the same spirit as the help-topics line: how many
        # rooms were read and from where, which room is the entry, and — the
        # operator's typo-visibility — every passage that is declared from one
        # side only. Room ids, never room text.
        log.info("world: %d room(s) loaded from %s; entry %s; jail %s",
                 len(self.world.rooms), world_dir, self.world.start_room,
                 self.world.jail_room or "none")
        # ...and the same facts into the in-memory report, structurally: the
        # count as a number and the passages as pairs, so the screen can lay
        # them out rather than re-reading the line above. Explicit here because
        # these are DATA; the warnings arrive through the handler.
        self.load_report.count(len(self.world.rooms), "room")
        self.load_report.fact(f"entry {self.world.start_room}, "
                              f"jail {self.world.jail_room or 'none'}")
        if self.world.one_way:
            log.info("one-way passages: %s",
                     ", ".join(f"{a} -> {b}" for a, b in self.world.one_way))
        self.load_report.note_one_way(self.world.one_way)
        # The entry room's fading memory and its atmospherics, tuned from config
        # and fed the operator's echo lines (world/echoes.txt; empty = silent).
        self.world.review_window = config.rev_window_minutes * 60.0
        self.world.review_max = config.rev_max_lines
        self.world.echo_min = config.echo_min_minutes * 60.0
        self.world.echo_max = config.echo_max_minutes * 60.0
        self.world.echoes = _load_echoes(world_dir / "echoes.txt")
        log.info("echoes: %d line(s) loaded from %s",
                 len(self.world.echoes), world_dir / "echoes.txt")   # count + path
        self.load_report.count(len(self.world.echoes), "echo", "echoes")
        self.greeting = _load_greeting(config.greeting_path())
        # greeting.narrow.txt if the house drew one, else "" — see greeting_for().
        # Silently optional, exactly like the narrow map.
        self.greeting_narrow = read_narrow(config.greeting_path())
        # Rank display names (theme.toml -> defaults if absent; never fatal).
        # ⚠ Loaded HERE, above the agreement, because the agreement quotes the
        # owner's rank word (`{owner}`, v1.31.1) — so the theme is an input to
        # the consent hash, not merely a display detail. It is still above the
        # AccountStore below, which needs the ladder to read stored ranks.
        self.theme = theme_mod.load_theme(config.theme_path())
        # ⛔ The theme's half of the unconfigured-install refusal, beside
        # validate_config's. A `[bracketed]` instance_name is the skeleton
        # theme.toml unedited; everything else in this file still falls back.
        # ⛔ ONE SOURCE for the house's name (v1.60.0): `config.toml`. The
        # theme carries the FIELD because eight readers ask the theme for it,
        # and this is the single place that fills it. `validate_config` has
        # already refused a `[bracketed]` value, so what lands here is a name
        # somebody chose. ⚠ `replace`, not assignment: `Theme` is frozen.
        self.theme = dataclasses.replace(
            self.theme, instance_name=config.instance_name)
        # Resolved ONCE, here, and it is the resolved text everything downstream
        # uses — the hash below and every render. The order is the guarantee:
        # substitute, then hash, then wrap. Nothing sees the raw token.
        self.agreement_text = resolve_agreement(
            _load_agreement(config.agreement_path()), config, self.theme
        )
        # Consent is tied to the text: this is the version everyone must have
        # accepted. An account carrying a different (or absent) hash re-consents
        # at its next login — absent covers every account written before this.
        # Because the text is resolved first, a change to a config value the
        # agreement quotes re-consents exactly as an edit to the prose does.
        self.agreement_hash = agreement_version(self.agreement_text)
        # The .reboot boot script: loaded once, parsed once, never fatal if absent
        # (unlike agreement.txt). None -> .reboot says "Nothing happens."
        reboot_text = reboot.load_script(config.reboot_path())
        self.reboot_steps = (
            reboot.parse(reboot_text, width=config.wrap_columns)
            if reboot_text is not None else None
        )
        # Parsed a SECOND time at the narrow width. The parse pre-wraps every
        # line, so one parse cannot serve both widths — and a boot sequence
        # replayed at 78 on a phone is the most visibly broken thing narrow mode
        # could ship with. Two small lists at startup, picked per session.
        self.reboot_steps_narrow = (
            reboot.parse(reboot_text, width=session_mod.NARROW_COLUMNS)
            if reboot_text is not None else None
        )
        # The house's tab icon, read ONCE and held as a data: URL so the browser
        # never fetches anything (the zero-external-requests rule). Unset or
        # unreadable -> empty, which the client renders as the blank icon.
        self.favicon_url = _load_favicon(config, self.theme.favicon,
                                         self.load_report)
        # .help <topic> pages, loaded once (operator-editable; never fatal).
        help_dir = config.help_path()
        self.help_topics = _load_help_topics(help_dir)
        # A fact, at startup: a count and a path, never a page's content. This
        # is the line that would have caught the topics being silently empty in
        # production for a week.
        log.info("help topics: %d loaded from %s", len(self.help_topics), help_dir)
        self.load_report.count(len(self.help_topics), "help topic")
        # The ladder rides in so the store can migrate `level` -> `rank` and
        # spot ranks it no longer knows. The theme is loaded ABOVE this line —
        # further above than it used to be, since v1.31.1 made it an input to
        # the agreement hash as well.
        self.accounts = AccountStore(config.resolve(config.accounts_file),
                                     ladder=self.theme,
                                     owner_name=config.owner_name,
                                     landing_level=config.landing_level)
        # The reorg collapse (v1.38.0): the ladder's SHAPE changed since these
        # ranks were last placed, so everyone was moved to the bottom of their
        # zone and is re-promoted deliberately. Counts, never names — and said
        # loudly, because it is the one migration that DEMOTES people.
        if self.accounts.reorg_ran:
            log.warning("ranks reorganised: %d account(s) collapsed to their "
                        "zone anchors (newb %r, floor %r, staff %r) — re-promote "
                        "as needed", self.accounts.reorg_collapsed,
                        self.theme.newb_rank, self.theme.resident_rank,
                        self.theme.staff_rank)
            self.load_report.fact(
                f"ranks reorganised: {self.accounts.reorg_collapsed} account(s) "
                f"collapsed to their zone anchors")
        # A rank RENAME in theme.toml reads every holder of the old word as a
        # floor resident. That is the fail-safe direction and it stays — but it
        # must never be silent, so say the count once. Facts only: a number and
        # the ladder, never a name.
        if self.accounts.unknown_ranks:
            log.warning("%d account(s) hold a rank that is not in [ranks] order "
                        "%s and were read as %r — check theme.toml",
                        self.accounts.unknown_ranks, list(self.theme.order),
                        self.theme.resident_rank)
        # ⛔ A rank name and an account name may never be the same word. A rank
        # word IS an identity here (".call_staff" means the rank; the letterbox is
        # addressed to the rank), so a house running with both is the very
        # collision the reserved-name rule exists to prevent — and unlike a typo
        # in a words file, it cannot be resolved by falling back to a default.
        # This is the ONE deliberate exception to "theme.toml is never fatal".
        for rank in self.theme.order:
            existing = self.accounts.get(rank)
            if existing is not None:
                raise ValueError(
                    f'Cannot start: rank "{rank}" collides with the account '
                    f'"{existing.name}". .deleteuser the account or rename the '
                    f'rank in theme.toml.')
        self.bans = BanStore(config.resolve(config.bans_file))
        # Resume tokens ("remember me"): opaque, hashed, web-only, revocable.
        # Auth data — never content. See resume.py for the covenant note.
        self.resume = resume_mod.ResumeStore(config.resume_path())
        # THE revocation hook. Every password change — the owner's `.passwd`, the
        # arch's `.resetpassword`, and the recovery-code reset inside login —
        # funnels through AccountStore, so wiring it once here is what makes
        # "changing your password signs out every device" true on all three
        # paths rather than on the two somebody remembered.
        self.accounts.on_password_change = self.resume.revoke_account
        # The command table is seeded from the code registry and then owned by
        # state.json. Seed and ladder are passed IN for the same reason the
        # account ladder is: `commands` imports `session`, which imports
        # `state`, so state must not import the registry.
        self.state = StateStore(config.resolve(config.state_file),
                                command_seed=commands.command_seed(),
                                ladder=self.theme)
        # ---- who may run what: permissions.toml (v1.37.0) -----------------
        #
        # The table moved out of state.json — config the operator edits must not
        # share a file with state the daemon rewrites. `permissions.load` does
        # all three cases once each: read an existing file (self-completing and
        # appending what it added), MIGRATE from the state.json block if there is
        # one and no file yet (so an operator's assignments survive the move
        # exactly), or write a fresh file from the registry seed.
        #
        # A file with no safe reading is FATAL here, like a broken world — at
        # boot the honest answer is to stop and say so. At runtime the same error
        # merely rejects the reload and keeps the running table; see reload.py.
        perms_path = config.permissions_path()
        # ⚠ The ladder makes the read STRICT (v1.38.0): a value that is neither
        # an anchor nor a rank in `[ranks] order` raises here, and the house does
        # not start. A permissions typo is an error to fix — its symptom is a
        # command silently out of reach of the people it was given to, which the
        # invisible-gate design guarantees nobody notices.
        perms = permissions.load(perms_path, commands.command_seed(),
                                 legacy=self.state.commands, ladder=self.theme)
        self.state.set_command_table(perms.table)
        # ⛔ Once the FILE exists it is the source, so state.json stops carrying
        # the legacy block — the first save after this drops it. While there is
        # no file, the block is written back untouched: it is the only copy of
        # an upgrading house's assignments until `--write-permissions` has run,
        # and dropping it early would lose them.
        self.state.drop_command_block = perms.from_file
        log.info("permissions: %d command(s) %s",
                 len(perms.table),
                 f"from {perms_path}" if perms.from_file
                 else "from the built-in defaults (no permissions.toml)")
        self.load_report.count(len(perms.table), "permission")
        if not perms.from_file:
            self.load_report.fact("permissions: built-in defaults "
                                  "(no permissions.toml — see --write-permissions)")
        if perms.added and perms.from_file:
            # A fact worth surfacing: these are complete IN MEMORY, and the file
            # gains them the next time root runs `--write-permissions`. The
            # daemon does not write config — see permissions.py.
            log.info("permissions: %d not in the file, using their defaults: %s",
                     len(perms.added), ", ".join(sorted(perms.added)))
            self.load_report.fact(
                f"permissions: {len(perms.added)} using built-in defaults "
                f"(run --write-permissions to add them to the file)")
        for name in perms.skipped:
            log.warning("%s: no command called %r — line ignored",
                        perms_path, name)
        # A table value that resolves to no rank at all — a rank renamed out from
        # under it, or a typo. Each such command is locked to arch until it is
        # fixed; the house still opens, and this is the line that says so.
        if self.state.unresolvable_commands:
            log.warning("%d command(s) in %s name a rank that is not in "
                        "[ranks] order %s and are locked to %r until fixed",
                        self.state.unresolvable_commands, perms_path,
                        list(self.theme.order), self.theme.owner_rank)
        # ---- names the operator will not host: forbidden_names.txt (v1.44.0) --
        #
        # Their list, beside permissions.toml and read the same way: config they
        # edit, never state the daemon writes, absent means empty. ⛔ ADDITIVE —
        # `reserved_names` is this ∪ every rank, and nothing here can unreserve a
        # rank. Never fatal: a blocklist is not a reason a house cannot open.
        self.forbidden_names = forbidden.load(config.forbidden_names_path())
        self._report_forbidden(self.forbidden_names, at_boot=True)
        self.churn = ConnectionChurn(
            max_concurrent=config.max_conns_per_ip,
            window=config.churn_window_seconds,
            max_in_window=config.churn_max_in_window,
        )
        self.auth_backoff = AuthBackoff(
            base=config.auth_backoff_base, cap=config.auth_backoff_cap
        )
        # Per-IP account-registration flood limit (bounds accounts.json growth).
        self.reg_limiter = PerIPRateLimiter(
            config.registration_burst, config.registration_refill_per_sec
        )
        # Server-wide auth-failure ceiling (outer layer vs distributed guessing).
        self.global_ceiling = GlobalAuthCeiling(
            window=config.global_auth_fail_window_seconds,
            threshold=config.global_auth_fail_threshold,
            cooldown=config.global_auth_cooldown_seconds,
            enabled=config.global_auth_enabled,
        )
        self.mail = MailStore(
            config.resolve(config.mail_file),
            config.resolve(config.mail_key_file),
            cap=config.mailbox_cap,
            purge_days=config.mail_purge_days,
            # ⚠ The report box ages on its OWN number (v1.50.0) — one store, one
            # key, one purge loop, two retentions. It has to be its own value
            # because expiry is the ONLY way a report leaves: there is no delete
            # verb, so this is a report's whole lifetime rather than a backstop.
            box_purge_days={commands.REPORT_BOX: config.report_retention_days},
            # ⛔ …and its own CAP since v1.58.0. Fifty was `mailbox_cap`'s number
            # inherited by accident, and it could wedge the report channel shut.
            box_caps={commands.REPORT_BOX: config.report_box_cap},
        )
        # The .revtell buffers: the same key, the same discipline, its own file.
        self.revtells = RevTellStore(
            config.revtell_path(),
            config.resolve(config.mail_key_file),
            cap=config.revtell_count,
            purge_days=config.mail_purge_days,
        )
        # The operational event log — every DOMAIN fact the engine records. The
        # constructor trims to the retention window, so the startup pass is just
        # building it. journald keeps process output only; nothing is in both.
        self.events = EventStore(
            config.events_path(),
            retention_days=config.event_retention_days,
        )
        # Every ban ever placed — outlives the ban, so recidivism survives its
        # expiry. Default retention is FOREVER (see banhistory.py).
        self.ban_history = BanHistoryStore(
            config.ban_history_path(),
            retention_days=config.ban_history_days,
        )
        self._servers: list[asyncio.base_events.Server] = []
        self._ws_server = None
        self._purge_task: Optional[asyncio.Task] = None
        self._echo_task: Optional[asyncio.Task] = None
        self._reload_task: Optional[asyncio.Task] = None
        # The local control channel and the flag it sets to bring the house
        # down. The Event is made HERE rather than in start() so a test can
        # build a server, drive a shutdown and assert on it without a loop.
        self.control = None
        self.stop_requested = asyncio.Event()
        # The live-reload mechanism, built HERE rather than in start(): it needs
        # no event loop, only the stores above, and building it with the server
        # means `check_once()` is available to anything holding a server —
        # which is how the tests drive it. start() adds only the clock.
        self.reloader = reload_mod.HouseReloader(self)

    # ---- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        # The listeners are part of the load as far as anyone reading a report is
        # concerned — "did TLS come up?" is the same kind of question as "how
        # many rooms?" — so the capture is re-attached for this phase too. Same
        # split as __init__: the body is `_start`, and the report wraps it.
        with self.load_report.capturing():
            await self._start()

    async def _start(self) -> None:
        cfg = self.config

        # Plaintext listener — 127.0.0.1 only, for dev/tests. Never public.
        # This is the ONLY door that may set is_local_console (the operator's
        # local console), which is what gates arch-name registration.
        plain = await asyncio.start_server(
            functools.partial(self._on_connect, is_console=True),
            cfg.plain_host, cfg.plain_port,
        )
        self._servers.append(plain)
        log.info("plaintext listener on %s:%d", cfg.plain_host, cfg.plain_port)
        # The report names WHICH doors opened, never where they are bound: a
        # bind address is config an operator can read in config.toml, and the
        # house's own screens do not print addresses (the v1.33.0 rule).
        self.load_report.door("plaintext")

        # TLS listener.
        ssl_ctx = self._make_ssl_context()
        if ssl_ctx is not None:
            tls = await asyncio.start_server(
                self._on_connect, cfg.tls_host, cfg.tls_port, ssl=ssl_ctx
            )
            self._servers.append(tls)
            log.info("TLS listener on %s:%d", cfg.tls_host, cfg.tls_port)
            self.load_report.door("TLS")
        else:
            log.warning(
                "TLS listener NOT started: cert/key missing (%s / %s). "
                "Run 'make cert' to generate a dev certificate.",
                cfg.cert_file, cfg.key_file,
            )

        # WebSocket listener — the browser door. Bound to localhost only; in
        # production Caddy terminates TLS at wss://.../ws and proxies here.
        # The web door's dead-peer detection is the `websockets` library's own
        # ping/pong, active by default (ping_interval=20s, ping_timeout=20s): a
        # ghost browser is dropped in ~20-40s, its handler returns, and the normal
        # disconnect → 60s grace path clears it. TCP keepalives below are the
        # equivalent safety net for the socket doors, which have no such heartbeat.
        #
        # ⛔ `max_size` bounds the WIRE (v1.49.0, audit A2). The library default is
        # 1 MiB, while the engine reads at most `MAX_LINE_LENGTH` (1024) off any
        # frame — so ~1000x was being received, decompressed, buffered and then
        # thrown away, per frame, for as long as a peer cared to keep sending.
        # WS_MAX_FRAME makes the wire bound match what is actually read.
        #
        # ⚠ 8 KB rather than 1 KB, and the headroom is deliberate: the cap is a
        # BYTE count while `MAX_LINE_LENGTH` is a CHARACTER count, so 1024
        # characters of four-byte UTF-8 is 4096 bytes on the wire and must still
        # fit. It also leaves room for a future client control frame.
        #
        # ⚠ An over-size frame is a protocol violation: the library closes the
        # connection with 1009, it does not truncate. That is why the CLIENT
        # truncates first (`MAX_SEND` in web/index.html) — a person pasting a
        # long message is well-behaved, and before that cap existed a 60 KB paste
        # would have dropped them out of the world where today it is simply
        # trimmed. Nothing our own client sends can reach this bound; it exists
        # for everything that is not our own client.
        self._ws_server = await ws_serve(self._on_ws, cfg.ws_host, cfg.ws_port,
                                         max_size=WS_MAX_FRAME)
        log.info("WebSocket listener on %s:%d", cfg.ws_host, cfg.ws_port)
        self.load_report.door("web")

        # Owner-account safety: warn loudly if the configured owner has no
        # account, and reserve its name against remote registration until it is
        # created. (owner_name being SET is enforced earlier, in validate_config;
        # this is the softer "set, but not yet registered" case.)
        if self.accounts.get(cfg.owner_name) is None:
            log.warning(
                "owner account %r does not exist; its name may only be registered "
                "from the localhost listener until created.", cfg.owner_name
            )

        # Purge stale mail and expired bans at startup, then once per day.
        removed = self.mail.purge()
        if removed:
            log.info("purged %d mail message(s) older than %d days",
                     removed, cfg.mail_purge_days)
        stale = self.revtells.purge()
        if stale:
            log.info("purged %d review line(s) older than %d days",
                     stale, cfg.mail_purge_days)
        # ⚠ The capture files on their OWN much longer clock (v1.58.0) — they
        # are the record, and the in-system copy is not. A count and a number of
        # days, never a filename: a capture's name carries the filing time, and
        # a log line naming one would say that a report was filed at a moment
        # the log is otherwise careful not to record.
        gone = capture.prune(cfg.report_capture_path(), cfg.report_capture_days)
        if gone:
            # ⚠ "purged", matching its two siblings above rather than the
            # function's own name: these three lines are one family in the log an
            # operator reads, and `test_event_routing`'s process-output sweep
            # allows the phrase precisely because that family is counts and days
            # and nothing about anybody.
            log.info("purged %d report capture(s) older than %d days",
                     gone, cfg.report_capture_days)
        self.bans.purge_expired()
        # ---- the local control channel (v1.46.0) --------------------------
        #
        # ⛔ A UNIX socket in the state dir, mode 0600 — never a port. Only the
        # engine holds the connections, so a warning before a restart has to be
        # broadcast BY it; the trigger comes from outside, by somebody who is on
        # the box. Never fatal if it cannot bind. See control.py.
        self.control = control_mod.ControlServer(
            self, cfg.control_socket_path())
        await self.control.start()
        self._purge_task = asyncio.create_task(self._purge_loop())
        # The atmospherics tick: only meaningful if there are echo lines to say.
        if self.world.echoes:
            self._echo_task = asyncio.create_task(self._echo_loop())
        # The config watch (v1.37.0): stat() the watched files every few seconds
        # and reload what changed. Built even when the poll is off, so a test can
        # drive `check_once()` directly — it is the whole mechanism, and the loop
        # is only a clock around it. ⚠ state.json is deliberately NOT watched:
        # the daemon rewrites it, so watching it would be a feedback loop.
        if cfg.reload_poll_seconds > 0:
            self._reload_task = asyncio.create_task(
                self.reloader.loop(cfg.reload_poll_seconds))
            # A count + the watch roots, not the ~100 individual paths: enough to
            # confirm the watch is alive without a screenful of noise. The house
            # content collapses to one root; the config-side files (config.toml,
            # permissions.toml, forbidden_names.txt) are named, since there are a
            # handful and which ones are watched is worth seeing.
            watched = [Path(p) for src in self.reloader.sources
                       for p in src.paths(self) if p is not None]
            house = cfg.house_path()
            in_house = sum(1 for p in watched if p.is_relative_to(house))
            others = sorted({p.name for p in watched
                             if not p.is_relative_to(house)})
            roots = ([f"{in_house} under {house.name}/"] if in_house else []) + others
            log.info("config watch: every %gs, %d file(s): %s",
                     cfg.reload_poll_seconds, len(watched), ", ".join(roots))

    # How often the atmospherics tick wakes to check the quiet clock. The echo
    # intervals are minutes, so a coarse tick is plenty — and it does nothing but
    # look at a clock unless the entry room is occupied and has gone quiet.
    ECHO_TICK_SECONDS = 20.0

    async def _echo_loop(self) -> None:
        """Wake periodically; if the entry room has been quiet long enough with
        someone in it, let it murmur one line. All the logic (occupancy, the
        quiet gate, no-immediate-repeat, recording into .rev) is in the World;
        here we only broadcast what it hands back."""
        try:
            while True:
                await asyncio.sleep(self.ECHO_TICK_SECONDS)
                line = self.world.maybe_echo(self.clock())
                if line is not None:
                    # The room speaking, not system machinery: no speaker (so no
                    # mute hides it) and not dimmed. A person's own `.ignore
                    # atmospherics` still hides it — that is a choice about
                    # noise, not about who is speaking.
                    self.world.broadcast_room(
                        self.world.start_room, None, line, dim=False,
                        category="atmospherics")
        except asyncio.CancelledError:
            return

    async def _purge_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(24 * 3600)
                removed = self.mail.purge()
                if removed:
                    log.info("purged %d stale mail message(s)", removed)
                # Backstop for review buffers nobody has touched since. A count,
                # never a line.
                stale = self.revtells.purge()
                if stale:
                    log.info("purged %d stale review line(s)", stale)
                gone = capture.prune(self.config.report_capture_path(),
                                     self.config.report_capture_days)
                if gone:
                    log.info("purged %d stale report capture(s)", gone)
                self.bans.purge_expired()
                # Lapsed resume tokens. A count, never a hash — the log has no
                # business knowing which devices these were.
                dead = self.resume.purge_expired()
                if dead:
                    log.info("purged %d expired resume token(s)", dead)
        except asyncio.CancelledError:
            return

    def announce(self, text: str) -> int:
        """⚡ THE PRIMITIVE: one line to every connected session, now.

        The same delivery path the presence lines take, so it reaches everyone
        on every door and wraps like anything else. A system notice, so it is
        dimmed and has no speaker — nobody's `.mute` can hide it — and
        `always=True` so no `.ignore` category can either: this is the house
        telling people it is about to go away, which is not noise anyone opted
        out of.

        Returns how many sessions it reached, which is what the CLI prints back.

        ⛔ The text is NEVER written down. Same rule as speech; see control.py.
        """
        n = sum(1 for r in self.world.all_connected() if r.session is not None)
        self.world.broadcast_all(None, text, dim=True, always=True)
        return n

    def request_stop(self) -> None:
        """Ask the main loop to close the house and exit 0.

        ⚠ Exit ZERO is the whole of "stays down": the unit's `Restart=on-failure`
        (v1.46.0, was `always`) leaves a clean exit alone. Under `always` a timed
        shutdown could not have worked at all — systemd would have brought the
        house straight back.
        """
        self.stop_requested.set()

    async def aclose(self) -> None:
        """Close every listener (used on shutdown).

        Waits are bounded so a stuck client connection can't hang shutdown —
        Python 3.12+ ``Server.wait_closed()`` blocks until live connections end.
        """
        self.accounts.flush()   # clean shutdown: persist coalesced preference writes
        if self.control is not None:
            await self.control.aclose()
        if self._purge_task is not None:
            self._purge_task.cancel()
        if self._echo_task is not None:
            self._echo_task.cancel()
        if self._reload_task is not None:
            self._reload_task.cancel()
        # Cancel any outstanding disconnect-grace timers so they don't outlive us.
        for res in list(self.world.residents.values()):
            if res.grace_task is not None:
                res.grace_task.cancel()
        for srv in self._servers:
            srv.close()
        if self._ws_server is not None:
            self._ws_server.close()
        for srv in self._servers:
            try:
                await asyncio.wait_for(srv.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._ws_server is not None:
            try:
                await asyncio.wait_for(self._ws_server.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

    def _make_ssl_context(self) -> Optional[ssl.SSLContext]:
        cert = self.config.resolve(self.config.cert_file)
        key = self.config.resolve(self.config.key_file)
        if not (Path(cert).exists() and Path(key).exists()):
            return None
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Refuse obsolete protocol versions rather than inheriting OpenSSL's
        # ambient default. TLS 1.2 is the floor (broad telnet-over-TLS / tt++
        # client compatibility); 1.3 is negotiated when both sides support it.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
        return ctx

    # ---- per-connection ---------------------------------------------

    async def _on_connect(self, reader, writer, *, is_console: bool = False):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        self._enable_keepalive(writer)
        # The local console flag is granted only on the plaintext listener AND
        # only to a genuine loopback peer — so even a misconfigured public bind
        # of the plain door can't hand a remote client console rights.
        console = is_console and _is_loopback(ip)
        transport = SocketTransport(reader, writer, ip, is_local_console=console)
        await self._serve(transport)

    def _enable_keepalive(self, writer) -> None:
        """Turn on TCP keepalives (with tuned knobs) for an accepted socket door
        connection — TLS and plaintext alike. A half-open peer with no FIN is then
        detected by the kernel in a few minutes; the socket closes, the normal
        disconnect path runs, and the 60s grace clears the ghost. Best-effort: an
        unsupported option on an exotic platform is simply skipped."""
        sock = writer.get_extra_info("socket")
        if sock is None:
            return
        cfg = self.config
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for opt_name, value in (
                ("TCP_KEEPIDLE", cfg.tcp_keepidle),
                ("TCP_KEEPINTVL", cfg.tcp_keepintvl),
                ("TCP_KEEPCNT", cfg.tcp_keepcnt),
            ):
                opt = getattr(socket, opt_name, None)
                if opt is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, value)
        except OSError:
            pass  # never let a socket-tuning hiccup refuse a connection

    async def _on_ws(self, connection):
        # Origin is intentionally NOT allowlisted. Auth is per-connection
        # credentials (no cookies, no ambient authority), so there is no CSRF
        # surface for an Origin check to protect; self-hosting operators serve
        # from their own domains; and non-browser WebSocket clients send no Origin
        # at all — an allowlist would reject legitimate clients for protection the
        # credential model plus per-IP rate limits already provide. Revisit under
        # Phase H (controlled multi-tenant domains).
        ip = self._ws_client_ip(connection)
        transport = WebSocketTransport(connection, ip)
        await self._serve(transport)

    def _ws_client_ip(self, connection) -> str:
        """Determine the client IP for a WS connection.

        Behind Caddy the socket peer is the proxy, so honor X-Forwarded-For — but
        only when the peer is the configured trusted proxy, and only the RIGHTMOST
        hop: that is the address the trusted proxy itself observed. The leftmost
        entries are client-supplied (Caddy appends rather than replaces), so a
        browser could forge them — taking [0] would let a client claim any IP,
        including 127.0.0.1. The chosen value is validated as a real IP before it
        becomes a ban / rate-limit key; anything malformed falls back to the peer.
        Default config trusts no proxy, so the socket peer is used as-is.
        """
        peer = connection.remote_address
        ip = peer[0] if peer else "?"
        trusted = self.config.trusted_proxy
        if trusted and ip == trusted:
            try:
                xff = connection.request.headers.get("X-Forwarded-For")
            except Exception:
                xff = None
            if xff:
                candidate = xff.split(",")[-1].strip()
                if _valid_ip(candidate):
                    return candidate
        return ip

    def _report_forbidden(self, names, at_boot: bool = False) -> int:
        """Say what the operator's list came to, and how many it lands on.

        ⛔ **A COUNT, never a name.** "N existing account(s) hold a now-forbidden
        name" is the fact an operator needs — the edit has landed on somebody —
        and *which* somebody is a thing to go and look up deliberately, not
        something a log line hands over on the way past. Same posture as
        `.security`'s six numbers.

        ⚠ And nobody is renamed or removed. This is the SOFT collision: the
        holder is grandfathered and only new registrations are blocked. The HARD
        rank↔account rule (which does refuse the boot) is a different rule about
        a different thing — see forbidden.py.
        """
        held = forbidden.grandfathered(names, self.accounts)
        if at_boot:
            log.info("forbidden names: %d from %s", len(names),
                     self.config.forbidden_names_path())
            self.load_report.count(len(names), "forbidden name")
        if held:
            log.warning("forbidden_names: %d existing account(s) hold a "
                        "now-forbidden name (grandfathered; nothing was "
                        "changed)", held)
            # ⚠ ONLY off the boot path (v1.47.0, audit F6). At boot the load is
            # running inside `LoadReport.capturing()`, which already files every
            # `log.warning` into the report — so adding one here printed the
            # same fact twice on `.load_report`. A live reload runs OUTSIDE that
            # capture, so there the explicit call is the only way it lands.
            if not at_boot:
                self.load_report.warn(
                    f"forbidden_names: {held} existing account(s) hold a "
                    f"now-forbidden name (grandfathered; nothing was changed)")
        return held

    LOOPBACK = ("127.0.0.1", "::1")

    def _ip_ban_exempt(self, ip: str) -> bool:
        """Is this address exempt from IP-ban enforcement?

        TWO exemptions, and they are belt and braces for the same failure: an
        operator IP-banning themselves out of their own house, with no way back
        in short of editing bans.json by hand on the box.

        1. **The loopback door.** 127.0.0.1 / ::1 is never IP-refused, full
           stop. It is the plaintext console — the operator standing at the
           machine — and it is the same door that exists so the arch account can
           be bootstrapped on a fresh box. A guaranteed local way in is worth
           more than the theoretical case of wanting to ban yourself locally.
        2. **The arch's own address**, live session first, else their most
           recent `login` inside the retention window. This is the one that
           matters remotely: an arch who bans the NAT they share with a
           troublemaker would otherwise lock themselves out from the far side of
           the internet.

        ⚠ The second is deliberately best-effort. If the arch has not been seen
        inside the window there is nothing to match on, and the loopback door is
        what remains — which is precisely why the first exemption is absolute
        rather than also conditional.
        """
        if ip in self.LOOPBACK:
            return True
        arch = self.config.owner_name
        res = self.world.get_resident(arch)
        if res is not None and res.session is not None and res.session.peer_ip == ip:
            return True
        return self.events.last_login_ip(arch) == ip

    async def _serve(self, transport) -> None:
        """Drive one connection through any door. The core is transport-blind."""
        ip = transport.peer_ip
        now = self.clock()

        # Banned IPs are refused before anything else — UNLESS the address is
        # exempt. See `_ip_ban_exempt`: an IP ban must never be able to shut the
        # arch out of their own house.
        if self.bans.is_banned_ip(ip) and not self._ip_ban_exempt(ip):
            self.events.record("refused", ip=ip, reason="ip_banned")
            try:
                transport.send_text("Connection refused: you are banned.\n")
                await transport.drain()
            except (ConnectionError, OSError, ConnectionClosed):
                pass
            transport.close()
            return

        allowed, reason = self.churn.try_open(ip, now)
        if not allowed:
            self.events.record("refused", ip=ip, reason="conn_cap")
            try:
                transport.send_text(f"Connection refused: {reason}.\n")
                await transport.drain()
            except (ConnectionError, OSError, ConnectionClosed):
                pass
            transport.close()
            return

        # A DOMAIN fact -> the event store, not journald. (Same for the close
        # below, the login timeout, auth failures and the staff actions: each
        # fact lives in exactly one place, and this is it.)
        self.events.record("connection", dir="open", ip=ip)
        session = Session(transport, self.world, self.accounts, self.config,
                          clock=self.clock, mail=self.mail, bans=self.bans,
                          state=self.state, resume=self.resume,
                          reboot_steps=self.reboot_steps,
                          reboot_steps_narrow=self.reboot_steps_narrow,
                          theme=self.theme, help_topics=self.help_topics,
                          revtells=self.revtells, events=self.events,
                          ban_history=self.ban_history,
                          load_report=self.load_report,
                          agreement_text=self.agreement_text,
                          favicon_url=self.favicon_url)
        intentional = False
        resident: Optional[Resident] = None
        try:
            # The pre-auth phase (name + password / create flow) has a hard
            # deadline: a slow-loris that connects and dribbles bytes at the login
            # prompt can't pin a coroutine + socket forever. An AUTHENTICATED
            # session is never wrapped in this — the deliberate no-idle-reaping
            # policy for present humans stays.
            #
            # It is an `asyncio.timeout` rather than a `wait_for` so the ONE step
            # inside that is not a prompt — reading the agreement — can suspend it
            # and run on its own clock (`_agreement_window`). The deadline is
            # handed to the session for the duration and taken back after.
            try:
                async with asyncio.timeout(self.config.auth_phase_seconds) as deadline:
                    session.auth_timeout = deadline
                    resident = await self._login(session, ip)
            except asyncio.TimeoutError:
                resident = None
                try:
                    session.send("Login timed out. Goodbye.")
                    await session.drain()
                except (ConnectionError, OSError, ConnectionClosed):
                    pass
                self.events.record("login_timeout", ip=ip)   # a fact, no user text
            finally:
                # The deadline dies with the phase it bounds: an authenticated
                # session holds no login clock, and nothing downstream can
                # reschedule one that has already fired.
                session.auth_timeout = None
            if resident is not None:
                intentional = await self._run_session(session, resident)
        except (ConnectionError, OSError, ConnectionClosed):
            pass  # ordinary network drop; no content is ever in scope here
        except Exception as exc:
            # Log the exception TYPE and stack frames only — never str(exc) or the
            # exception args, which could carry a user-supplied string. format_tb
            # renders code locations, not variable values.
            frames = "".join(traceback.format_tb(exc.__traceback__))
            log.error("session error from %s: %s\n%s", ip, type(exc).__name__, frames)
        finally:
            self.churn.close(ip)
            # ⛔ A door that hung up on a hostile peer says so HERE (v1.52.0).
            #
            # The transport signals with `(reason, sample)` and never logs — this
            # is where the fact becomes a record, because the events layer is the
            # component whose job that is, and because `_serve`'s finally is the
            # ONE place both phases pass through. A frame rejected at the name
            # prompt and one rejected mid-conversation take the same path out, so
            # they take the same path into the log.
            #
            # ⚠ `account` is present only if the session ever reached one. A
            # pre-auth rejection has no name to give, and that absence is itself
            # the signal the maintainer wants: somebody probing before they have logged in.
            rejected = getattr(transport, "rejected", None)
            if rejected:
                reason, sample = rejected
                facts = {"ip": ip, "reason": reason, "sample": sample}
                if resident is not None:
                    facts["account"] = resident.name
                # The store escapes and caps the sample; nothing raw is written.
                self.events.record("bad_frame", **facts)
            if resident is not None:
                self._detach(resident, session, intentional)
            self.accounts.flush()   # persist any coalesced .set/.desc/.profile writes
            transport.close()
            self.events.record("connection", dir="close", ip=ip)

    # ---- login flow --------------------------------------------------

    async def _login(self, session: Session, ip: str) -> Optional[Resident]:
        # The banner is the first thing written to a new connection, so a phone
        # would otherwise see one wide screen before anything could tell us it
        # was a phone. Bounded, best-effort, and a no-op on any door that cannot
        # report a width (which is every door but the browser).
        await session.await_client_hello()
        # A remembered browser logs in HERE, before a banner or a prompt is
        # painted — the whole point of "silently" is that nothing flashes past.
        # A rejected or absent token simply falls through to the ordinary door.
        resumed = await self._try_resume(session, ip)
        if resumed is not None:
            return resumed
        session.send_raw(self.login_banner_for(session))
        # One blank line of breathing room between the banner and the prompt —
        # always, whatever the greeting's contents. (_emit_unit drops an all-blank
        # unit, so the blank rides the first prompt; re-prompts don't repeat it.)
        prompt = "\nName (or 'quit'): "
        while not session._closing:
            name = await session.ask(prompt)
            prompt = "Name (or 'quit'): "
            if name is None:
                return None
            if not name:
                continue
            if name.lower() in ("quit", ".quit", "q"):
                session.send("Goodbye.")
                return None

            is_arch = name.casefold() == self.config.owner_name.casefold()

            # Private mode: logins closed. Everyone but the arch is turned away at
            # the name prompt — password never asked, existence never revealed. The
            # one exception is a grace-window reattach: that session never left.
            if not self.state.allow_logins and not is_arch:
                existing = self.world.get_resident(name)
                if existing is None or not existing.in_grace:
                    session.send(f"{self.theme.instance_name} is closed just now.")
                    continue

            if self.accounts.exists(name):
                resident = await self._login_existing(session, ip, name)
                if resident is not None:
                    return resident
                continue
            else:
                # Private mode: creation closed. An unknown name gets the same line
                # whether or not it's taken — no "Create it?" offer. The arch name
                # from the local console stays creatable (lockout-proofing).
                if not self.state.allow_new_accounts and not (is_arch and session.is_local_console):
                    session.send("New accounts aren't being created right now.")
                    continue
                resident = await self._create_account(session, name)
                if resident is not None:
                    return resident
                continue
        return None

    # ---- remember-me / resume tokens ---------------------------------

    def _logout_tokens(self, name: str) -> int:
        """Sign every remembered device out of ``name``'s account.

        The one place a deliberate logout revokes, reusing exactly what a
        password change reaches for. Safe for an account that was never
        remembered — it is simply zero.
        """
        return self.resume.revoke_account(name) if self.resume is not None else 0

    def _issue_resume(self, session, acct) -> None:
        """Give this browser a fresh token, if the account asked to be remembered.

        Three gates, all of which must pass, and the first two are structural:
        a store must exist, the door must be a browser (``send_control`` is a
        no-op elsewhere, but the token would still be minted and stored, so the
        check is here rather than left to the sink), and the ACCOUNT must have
        opted in. Nobody is remembered who did not say so.
        """
        if self.resume is None or acct is None:
            return
        if not getattr(session.transport, "supports_control_frames", False):
            return                       # telnet/TLS/console: never, by construction
        if not commands.remembered(acct):
            return
        session.send_control_resume(self.resume.issue(acct.name))

    async def _try_resume(self, session, ip: str) -> Optional[Resident]:
        """The silent login. Returns a Resident on success, else None.

        Everything a password login checks, this checks too — it is a LOGIN, not
        a reattach: bans, private mode, and the agreement all apply. The one
        thing it does differently is the already-connected case, where it takes
        over rather than asking (there is nobody at a prompt to answer, and the
        token proves it is the same person).

        A rejected token is answered with an empty push so the browser clears the
        dead one and asks for a password next time, and is fed to the ordinary
        auth-failure funnel so it is rate-limited and fail2ban-visible like any
        other bad credential. Never says WHY it failed.
        """
        token = session.take_pending_resume()
        if not token or self.resume is None:
            return None                  # no token presented: the ordinary door

        delay = self.auth_backoff.delay(ip)
        if delay > 0:
            await asyncio.sleep(delay)
        await self._auth_cooldown_gate()

        name = self.resume.resolve(token)
        if name is None:                 # unknown, malformed, expired or revoked
            self._record_auth_failure(ip, "resume")
            session.send_control_resume("")          # tell the browser to forget it
            return None
        # Rotate on use: the presented token dies here whatever happens next, so
        # a leaked one is good for at most a single reconnect.
        fresh = self.resume.rotate(token)
        if fresh is None:                # lost a race with a concurrent revoke
            self._record_auth_failure(ip, "resume")
            session.send_control_resume("")
            return None
        acct = self.accounts.get(name)
        if acct is None:
            # The account went away under the token (deleted between issue and
            # use). Drop the token and fall through to the ordinary door.
            self.resume.revoke_account(name)
            self._record_auth_failure(ip, "resume")
            session.send_control_resume("")
            return None
        # An account whose owner has since turned remember-me off must not be
        # resumable, even if a token outlived the change.
        if not commands.remembered(acct):
            self.resume.revoke_account(acct.name)
            session.send_control_resume("")
            return None
        # Private mode. A 30-day token resume is a LOGIN, not a grace reattach,
        # so `.logins off` closes this door too — otherwise the switch would not
        # actually close the house.
        if not self.state.allow_logins and \
                acct.name.casefold() != self.config.owner_name.casefold():
            session.send_control_resume("")
            session.send(f"{self.theme.instance_name} is closed just now.")
            return None

        self.auth_backoff.record_success(ip)
        self._record_login(acct.name, ip)
        resident = await self._enter(session, acct, silent=True)
        if resident is None:
            # Refused after auth (banned, or the agreement was declined). The
            # token is already rotated; revoke the new one too rather than leave
            # a live credential for an account that could not get in.
            self.resume.revoke(fresh)
            session.send_control_resume("")
            return None
        session.send_control_resume(fresh)           # the browser stores the new one
        # A resume is a kind of arrival, so it is a DIRECTION rather than a
        # type: a view asking "who has been connecting?" wants it in the list.
        self.events.record("connection", dir="resume", ip=ip)   # no name, no token
        return resident

    @staticmethod
    def _show_recovery(session, codes) -> None:
        """Display a fresh recovery-code set — once. (Codes go to the socket, never
        to the log.)"""
        lines = [
            "Copy these somewhere real. They will never be shown again. Any one",
            "of them can reset a forgotten password:",
            "",
        ]
        lines.extend(f"  {c}" for c in codes)
        # Framed like any block the server shows you — and this is the one block
        # a person genuinely cannot afford to scroll past.
        session.send_raw(commands.framed("Your recovery codes", "\n".join(lines),
                                         width=session.width))

    @asynccontextmanager
    async def _agreement_window(self, session):
        """Read the house rules on the agreement's clock, not the password prompt's.

        The pre-auth phase runs under one short hard deadline
        (`auth_phase_seconds`, 60) so a slowloris cannot hold a socket at the
        name prompt. Showing the agreement happens inside that phase and is the
        one part of it that is not a prompt at all: eight clauses of prose, on a
        phone, for someone who has never seen them. At sixty seconds a careful
        reader was dropped mid-clause — which is the worst possible moment to
        drop somebody, because the thing they were being careful about is the
        thing they are consenting to.

        So the credential clock STOPS here and the agreement runs on its own,
        equally **finite** budget (`agreement_seconds`, 300). Nothing is
        unbounded at any point: while this window is open the inner timeout is
        the only clock, and it always fires.

        On the way out the credential clock resumes with **exactly the time it
        had left**, never a fresh budget. That is what keeps this from being a
        way to buy time at the prompts, and it is what keeps the phase bounded
        even for someone who reaches the agreement more than once — reading is
        free, dawdling is not.

        Strictly more generous than the single deadline it replaces, at every
        point: nobody who completes a login today is dropped by this, because
        every clock either runs as it did or is paused.
        """
        loop = asyncio.get_running_loop()
        deadline = session.auth_timeout
        if deadline is None:
            # No credential clock to suspend (a unit session, or a path that
            # reaches the agreement outside `_login`). The agreement's own bound
            # still applies — it is the guarantee, not a decoration.
            async with asyncio.timeout(self.config.agreement_seconds):
                yield
            return
        when, started = deadline.when(), loop.time()
        deadline.reschedule(None)               # the credential clock stops
        try:
            async with asyncio.timeout(self.config.agreement_seconds):
                yield
        finally:
            # Push the old deadline forward by exactly what reading cost, which
            # restores the remaining time rather than granting any.
            deadline.reschedule(
                None if when is None else when + (loop.time() - started)
            )

    async def _reconsent(self, session, acct) -> bool:
        """If the agreement has changed since this account agreed, ask again.

        Runs after authentication and BEFORE anything else entry does — before
        arrival, before the takeover offer — so nobody is in-world under a
        version they haven't accepted. Returns True to continue the login.

        No exemptions: staff and the owner re-consent like anyone. There
        is no lockout risk in that, because declining only ends THIS connection;
        the account is untouched and they can reconnect and agree any time.

        NOT called on a grace reattach: that is resuming a live session, not a
        login. Self-takeover IS a fresh authenticated login, so it is called.
        """
        if acct.agreed_hash == self.agreement_hash:
            return True                     # already agreed to this exact text
        session.send(REVISED_NOTICE)
        # Re-reading the whole thing is exactly as slow as reading it the first
        # time, so this path gets the reading clock too.
        async with self._agreement_window(session):
            session.send(self.agreement_for(session))
            while True:
                ans = await session.ask(AGREE_PROMPT)
                if ans is None:
                    return False            # hung up at the prompt
                choice = ans.strip().lower()
                if choice in ("y", "yes"):
                    # Fresh timestamp + the version they just read.
                    self.accounts.record_agreement(acct.name, self.agreement_hash)
                    # ⛔ NO ACCOUNT NAME on this one, deliberately, and it is the
                    # only `account_security` event without it. The journald line
                    # this replaced was name-less (it carried the IP), and
                    # v1.30.1 briefly named the account "because accounts.json
                    # already does" — true, and beside the point: that file holds
                    # the CURRENT state of one consent, while a log accumulates a
                    # record of who was at the door and when. Those are different
                    # things to hold about a person, and the smaller one is
                    # enough. What the fact is FOR is "the revision is landing" —
                    # a count over time — and that needs no name.
                    #
                    # The other two events keep their `account`: a password reset
                    # or a fresh code set is a credential changing hands, and a
                    # record of that without the account it belongs to is no
                    # record at all.
                    self.events.record("account_security", ip=session.peer_ip,
                                       event="reconsent")
                    return True
                if choice in ("n", "no"):
                    session.send(declined_msg(self.theme))
                    # Flush before unwinding: the web door's close() cancels its
                    # sender task, so an un-drained goodbye can be lost.
                    await session.drain()
                    # Nothing changes on the account. Close this connection; they
                    # can come back and agree whenever they like.
                    session.request_disconnect("declined agreement")
                    return False
                # anything else: re-ask

    async def _enter(self, session, acct, silent: bool = False) -> Optional[Resident]:
        """Post-authentication entry, shared by password and recovery login: ban
        check, re-consent if the agreement moved, then arrive / reattach /
        self-takeover.

        The caller has already proven the password. If the account is live on
        another connection (a real second login, or a half-open ghost the server
        still believes is alive), we offer a self-takeover rather than refusing —
        an authenticated owner is never locked out by their own stuck session.
        """
        if self.bans.is_banned_name(acct.name):
            self.events.record("refused", ip=session.peer_ip,
                               reason="banned_name")
            session.send("That account is banned.")
            return None
        existing = self.world.get_resident(acct.name)
        # A grace reattach is a resumption, not a login — never interrupt it with
        # a prompt. Every other path through here re-consents first.
        resuming = existing is not None and existing.in_grace
        if not resuming and not await self._reconsent(session, acct):
            return None
        acct = self.accounts.get(acct.name) or acct   # re-read: the hash just moved
        if existing is not None and existing.connected and not existing.in_grace:
            # A resume has nobody standing at a prompt to answer y/n, and the
            # token already proved it is the same person — so it takes over.
            # For a phone that is nearly always the right answer anyway: the
            # "other session" is your own tab that the browser threw away.
            if silent:
                return self._takeover(session, existing, acct)
            ans = await session.ask(
                "You're already connected from elsewhere. "
                "Take over that session? (y/n) "
            )
            if ans is None:
                return None
            if ans.strip().lower() not in ("y", "yes"):
                # 'n' → back to the name prompt; the other session is left alone.
                session.send("Left the other session alone.")
                return None
            return self._takeover(session, existing, acct)
        if existing is not None and existing.in_grace:
            return self._reattach(session, existing, acct)
        return self._arrive(session, acct)

    def _takeover(self, session, res: Resident, acct) -> Resident:
        """The freshly-authenticated `session` displaces the account's live session
        and takes the resident's place in-world — same room, no departure/arrival
        churn (it's the same person; treat it exactly like a grace reattach, so
        nothing said while ghosted is replayed). The old transport gets a one-line
        notice and is closed, unwedging its loop. Logged as a fact: name + IPs."""
        old_session = res.session
        old_ip = old_session.peer_ip if old_session is not None else "?"
        # Re-point the resident at the NEW session first, so when the old session's
        # loop unwinds its _detach sees it no longer owns the resident and no-ops
        # (no grace hold, no departure broadcast, name never freed).
        res.session = session
        res.in_grace = False
        if res.grace_task is not None:      # defensive; a live ghost has none
            res.grace_task.cancel()
            res.grace_task = None
        res.set_rank(self._rank_for(acct), self.theme)
        res.last_input = self.clock()
        res.afk = False                     # taking over is being present again
        res.afk_message = ""
        session.resident = res
        self._load_prefs(session, acct)
        if old_session is not None and old_session is not session:
            old_session.displace("Your account has logged in from elsewhere.")
        # A fact: the account and the address it was displaced FROM. The new
        # session's own IP is already in the `connection` open that preceded it,
        # so what this adds is the one thing that record cannot show — where the
        # session that just lost the account had been sitting.
        self.events.record("takeover", account=res.name, from_ip=old_ip)
        session.send("You've taken over your other session.")
        commands.cmd_look(session, "")
        return res

    def _record_login(self, name: str, ip: str) -> None:
        """The account↔address tie, recorded on every successful auth.

        The ONE event that holds both a name and an address (see events.py). It
        exists so `.ban_ip` can answer "what address is this person on?" without
        the engine keeping a durable `last_ip` on the account — a permanent
        account↔address link, which is the thing the covenant declines. Bounded
        by the ordinary retention window like everything else in the store, so
        the tie lasts exactly as long as the operational record and no longer.

        Fires for password logins, recovery-code logins and silent token
        resumes alike: all three are somebody proving who they are from an
        address, which is the whole content of the fact.
        """
        acct = self.accounts.get(name)
        self.events.record("login", account=acct.name if acct else name, ip=ip)

    def _record_auth_failure(self, ip: str, kind: str) -> None:
        """The ONE place every failed auth flows through — a wrong password AND a
        bad recovery code alike: the per-IP backoff, the server-wide ceiling, and
        one fail2ban-parseable log line. ``kind`` is 'login' or 'recovery'."""
        now = self.clock()
        self.auth_backoff.record_failure(ip, now)
        if self.global_ceiling.note_failure(now):
            # ⚠ Inside the guard, which returns True exactly ONCE per trip — so
            # this is the transition, not the state, and a sustained run writes
            # one of these beside its thousands of `auth_fail`s.
            log.warning(
                "global auth-failure ceiling tripped (>=%d failures / %.0fs); new "
                "auth attempts slowed and account creation paused for %.0fs",
                self.config.global_auth_fail_threshold,
                self.config.global_auth_fail_window_seconds,
                self.config.global_auth_cooldown_seconds,
            )
            # ...and the same fact where every other notable act already goes.
            # The journal line stays for whoever is watching it during an
            # incident; this is a SECOND listener, not a relocation, and it is
            # what `.security` counts and an ops watcher keys on. No ip: the
            # ceiling is server-wide, so there is no peer it is about.
            self.events.record(
                "auth_ceiling", phase="tripped",
                threshold=self.config.global_auth_fail_threshold,
                window=self.config.global_auth_fail_window_seconds,
                cooldown=self.config.global_auth_cooldown_seconds,
            )
        # A domain fact -> the event store. This is ALSO the fail2ban key, so the
        # jail's filter reads events.jsonl rather than journald now; the line is
        # still one stable, greppable shape per failure. See deploy/install/README.
        self.events.record("auth_fail", ip=ip, phase=kind)

    async def _auth_cooldown_gate(self) -> None:
        """Run at the start of each NEW auth attempt: log the all-clear once when a
        cooldown lapses, and — while cooling — impose a small fixed delay so a
        distributed run is slowed for everyone. Authenticated sessions never reach
        this path, so they are untouched."""
        now = self.clock()
        if self.global_ceiling.note_cleared(now):
            # The other edge, and `note_cleared()` is the same once-only guard.
            # Nothing but the phase: how long it lasted is the difference between
            # two timestamps the store already has.
            log.warning("global auth-failure ceiling cleared; normal service resumed")
            self.events.record("auth_ceiling", phase="cleared")
        if self.global_ceiling.in_cooldown(now):
            await asyncio.sleep(AUTH_COOLDOWN_DELAY)

    async def _login_existing(self, session, ip, name) -> Optional[Resident]:
        delay = self.auth_backoff.delay(ip)
        if delay > 0:
            await asyncio.sleep(delay)
        await self._auth_cooldown_gate()
        pw = await session.ask("Password (or 'recover'): ", secret=True)
        if pw is None:
            return None
        # Forgotten password? The literal 'recover' switches to the code path.
        if pw.strip().lower() == "recover":
            return await self._login_recover(session, ip, name)
        if not await self.accounts.averify(name, pw):
            self._record_auth_failure(ip, "login")
            session.send("Wrong password.")
            return None
        self.auth_backoff.record_success(ip)
        self._record_login(name, ip)
        acct = self.accounts.get(name)
        resident = await self._enter(session, acct)
        if resident is not None:
            self._issue_resume(session, acct)
        return resident

    async def _login_recover(self, session, ip, name) -> Optional[Resident]:
        """Reset a forgotten password with a one-time recovery code."""
        delay = self.auth_backoff.delay(ip)
        if delay > 0:
            await asyncio.sleep(delay)
        await self._auth_cooldown_gate()
        code = await session.ask("Recovery code: ", secret=True)
        if code is None:
            return None
        # Generic failure for a bad/absent code — never reveal whether codes exist.
        idx = (await self.accounts.afind_recovery(name, code)) if code else -1
        if idx < 0:
            self._record_auth_failure(ip, "recovery")   # recovery is a first-class auth path
            session.send("That didn't work.")
            return None
        self.auth_backoff.record_success(ip)
        self._record_login(name, ip)

        new1 = await session.ask("New password: ", secret=True)
        if new1 is None:
            return None
        problem = _password_problem(new1)
        if problem:
            session.send(problem)
            return None
        new2 = await session.ask("New password again: ", secret=True)
        if new2 is None:
            return None
        if new1 != new2:
            session.send("Passwords didn't match.")
            return None

        await self.accounts.achange_password(name, new1)
        self.accounts.burn_recovery(name, idx)     # burn only the one used
        # The DISPLAY name if the account still resolves, else what was typed —
        # either way a name, never the code that was burned to get here.
        who = self.accounts.get(name)
        self.events.record("account_security",
                           account=who.name if who is not None else name,
                           event="password_reset_recovery")
        remaining = self.accounts.recovery_count(name)
        session.send(f"Password reset. {remaining} recovery code(s) remain.")
        if remaining <= 1:
            session.send("You're running low — use '.recovery new' once you're in.")
        # REVOKE-then-issue: the password just changed, so every device already
        # remembered is signed out (the AccountStore hook did that), and only
        # then does this browser get a new token. The order matters — issuing
        # first would hand out a token the revoke then took straight back.
        acct = self.accounts.get(name)
        resident = await self._enter(session, acct)
        if resident is not None:
            self._issue_resume(session, acct)
        return resident

    async def _create_account(self, session, name) -> Optional[Resident]:
        # Names nobody may register, from any door: every RANK on the ladder
        # (built in — a rank word is an identity here) plus whatever the operator
        # put in `forbidden_names.txt`. Unlike the arch name, which IS
        # loopback-creatable so the operator can bootstrap it.
        #
        # ⛔ The refusal says the same thing whichever half caught it, and never
        # says which or why. The list is the operator's business, and a message
        # that distinguished "a rank" from "on the blocklist" would let anyone
        # read the blocklist back one guess at a time.
        if name.casefold() in reserved_names(self.theme, self.forbidden_names):
            session.send("That name isn't available.")
            self.events.record("refused", ip=session.peer_ip,
                               reason="reserved_name")
            return None
        # Arch-name protection. We only reach here when no account exists for
        # this name, so if it IS the arch name, the arch account is missing:
        # allow registration ONLY from the local plaintext console (the operator
        # at the box), never from any network door — a loopback-looking peer IP is
        # forgeable behind the proxy, the console flag is not.
        if (name.casefold() == self.config.owner_name.casefold()
                and not session.is_local_console):
            # ⚠ Word for word the refusal above, deliberately: three different
            # reasons a name is unavailable, one sentence. Distinguishing them
            # would let anyone map the reservations one guess at a time. The
            # EVENT keeps them apart, which is where the distinction belongs.
            session.send("That name isn't available.")
            self.events.record("refused", ip=session.peer_ip,
                               reason="owner_name")
            return None
        if not valid_name(name):
            session.send(
                "Names are 2–16 chars, start with a letter, letters/digits/-/_ only."
            )
            return None
        # Per-IP registration flood limit — bounds how fast accounts.json grows
        # from one address. The local console (the operator) is exempt.
        if not session.is_local_console and not self.reg_limiter.allow(
                session.peer_ip, self.clock()):
            session.send("Accounts are being created too quickly from your "
                         "address — please try again shortly.")
            self.events.record("refused", ip=session.peer_ip,
                               reason="rate_limited")
            return None
        # New-account creation is paused while the server-wide auth-failure ceiling
        # is in cooldown (the local console is exempt, so the operator is never
        # locked out of bootstrapping).
        if not session.is_local_console and self.global_ceiling.in_cooldown(self.clock()):
            session.send("New accounts can't be created just now — please try "
                         "again shortly.")
            return None
        confirm = await session.ask(f"No one uses '{name}'. Create it? (yes/no) ")
        if confirm is None:
            return None
        if confirm.lower() not in ("y", "yes"):
            session.send("No account created.")
            return None
        pw1 = await session.ask("Choose a password: ", secret=True)
        if pw1 is None:
            return None
        problem = _password_problem(pw1)
        if problem:
            session.send(problem)
            return None
        pw2 = await session.ask("Confirm password: ", secret=True)
        if pw2 is None:
            return None
        if pw1 != pw2:
            session.send("Passwords didn't match.")
            return None

        # The house rules, then y/n. Nothing is written to disk until they agree.
        # On the reading clock: the credential prompts above were quick questions,
        # this is a document.
        async with self._agreement_window(session):
            session.send(self.agreement_for(session))
            while True:
                ans = await session.ask(AGREE_PROMPT)
                if ans is None:
                    return None
                choice = ans.strip().lower()
                if choice in ("y", "yes"):
                    break
                if choice in ("n", "no"):
                    session.send(declined_msg(self.theme))
                    return None
                # anything else: re-ask

        # Agreed: store the account (with the agreed timestamp), THEN codes.
        acct = await self.accounts.acreate(name, pw1, agreed=True,
                                    agreed_hash=self.agreement_hash)
        codes = recovery.generate_set()
        await self.accounts.aset_recovery(acct.name, codes)
        await self._ask_remember(session, acct)
        session.send(f"Welcome, {acct.name}. Type .help once you're in.")
        self._show_recovery(session, codes)
        resident = self._arrive(session, acct)
        if resident is not None:
            self._issue_resume(session, acct)
        return resident

    async def _ask_remember(self, session, acct) -> None:
        """Ask ONCE, at creation, whether to stay logged in on this device.

        This question is the consent. The agreement describes what the house
        keeps about everyone; a resume token is kept only for people who asked
        for one, so it is asked for here — plainly, in the one moment the person
        is already deciding how their account works — rather than by amending a
        document everyone would have to re-accept.

        Defaults to NO: silence, a blank line, or anything that is not a yes
        leaves it off. Only asked on a door that could actually use it, because
        offering it at a telnet prompt would be offering something that cannot
        happen.
        """
        if not getattr(session.transport, "supports_control_frames", False):
            return
        session.send(
            "One more thing. This browser can stay logged in, so coming back "
            "after a text message doesn't ask for your password again — even "
            "if the tab was closed. It stores a token, never your password, "
            "for 30 days. You can change this any time with .remember, and "
            ".help remember explains it fully."
        )
        ans = await session.ask("Stay logged in on this device? (y/N) ")
        if ans is not None and ans.strip().lower() in ("y", "yes"):
            self.accounts.set_setting(acct.name, commands.REMEMBER_SETTING, True)
            session.send("Right — this browser will remember you.")
        else:
            session.send("Left off. Turn it on later with '.remember on'.")

    # ---- presence transitions ---------------------------------------

    def _rank_for(self, acct) -> str:
        """This account's EFFECTIVE rank name.

        The arch owner is named in config and holds the top rank regardless of
        what is stored — unchanged from v1.26. Everyone else holds what they were
        granted, with anything the ladder does not know read as the floor (the
        fail-safe direction; `AccountStore` counts these and startup says so).
        """
        if acct.name.casefold() == self.config.owner_name.casefold():
            return self.theme.owner_rank
        return self.theme.rank(acct.rank)

    def _load_prefs(self, session, acct) -> None:
        # A stored "color" key from before the rename is simply not read: the
        # setting reverts to its default rather than migrating (the maintainer's call).
        #
        # The three display switches. Only what the resident has ACTUALLY chosen
        # is carried; everything else is absent, so it falls through to the live
        # house `.ownerset` value at render time (v1.25.0) — which is what
        # makes an owner's change reach a connected session with no relogin.
        # Exactly the rule the message prefs below have always used.
        switches = {}
        for key in commands.SWITCH_KEYS:
            value = acct.settings.get(key)
            if value is None:
                continue
            if key == commands.SET_HELPSTYLE:
                if value in commands.HELP_STYLES:
                    switches[key] = value
            else:
                switches[key] = bool(value)
        session.resident.switches = switches
        # The resident's own comings-and-goings wording. Only keys they have
        # actually set are carried; the rest fall through to the LIVE house
        # default (.ownerset), which is why nothing is mirrored here.
        session.resident.messages = {
            k: str(acct.settings[k])
            for k in commands.MSG_COMMANDS
            if isinstance(acct.settings.get(k), str) and acct.settings[k].strip()
        }
        # The .ignore filter. Only categories still recognised are carried, so a
        # retired category in an old account is dropped rather than lingering as
        # a filter nothing can turn off.
        stored = acct.settings.get(commands.IGNORE_SETTING)
        session.resident.ignores = (
            {k: True for k, v in stored.items()
             if v and k in commands.IGNORE_CATEGORIES}
            if isinstance(stored, dict) else {}
        )
        # The client-side ones have to be PUSHED — the browser is where they take
        # effect. Done here rather than in _arrive because all three ways into the
        # world (fresh login, grace reattach, self-takeover) come through this one
        # method, and each of them is a page that needs to start out correct.
        # A no-op on the socket doors.
        commands.push_client_settings(session)

    def _presence_broadcast(self, name: str, line: str, category: str) -> None:
        """A house-wide enter/leave line, to every occupied room (broadcast_scope
        'world', the default) or the entry room only ('entry'), never the subject.

        ``category`` is the `.ignore` category, so someone who has switched off
        logins or logouts does not receive it."""
        if self.state.house_setting("broadcast_scope") == "entry":
            self.world.broadcast_room(self.world.start_room, None, line,
                                      exclude=[name], category=category)
        else:
            self.world.broadcast_all(None, line, exclude=[name], category=category)

    def _arrive(self, session, acct) -> Resident:
        # A jailed resident lands in the jail room, confined until freed — NOT the
        # entry room. With no jail room defined, fall back to entry but KEEP the
        # flag (so re-adding a jail room later re-confines them).
        jailed = self.state.is_jailed(acct.name)
        room_id = (self.world.jail_room
                   if jailed and self.world.jail_room is not None
                   else self.world.start_room)
        res = Resident(name=acct.name, description=acct.description,
                       room_id=room_id, rank=self._rank_for(acct),
                       ladder=self.theme)
        res.jailed = jailed
        res.session = session
        res.last_input = self.clock()
        session.resident = res
        self._load_prefs(session, acct)
        self.world.add_resident(res)
        # House-wide login announcement, never to the arriving person.
        # Precedence: jailed -> the house jail_logon_msg phrase, rendered
        # [*** name phrase ***] (personal override ignored); else the resident's
        # own login_msg; else the house login_msg, rendered [name phrase]. A
        # legacy full-template value falls back to the default phrase.
        #
        # Both go out under the `login_msg` ignore category: a jailed login is
        # still a login, so someone who has switched logins off does not get the
        # jail ones either.
        if jailed:
            line = jail_presence_line(res.name,
                                      house_phrase(self.state, "jail_logon_msg"))
        else:
            phrase = (phrase_or_none((res.messages or {}).get("login_msg"))
                      or house_phrase(self.state, "login_msg"))
            line = presence_line(res.name, phrase)
        self._presence_broadcast(res.name, line, "login_msg")
        commands.cmd_look(session, "")
        # Mail notice at login ONLY — never mid-session, never a push/badge —
        # and only when something is UNREAD. A box you have already read is not
        # news, and repeating the line until you delete it is a nag.
        if self.mail.has_unread(res.name):
            session.send("You have mail.")
        # Staff mail: the shared staff letterbox, announced to staff at login only,
        # after their personal mail notice. Deliberately still gated on non-empty:
        # the box is SHARED, so "read" would have to be per-reader state that the
        # store does not hold. See CLAUDE.md's mail notes.
        if res.is_staff() and self.mail.has_mail(commands.STAFF_BOX):
            session.send(f"You have {self.theme.staff_word} mail.")
        # Reports: a third notice, its own line, for whoever the house lets read
        # them (v1.50.0). ⚠ UNREAD-gated like personal mail and unlike the
        # staff letterbox — a box you have already been through is not news,
        # and a line that repeats until you empty it would teach the reader to
        # want an empty box, which is precisely the instinct a report box must
        # not encourage. There is no delete verb, so "clear it by reading it" is
        # the only clearing there is.
        #
        # ⛔ Asked through `may_run`, the dispatcher's own gate — so this line
        # appears for exactly the people `.read_report` works for, however the
        # operator has set it.
        if commands.may_run("read_report", session):
            unread = self.mail.unread_count(commands.REPORT_BOX)
            if unread:
                session.send(f"You have {unread} unread report"
                             f"{'s' if unread != 1 else ''}.")
        return res

    def _reattach(self, session, res: Resident, acct) -> Resident:
        # Reconnected within the grace window: no exit/enter churn is shown.
        if res.grace_task is not None:
            res.grace_task.cancel()
            res.grace_task = None
        res.in_grace = False
        res.session = session
        res.set_rank(self._rank_for(acct), self.theme)
        res.last_input = self.clock()
        res.afk = False           # reconnecting is being back; AFK never persists
        res.afk_message = ""
        session.resident = res
        self._load_prefs(session, acct)
        session.send("Reconnected. (You missed anything said while you were gone.)")
        commands.cmd_look(session, "")
        return res

    def _announce_logout(self, res: Resident) -> None:
        """The one place a departure is announced house-wide, rendered [name
        phrase] — a clean .quit and a grace-window expiry say the same line. (A
        grace REATTACH and a self-takeover stay silent, so a quick reconnect makes
        no leave/enter spam.) Precedence: the resident's own logout_msg, else the
        house logout_msg — but a jailed resident uses the house phrase (personal
        override ignored, as on entry)."""
        personal = None if res.jailed else phrase_or_none((res.messages or {}).get("logout_msg"))
        phrase = personal or house_phrase(self.state, "logout_msg")
        self._presence_broadcast(res.name, presence_line(res.name, phrase),
                                 "logout_msg")

    def _detach(self, res: Resident, session: Session, intentional: bool) -> None:
        # Only detach if this session still owns the resident (guards against a
        # stale session firing after a reattach replaced it).
        if res.session is not session:
            return
        if intentional:
            self._announce_logout(res)
            self.world.remove_resident(res)
            self.world.room_emptied(res.room_id)
            return
        # Unexpected drop: hold presence for the grace window.
        res.session = None
        res.in_grace = True
        res.grace_task = asyncio.create_task(self._grace_expire(res))

    async def _grace_expire(self, res: Resident) -> None:
        try:
            await asyncio.sleep(self.config.disconnect_grace_seconds)
        except asyncio.CancelledError:
            return
        if res.in_grace and res.session is None:
            self._announce_logout(res)
            self.world.remove_resident(res)
            self.world.room_emptied(res.room_id)
            res.in_grace = False

    # ---- main loop ---------------------------------------------------

    async def _run_session(self, session: Session, res: Resident) -> bool:
        """Drive one attached session. Returns True if it left intentionally."""
        while not session._closing:
            if not session.in_editor():
                session.prompt()
                await session.drain()
            line = await session.read_line()
            if line is None:
                # A kicked/booted session set _closing before its read returned;
                # that's an intentional departure (free the name, no grace hold).
                return session._closing

            # AFK: any input other than (re)setting AFK brings you back — a quiet
            # confirmation to you alone; not announced to the room.
            if res.afk and commands.clears_afk(line):
                res.afk = False
                res.afk_message = ""
                session.send("You're back.")

            # While composing, input is buffered — never speech, never commands.
            # We deliberately do NOT touch last_input, so a writer looks idle.
            if session.in_editor():
                session.feed_editor(line)
                await session.drain()
                continue

            res.last_input = self.clock()
            result = commands.dispatch(session, line)
            await session.drain()
            if result == commands.QUIT:
                # `.quit` is a full ACCOUNT logout, not just a disconnect: it
                # signs out every remembered device, not merely this one.
                #
                # This branch is reached ONLY by a deliberate `.quit`. A boot, a
                # ban, a displaced self-takeover and a dropped socket all leave
                # through `line is None` above, so none of them revoke — which is
                # right: being kicked, or having your other tab displaced, is not
                # you asking to be logged out everywhere.
                #
                # The `.remember` SETTING is deliberately untouched. So a
                # remembered resident who quits types a password next time, and
                # that login issues a fresh token and remembers them again.
                # To stay logged in you close the tab; to stop being remembered
                # at all you use `.remember off`. Said plainly in help/remember.
                self._logout_tokens(res.name)
                session.send("Goodbye.")
                await session.drain()
                return True
            if result == commands.PASSWD:
                await self._change_password(session)
                await session.drain()
                continue
            if result == commands.RECOVERY_NEW:
                await self._recovery_new(session)
                await session.drain()
                continue
            if result == commands.LOGOUTALL:
                await self._logoutall(session)
                await session.drain()
                continue
            if result == commands.BAN_IP:
                await self._ban_ip(session)
                await session.drain()
                continue
            if result == commands.DESC_CONFIRM:
                # A bare `.desc` has shown them their own description; ask
                # before emptying it. Anything but an explicit yes keeps it —
                # including a bare Enter, which is the likeliest way somebody
                # arrives here by accident in the first place.
                ans = await session.ask("Clear it? (y/n) ")
                if ans is not None and ans.strip().lower() in ("y", "yes"):
                    commands.clear_description(session)
                else:
                    session.send("Kept.")
                await session.drain()
                continue
            if result == commands.DELETEME:
                if await self._deleteme(session):
                    return True   # account deleted -> intentional departure
                await session.drain()
                continue
            if result == commands.DELETEUSER:
                await self._deleteuser(session)
                await session.drain()
                continue
            if result == commands.RESETPASSWORD:
                await self._resetpassword(session)
                await session.drain()
                continue
            if result == commands.MAILPURGE:
                await self._mailpurge(session)
                await session.drain()
                continue
            if result == commands.REBOOT:
                if await self._reboot(session, res):
                    return session._closing   # disconnected mid-boot
                await session.drain()
                continue
            if session._closing:  # e.g. flood escalation
                if session._disconnect_reason == "flood":
                    self.events.record("flood_disconnect", ip=session.peer_ip)
                return True
        return True

    async def _reboot(self, session: Session, res: Resident) -> bool:
        """Play the boot theater on this session. Returns True if the peer
        disconnected mid-boot (the caller then unwinds the loop), else False.

        The command handler already spent the rate token. Here we first ask the
        fake confirmation — "Reboot server? (y/n)" — and read one line that we
        deliberately IGNORE: y, n, empty, gibberish all proceed (the user must
        believe their "n" didn't save them). Only once they've answered do we
        fire the room's one dry emote and begin. Then we run two tasks: the
        player, and a swallow task that reads-and-drops the invoker's own input
        so keystrokes during the boot don't queue as commands to run when it
        ends. Whichever finishes first, we cancel and await both — a disconnect
        mid-boot leaks no task."""
        # The fake confirmation. The answer is read and thrown away; a None means
        # the peer hung up at the prompt (nothing played) -> unwind like a drop.
        answer = await session.ask(commands.REBOOT_PROMPT)
        if answer is None:
            return True
        # Answered: NOW the room learns a terminal blinked (never the boot lines),
        # and the sequence begins immediately — no acknowledgement of the answer.
        session.world.broadcast_room(
            res.room_id, None, f"{res.name}'s terminal reboots.", exclude=[res.name]
        )
        session._reboot_active = True
        sink = _SessionSink(session)
        sound_frames = getattr(session.transport, "supports_sound_frames", False)
        play_task = asyncio.create_task(
            reboot.play(session.reboot_script(), sink, sleep=asyncio.sleep,
                        sound_frames=sound_frames,
                        pause_budget=self.config.reboot_pause_budget)
        )
        swallow_task = asyncio.create_task(self._swallow_input(session))
        disconnected = False
        try:
            done, _pending = await asyncio.wait(
                {play_task, swallow_task}, return_when=asyncio.FIRST_COMPLETED
            )
            # The swallow task completes only on EOF, so its presence in `done`
            # means the peer left mid-boot; a normal finish is the player alone.
            disconnected = swallow_task in done
        finally:
            for t in (play_task, swallow_task):
                if not t.done():
                    t.cancel()
            for t in (play_task, swallow_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass  # cancellation is expected; a playback error must not
                          # brick the session (and never carries user content)
            session._reboot_active = False
        if disconnected:
            return True
        # After the sequence: re-show the login banner, then the current room.
        session.send_raw(self.greeting_for(session))
        commands.cmd_look(session, "")
        return False

    async def _swallow_input(self, session: Session) -> None:
        """Read and drop the invoker's input while a reboot plays. Returns only on
        EOF/disconnect; otherwise loops until the caller cancels it."""
        while True:
            line = await session.read_line()
            if line is None:
                return  # peer disconnected mid-boot

    async def _change_password(self, session: Session) -> None:
        """Change the caller's own password. All three prompts are echo-off."""
        res = session.resident
        old = await session.ask("Old password: ", secret=True)
        if old is None:
            return
        if not await self.accounts.averify(res.name, old):
            session.send("Wrong password.")
            return
        new1 = await session.ask("New password: ", secret=True)
        if new1 is None:
            return
        problem = _password_problem(new1)
        if problem:
            session.send(problem)
            return
        new2 = await session.ask("New password again: ", secret=True)
        if new2 is None:
            return
        if new1 != new2:
            session.send("Passwords didn't match.")
            return
        await self.accounts.achange_password(res.name, new1)
        session.send("Password changed.")

    async def _disconnect_account(self, name: str, reason: str) -> None:
        """Force off any live session for ``name``, reusing the .boot path.

        A deleted account must not keep talking from a session whose record no
        longer exists, so this runs whether or not they are connected — a name
        with nobody behind it is simply a no-op.
        """
        res = self.world.get_resident(name)
        if res is None:
            return
        if res.session is not None:
            res.session.kick(reason)
        else:
            # In the disconnect grace window with no live session: end it now
            # rather than let a ghost reattach to a deleted account.
            if res.grace_task is not None:
                res.grace_task.cancel()
            self.world.remove_resident(res)
            self.world.room_emptied(res.room_id)
            res.in_grace = False

    async def _deleteuser(self, session: Session) -> None:
        """Arch-only: erase an account. TWO gates, in this order.

        1. The permanence warning, naming exactly what goes, then **re-type the
           username**. Typing a name you were shown is a different act from
           pressing y — it is the one gate that catches "wrong person", which is
           the mistake that actually happens here.
        2. The CALLER's OWN password (echo-off), exactly as `.deleteme` asks for
           the account holder's. Authority is proved, not assumed from the rank
           the session already holds — and it is the caller's rather than the
           arch's on purpose, so the gate stays correct if the command is ever
           reassigned to another rank.

        Either gate failing cancels with nothing touched.
        """
        target = getattr(session, "pending_target", None)
        session.pending_target = None
        if not target:
            return
        session.send(f"This permanently deletes {target}'s account, their profile, "
                     f"their mailbox, and their private lines. It cannot be undone.")
        typed = await session.ask(f"Re-type the username to confirm ({target}): ")
        if typed is None:
            return
        if typed.strip().casefold() != target.casefold():
            session.send("That doesn't match — nothing was deleted.")
            return
        pw = await session.ask("Your own password: ", secret=True)
        if pw is None:
            return
        if not await self.accounts.averify(session.resident.name, pw):
            session.send("That's not the right password — nothing was deleted.")
            return

        # Gone: the account record, their mailbox, their private-line buffer.
        # NOT touched, deliberately: mail this person SENT lives in the
        # RECIPIENTS' boxes and is theirs, not the sender's, to keep; and any ban
        # on the name or IP persists, because deletion must not be ban-evasion.
        self.accounts.delete(target)
        self.mail.purge_mailbox(target)
        self.revtells.delete_all(target)
        self.resume.revoke_account(target)   # no token outlives its account
        await self._disconnect_account(target, "Your account has been deleted.")
        commands.log_staff_action(session, "deleteuser", target)
        session.send(f"{target} is gone. The name is free again.")

    async def _mailpurge(self, session: Session) -> None:
        """Delete a whole mailbox, after the CALLER's own password (v1.41.0).

        ⛔ It never decrypts anything — `purge_mailbox` removes the box without
        reading a byte, and the mail-power rule is untouched. What the password
        gate protects is not secrecy but IRREVERSIBILITY: this is somebody's
        correspondence, it is the one thing the covenant lets the server keep,
        and until now a single mistyped line finished the job.

        The caller's OWN password, as with `.deleteuser`/`.resetpassword` — the
        person asked is always the person acting, which is what keeps the gate
        correct when a house reassigns the command.
        """
        target = getattr(session, "pending_target", None)
        session.pending_target = None
        if not target:
            return
        session.send(f"This deletes every message in {target}'s mailbox, "
                     f"permanently. Nothing is read; nothing can be recovered.")
        pw = await session.ask("Your own password: ", secret=True)
        if pw is None:
            return
        if not await self.accounts.averify(session.resident.name, pw):
            session.send("That's not the right password — nothing was purged.")
            return
        n = self.mail.purge_mailbox(target)
        commands.log_staff_action(session, "mailpurge", target)
        session.send(f"Purged {n} message(s) from {target}'s mailbox.")

    async def _resetpassword(self, session: Session) -> None:
        """Arch-only: set someone's password — the locked-out backstop.

        The new password is policy-checked exactly as the owner's own `.passwd`
        would be (length floor and ceiling, common-password blocklist) and typed
        twice, because a typo here locks the person out a second time. Then the
        CALLER's OWN password authorizes it, the same gate as `.deleteuser` —
        the caller's, not the arch's, so it stays correct under reassignment.

        Recovery codes are deliberately left alone: this does not consume or void
        them, so a resident who later finds their codes can still use them.
        """
        target = getattr(session, "pending_target", None)
        session.pending_target = None
        if not target:
            return
        session.send(f"Setting a new password for {target}. "
                     f"Tell them out of band; they can change it with .passwd.")
        new1 = await session.ask(f"New password for {target}: ", secret=True)
        if new1 is None:
            return
        problem = _password_problem(new1)
        if problem:
            session.send(problem)
            return
        new2 = await session.ask("New password again: ", secret=True)
        if new2 is None:
            return
        if new1 != new2:
            session.send("Passwords didn't match — nothing was changed.")
            return
        pw = await session.ask("Your own password: ", secret=True)
        if pw is None:
            return
        if not await self.accounts.averify(session.resident.name, pw):
            session.send("That's not the right password — nothing was changed.")
            return
        await self.accounts.achange_password(target, new1)
        commands.log_staff_action(session, "resetpassword", target)
        session.send(f"{target}'s password is set. Their recovery codes are untouched.")

    async def _recovery_new(self, session: Session) -> None:
        """Regenerate the caller's recovery set (requires the current password)."""
        res = session.resident
        pw = await session.ask("Current password: ", secret=True)
        if pw is None:
            return
        if not await self.accounts.averify(res.name, pw):
            session.send("Wrong password.")
            return
        codes = recovery.generate_set()
        await self.accounts.aset_recovery(res.name, codes)   # replaces the old set
        self.events.record("account_security", account=session.resident.name,
                           event="recovery_codes_regenerated")  # never the codes
        session.send("New recovery codes (the previous set is now void):")
        self._show_recovery(session, codes)

    async def _ban_ip(self, session: Session) -> None:
        """Show what an IP ban would catch, then ask before placing it.

        The collateral confirm is the point of the command being separate from
        `.ban`. An address is not a person: it can be a household, a shared
        flat, a college block or a phone carrier's NAT, and the count is the
        difference between banning one troublemaker and banning eleven people
        who live with them. So it is shown BEFORE the y/n, not reported after.

        Anything but an explicit yes cancels, including a bare Enter.
        """
        pending = getattr(session, "pending_ban_ip", None)
        session.pending_ban_ip = None
        if not pending:
            return
        target, ip, hours = pending

        # Who this catches, split online/offline — the one moment the house
        # tells a staff member these accounts share an address, and it does so
        # only because an act is being considered. No address is printed.
        # ONE output unit, not a line each: `_emit_unit` puts a blank line
        # before every unit, so sending these separately would deal the block
        # out double-spaced.
        session.send_raw("\n".join(commands.ban_ip_collateral_lines(
            session, ip, self.config.event_retention_days)))
        # ...and whether this address is a repeat. Matched internally.
        prior = commands.ban_ip_recidivism(session, ip)
        if prior:
            session.send(prior)

        # ⛔ THE STAFF COLLISION (v1.42.0). Reaching here at all means the caller
        # is the arch: `cmd_ban_ip` refuses anyone else outright, anonymously.
        # The arch is the one rank allowed to be told WHO, so they are told, by
        # name, and asked a second question — because "this also takes staff
        # off the door" is a different decision from "this catches four
        # housemates", and one y/n cannot mean both.
        caught = commands.ban_ip_staff_caught(session, ip, target)
        if caught:
            session.send(f"⚠ This would also ban staff: {', '.join(caught)}.")
            ans = await session.ask("Continue? (y/n) ")
            if ans is None or ans.strip().lower() not in ("y", "yes"):
                # Audited as an attempt, exactly like a decline at the main
                # prompt: being SHOWN who shares an address is the notable act.
                commands.log_staff_action(session, f"ban_ip[{hours:g}h]",
                                           target, outcome="cancelled")
                session.send("Never mind.")
                return

        ans = await session.ask(f"Ban it for {hours:g}h? (y/n) ")
        confirmed = ans is not None and ans.strip().lower() in ("y", "yes")

        # ⚠ RECORDED EITHER WAY. Running `.ban_ip` is itself the notable act —
        # it is the moment a staff member is shown who shares an address — so
        # the attempt is audited even when it ends in nothing. The target is the
        # ACCOUNT NAME, never the address.
        commands.log_staff_action(
            session, f"ban_ip[{hours:g}h]", target,
            outcome="confirmed" if confirmed else "cancelled")

        if not confirmed:
            session.send("Never mind.")
            return
        commands.apply_ip_ban(session, target, ip, hours)

    async def _logoutall(self, session: Session) -> None:
        """Clear the house: disconnect EVERY session except the arch's.

        ⚠ **This is deliberately NOT the target-rank rule** (v1.47.0). It is not
        a moderation command aimed at a person — it is "close the house", and a
        version of it that spared everyone at or above the caller would leave
        exactly the people it is meant to clear. So it takes the opposite shape:
        it hits **all ranks**, and ⛔ **a non-arch caller goes with them.**

        That inclusion is what makes it safe to leave outside `_may_act_on`. A
        staff account handed this command can empty the house, but cannot use it to
        remove a rival and remain — the only person who can run it and still be
        standing afterwards is the arch, who is the one account it cannot touch
        anyway. (F1 of the v1.46 audit: before this, the caller was always
        spared, so a staff account could disconnect its seniors and keep the room.)

        The arch is exempt as a TARGET whoever runs it — an arch shut out of
        their own house has no way back in — and that is the whole of the
        exemption.
        """
        arch_key = self.config.owner_name.casefold()
        caller_is_arch = session.resident.name.casefold() == arch_key
        # Everyone but the arch. ⚠ The caller is in this list unless they ARE
        # the arch; `r.session is not session` is gone on purpose.
        targets = [
            r for r in self.world.all_connected()
            if r.name.casefold() != arch_key and r.session is not None
        ]
        n = len(targets)
        # ⚠ "account(s)", not "resident(s)": it reaches every non-arch rank, and
        # calling staff residents would understate what is about to happen.
        # A non-arch caller is told they are in the count, because they are.
        question = (f"Log out {n} account(s)? (y/n) " if caller_is_arch
                    else f"Log out {n} account(s), including yourself? (y/n) ")
        ans = await session.ask(question)
        if ans is None or ans.strip().lower() not in ("y", "yes"):
            session.send("Never mind.")
            return
        # A count, not names — `.logoutall` reaches everyone, so naming them
        # would be a roll call rather than accountability. Recorded BEFORE the
        # sweep: a non-arch caller is about to disconnect themselves, and the
        # fact must not depend on the actor still being there to write it.
        self.events.record("staff_action", actor=session.resident.name,
                           action="logoutall", target=f"{n} session(s)")
        if not caller_is_arch:
            # Say it before the kick lands, so they see why their own screen
            # went away rather than reading it as a fault.
            session.send(f"Logging out {n} account(s), including you.")
            await session.drain()
        for r in targets:
            if r.session is not None:                    # may have dropped mid-prompt
                r.session.send("The house is closing for now.")
                r.session.kick("")
        if caller_is_arch:
            session.send(f"Logged out {n} account(s).")

    async def _deleteme(self, session: Session) -> bool:
        """Confirm and perform self-service account deletion. Returns True if the
        account was deleted (the caller then disconnects, freeing the name)."""
        res = session.resident
        # The config-named arch may not self-delete: freeing that name would let
        # the next registrant claim arch. Mirrors the fixed-rank guard on .promote/
        # .demote (commands/staff.py). The operator changes the arch in config.
        if res.name.casefold() == self.config.owner_name.casefold():
            session.send("The owner account can't be deleted from here — "
                         "change the owner in the server config instead.")
            return False
        session.send("This permanently deletes your account, your profile, your "
                     "mailbox, and your review buffer. There is no undo.")
        pw = await session.ask("Password: ", secret=True)
        if pw is None:
            return False
        if not await self.accounts.averify(res.name, pw):
            # Wrong password counts toward nothing — just refuse and stay put.
            session.send("That's not the right password — nothing was deleted.")
            return False
        name = res.name
        # Remove the account record and the owner's own mailbox. Mail this person
        # SENT to others lives in the recipients' boxes and is untouched; bans on
        # the name/IP persist (deletion is not ban-evasion).
        self.accounts.delete(name)
        self.mail.purge_mailbox(name)
        self.revtells.delete_all(name)   # the review buffer goes with the account
        self.resume.revoke_account(name)   # and so do any remembered devices
        # A fact, same class as moderation — and now in the same store as one.
        self.events.record("account_deleted", name=name)
        session.send("Your account is gone. Thank you for having been here. Goodbye.")
        await session.drain()
        return True
