# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared test fixtures: a fake in-memory session and a small world."""

import dataclasses
from pathlib import Path

import pytest

from heresay.commands import house_switch
from heresay.config import Config
from heresay.server import _load_agreement, agreement_version, resolve_agreement
from heresay.ratelimit import ShoutLimiter, SpeechGovernor, TokenBucket
from heresay.session import Session
from heresay.theme import load_theme
from heresay.world import Resident, load_world

HOUSE = Path(__file__).resolve().parent / "fixtures" / "house"
WORLD = HOUSE / "world"

# ⛔ Repoint Config's DEFAULT house_dir at the fixture house above.
#
# Many tests build a bare `Config()` and lean on `house_dir` defaulting to
# "house" beside the checkout. In this tree that is the SKELETON — one sealed
# room and an unnamed instance — so a server built on it refuses to boot, which
# is exactly what the skeleton is for. The suite means the fixture house, so it
# says so, once, here.
#
# ⚠ `__defaults__` rather than `Config.house_dir = ...`: a dataclass captures
# its defaults into the generated `__init__` when the class is created, so the
# class attribute is not what a later `Config()` reads. The assert is the guard
# — if a second field ever defaults to the string "house", this would quietly
# repoint that one too.
_house_defaults = [d for d in Config.__init__.__defaults__ if d == "house"]
assert len(_house_defaults) == 1, (
    "conftest repoints the default house_dir at the fixture house; exactly "
    "one Config field may default to the bare string house")
Config.__init__.__defaults__ = tuple(
    str(HOUSE) if d == "house" else d for d in Config.__init__.__defaults__)

# The tests exercise the REFERENCE INSTANCE, so the default theme is Commons's
# own (house/theme.toml: ranks resident/staff/owner, staff level "staff",
# owner level "owner"), not the engine's GENERIC built-in defaults. This is
# what keeps the display-word assertions ("staff", "owner") reading as they
# always have after the v1.54.0 genericization: those are Commons's CONFIGURED
# words, and the server a test boots loads exactly this file.
# ⚠ `instance_name` is `config.toml [world]` since v1.60.0, and the server
# fills it into the theme in `_build`. A FakeSession has no Config, so the
# fixture does the same thing here — otherwise every fake would call the house
# "the house" while production calls it by name.
COMMONS_THEME = dataclasses.replace(
    load_theme(WORLD.parent / "theme.toml"), instance_name="Commons")


def _legacy_rank(level, theme):
    """A pre-1.27 level word as a rank name on this ladder.

    ⚠ `level="wizard"`/`level="arch"` are legacy INPUT tokens (a deliberate
    back-compat surface, see CODE_AUDIT): they map to this house's staff/owner
    RANK, so a test written against the old vocabulary keeps testing the same
    thing. `rank="staff"` is the current spelling for the same intent.
    """
    if level in ("wizard", "staff"):
        return theme.staff_rank
    if level in ("arch", "owner"):
        return theme.owner_rank
    return theme.rank(level) if level in theme.order else theme.resident_rank


class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeAccounts:
    """Just enough of AccountStore for command handlers."""

    def __init__(self):
        self.descriptions = {}
        self.settings = {}          # {casefolded name: {key: value}}

    def set_description(self, name, desc):
        self.descriptions[name.lower()] = desc

    def set_setting(self, name, key, value):
        self.settings.setdefault(name.casefold(), {})[key] = value

    def clear_setting(self, name, key):
        self.settings.setdefault(name.casefold(), {}).pop(key, None)

    def resolve(self, name):
        """(display_name, ambiguous) — the real store's contract.

        The stub knows no accounts, so it echoes the name back unambiguously,
        which is what the real store does for an exact match. Handlers that
        resolve a name before acting (`.ban` since v1.32.0, `.examine`, `.mail`)
        therefore behave here as they do in production instead of raising.
        """
        return name, False

    def get(self, name):
        return None


class RecordingTransport:
    """A transport that records the exact text frames a real Session emits.

    Used to exercise Session.send formatting (ANSI colour, time prefix) that the
    plain FakeSession deliberately skips.
    """

    wants_prompt = True

    def __init__(self, peer_ip="127.0.0.1", supports_ansi=True):
        self.peer_ip = peer_ip
        self.supports_ansi = supports_ansi
        self.frames = []

    def send_text(self, text):
        self.frames.append(text)

    def prompt(self):
        pass

    def set_echo(self, on):
        pass

    async def drain(self):
        pass

    def close(self):
        pass

    @property
    def text(self):
        return "".join(self.frames)


class FakeSession:
    """Captures output instead of writing to a socket.

    ``out`` is the list of text blocks sent; ``text`` joins them for matching.
    """

    def __init__(self, world, name="Tester", room=None, clock=None, config=None,
                 accounts=None, mail=None, bans=None, level="resident", rank=None,
                 wall_clock=None, theme=None, help_topics=None, revtells=None,
                 state=None, supports_control_frames=True, width=None,
                 resume=None, events=None, ban_history=None,
                 load_report=None):
        self.world = world
        self.config = config or Config(wrap_columns=1000)
        self.accounts = accounts or FakeAccounts()
        self.mail = mail
        self.revtells = revtells
        self.events = events        # EventStore for staff-action tests (or None)
        self.load_report = load_report   # LoadReport for .load_report (or None)
        self.ban_history = ban_history   # BanHistoryStore (or None)
        self.resume = resume        # ResumeStore for .remember tests (or None)
        self.bans = bans
        self.state = state          # StateStore for .ownerset / jail tests (or None)
        # Control frames (\x01…) this session would put on the wire. Only the WEB
        # door has a control convention, so a socket door is modelled by passing
        # supports_control_frames=False — the same gate the real Session applies.
        self.supports_control_frames = supports_control_frames
        self.controls = []
        # The render width. A real Session derives this from the transport's
        # reported columns; a fake one is simply told, so a narrow test is
        # `FakeSession(..., width=40)` and nothing else.
        #
        # NOT taken from `config.wrap_columns`: the fake config's 1000 is a
        # test convenience meaning "don't wrap speech in this test", not a claim
        # about screen size, and letting it size the *** rules would give every
        # framed block a thousand-column rule. An ordinary screen is the default.
        self.width = width if width is not None else Config().wrap_columns
        self.clock = clock or FakeClock()
        self._wall = wall_clock or self.clock
        self.peer_ip = "127.0.0.1"
        self.out = []
        self._closing = False
        self._disconnect_reason = None
        self.editor = None
        # The real Session carries these for the async prompts the session
        # loop owns (.deleteuser / .resetpassword / .ban_ip). A handler that
        # validates and stages a target sets one, so a fake needs them to
        # test that a REFUSED command staged nothing.
        self.pending_target = None
        self.pending_ban_ip = None
        # The display switches are NOT set here — they are live properties on
        # the real Session (reused below), resolving own-then-house at use time.
        # Assigning one would record an explicit override and make every fake
        # session look like a resident who had chosen a value.
        self.theme = theme or COMMONS_THEME
        # ⚠ A REFERENCE, exactly as the real Session takes one
        # (`self.help_topics = help_topics or {}`). Copying it here would make
        # the fake disagree with production about the one property the live
        # help-reload depends on: that an open session sees a page edited under
        # it (v1.39.0). A fake that models the wrong thing is worse than none.
        self.help_topics = help_topics if help_topics is not None else {}
        self.reboot_steps = None
        self._reboot_active = False
        self.reboot_bucket = TokenBucket(
            1, 1.0 / max(self.config.reboot_rate_seconds, 1e-6)
        )
        self.staff_bucket = TokenBucket(
            1, 1.0 / max(self.config.staff_call_seconds, 1e-6)
        )
        self.speech = SpeechGovernor(
            burst=self.config.speech_burst,
            refill_per_sec=self.config.speech_refill_per_sec,
            mute_strikes=self.config.speech_mute_strikes,
            disconnect_strikes=self.config.speech_disconnect_strikes,
            mute_seconds=self.config.speech_mute_seconds,
        )
        self.shout_limiter = ShoutLimiter(self.config.shout_per_seconds)
        self.mail_bucket = TokenBucket(
            self.config.mail_send_burst, self.config.mail_send_refill_per_sec
        )
        self.invite_bucket = TokenBucket(
            self.config.invite_burst, self.config.invite_refill_per_sec
        )
        # Effectively unlimited in unit tests (many dispatch calls at a fixed
        # clock); the command governor is exercised on its own in test_hardening.
        self.command_bucket = TokenBucket(10**9, 10**9)
        # Ranks are ladder NAMES since v1.27.0. `rank=` is the current spelling;
        # `level=` still takes the pre-1.27 words ("wizard"/"arch") and is mapped
        # through the session's own ladder, so the tests written against the old
        # vocabulary keep testing the same thing without being rewritten.
        self.resident = Resident(name=name, description="",
                                 room_id=room or world.start_room,
                                 rank=rank if rank is not None
                                 else _legacy_rank(level, self.theme),
                                 ladder=self.theme)
        self.resident.session = self
        self.resident.last_input = self.clock()
        world.add_resident(self.resident)

    # The live display switches, reused from the real Session so unit tests
    # exercise production resolution rather than a stand-in. Each resolves
    # own-then-house at use-time; assigning one records an explicit override.
    _switch = Session._switch
    _set_switch = Session._set_switch
    dim_system = Session.dim_system
    show_time = Session.show_time
    help_style = Session.help_style
    follows_house = Session.follows_house

    # Reuse the real editor machinery so tests exercise production logic.
    in_editor = Session.in_editor
    start_editor = Session.start_editor
    feed_editor = Session.feed_editor
    has_reboot_script = Session.has_reboot_script
    is_narrow = Session.is_narrow          # derived from `width`, as in the real one
    reboot_steps_narrow = None
    reboot_script = Session.reboot_script

    # methods the command layer calls
    def now(self):
        return self.clock()

    def now_wall(self):
        return self._wall()

    def speech_check(self):
        return self.speech.check(self.now())

    def request_disconnect(self, reason):
        self._disconnect_reason = reason
        self._closing = True

    def kick(self, reason):
        # Unit-test stand-in: record intent without a real transport/loop.
        self._disconnect_reason = "kicked"
        self._closing = True
        self.out.append(f"You have been disconnected. {reason}".rstrip())

    def send(self, text, *, dim=True):
        self.out.append(text)

    def send_raw(self, text):
        self.out.append(text)

    send_control_resume = Session.send_control_resume

    def send_control(self, payload):
        # Mirrors Session.send_control: silently dropped on a door without the
        # control convention, and never part of `out` (it is not output).
        if not self.supports_control_frames:
            return
        self.controls.append(payload)

    def prompt(self):
        pass

    @property
    def text(self):
        return "\n".join(self.out)

    def clear(self):
        self.out.clear()


# The operator's real runtime state lives at the repo root. A test that leaves a
# Config field at its default gets a path resolved against the CWD — i.e. right
# on top of it. CLAUDE.md's rule is absolute ("Tests use tmp paths, never the
# root files"), so enforce it mechanically rather than by remembering: this
# autouse guard fails the exact test that creates one, instead of letting a
# stray file appear silently.
REPO_ROOT = Path(__file__).resolve().parent.parent

# The version of the agreement a freshly-seeded test account should look like it
# has accepted. Consent is tied to the text (see test_reconsent.py), so an
# account fabricated straight into the store has consented to nothing and would
# be stopped at the re-consent prompt on its way in.
# Resolved against a DEFAULT Config, because that is what a test server is built
# with — the placeholders are part of the text now (see resolve_agreement), so a
# hash of the raw file would match nothing.
CURRENT_AGREEMENT_HASH = agreement_version(
    resolve_agreement(_load_agreement(REPO_ROOT / "tests" / "fixtures" / "house" / "agreement.txt"),
                      Config(), COMMONS_THEME)
)


def seed_account(store, name, password, **kw):
    """Create an account that has already agreed to the CURRENT agreement.

    For tests that need a resident to exist but are not about consent: it puts
    them in the same state a real account reaches by going through creation.
    """
    return store.create(name, password, agreed=True,
                        agreed_hash=CURRENT_AGREEMENT_HASH, **kw)
RUNTIME_FILES = ("accounts.json", "mailboxes.json", "mail.key", "revtells.json",
                 "bans.json", "state.json", "resume.json", "events.jsonl",
                 # permissions.toml is CONFIG rather than state, but it is
                 # written by the daemon on first boot and resolves beside
                 # config.toml — i.e. the repo root — so a test that forgets to
                 # point it at tmp_path leaves one here. It did, once, before
                 # this line existed (v1.37.0).
                 "permissions.toml", "ban_history.json",
                 # Never written by anything — but it resolves beside
                 # config.toml too, so a test that WRITES one and forgets to
                 # point it at tmp_path would leave it in the repo. Cheap guard,
                 # and the same trap permissions.toml already fell into once.
                 "forbidden_names.txt",
                 # The control socket (v1.46.0). The daemon really does create
                 # this one, so a test that starts a server without pointing it
                 # at tmp_path leaves a socket file in the repo.
                 "control.sock",
                 # The per-box stores (v1.52.5). Derived from mail_file /
                 # revtell_file, so a test that points those at tmp_path gets
                 # these there too — and one that forgets leaves a DIRECTORY
                 # here, which the old file-only guard would have walked past.
                 "mailboxes.json.d", "revtells.json.d",
                 # ...and the retired single files, for the same reason.
                 "mailboxes.json.pre-split", "revtells.json.pre-split")


@pytest.fixture(autouse=True)
def no_repo_root_state():
    before = {f: (REPO_ROOT / f).exists() for f in RUNTIME_FILES}
    yield
    created = [f for f in RUNTIME_FILES
               if (REPO_ROOT / f).exists() and not before[f]]
    assert not created, (
        f"test created repo-root runtime state: {created}. Every Config in a "
        f"test must point ALL state files at tmp_path (see CLAUDE.md)."
    )


# Commons's front door and jail, named the way config.toml names them
# (v1.17.0 — no longer read from the room files).
ENTRY_ROOM = "atrium"
JAIL_ROOM = "holding_cell"


@pytest.fixture
def world():
    return load_world(WORLD, ENTRY_ROOM, JAIL_ROOM)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def session(world, clock):
    return FakeSession(world, name="Corvid", clock=clock)
