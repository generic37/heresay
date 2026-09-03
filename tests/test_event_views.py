# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The counting views over the event store (v1.35.0): `.security`, `.staff_calls`.

`.staff_actions` set the shape — a filter over `events.read()` plus a layout —
and these two are that shape again, with two differences worth protecting:

- they COUNT rather than list, over a window each view owns
  (`security_window_hours` / `staff_calls_window_hours`, both capped at the
  store's retention, both defaulting to a day);
- `.security` shows numbers and NOTHING else, while `.staff_calls` names
  residents because a tally with the names removed cannot answer the question it
  exists for. Neither prints an address, and a grep test says so.

The third thing here is the twelfth event type, `staff_mail` — the other half of
`staff_call`, without which the Mailed column has nothing to count.
"""

from pathlib import Path

import pytest

import heresay.commands as C
from heresay.commands import dispatch
from heresay.commands.registry import OWNER, STAFF, command_seed
from heresay.commands.staff import STAFF_BOX, SECURITY_ROWS
from heresay.config import Config
from heresay.events import EVENT_TYPES, EventStore
from heresay.mail import MailStore
from heresay.theme import DEFAULT_THEME

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"

# An address that must never reach a screen. Fed into every type that carries
# one, then grepped for out of the whole transcript.
IP = "203.0.113.77"


class FakeWall:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def wall():
    return FakeWall()


@pytest.fixture
def events(tmp_path, wall):
    return EventStore(tmp_path / "events.jsonl", retention_days=10.0,
                      wall_clock=wall)


def _session(world, clock, events, name="Generic", level="arch", **kw):
    return FakeSession(world, name=name, clock=clock, level=level,
                       events=events, **kw)


def _run(session, line):
    session.clear()
    dispatch(session, line)
    return session.text


def _body(text):
    """The rows, without the frame rules."""
    return [l for l in text.split("\n") if l.strip() and "***" not in l]


def _count_for(text, label):
    line = next(l for l in _body(text) if l.strip().startswith(label))
    return int(line.split()[-1])


# ---------------------------------------------------------------------------
# The `staff_mail` event (part 1) — the Mailed column's source
# ---------------------------------------------------------------------------

def _mail_store(tmp_path):
    return MailStore(tmp_path / "mailboxes.json", tmp_path / "mail.key")


def test_staff_mail_is_a_declared_type():
    """⚠ Renamed from "the twelfth": `bad_frame` made thirteen in v1.52.0, and a
    test named after an ordinal has to be renamed every time the set grows —
    which is how the ordinal stops being the point."""
    assert "staff_mail" in EVENT_TYPES
    assert len(EVENT_TYPES) == 15


def test_mail_staff_records_the_writer_and_nothing_else(world, clock, events,
                                                        tmp_path):
    mail = _mail_store(tmp_path)
    s = FakeSession(world, name="Wren", clock=clock, events=events, mail=mail)
    dispatch(s, ".mail_staff the tap in the cabin is running")

    recorded = events.read(types=("staff_mail",))
    assert [e["account"] for e in recorded] == ["Wren"]
    # Facts only: the account, and the two fields every event has.
    assert all(set(e) == {"ts", "type", "account"} for e in recorded)
    # ...and not a syllable of the note anywhere in the file.
    assert "tap in the cabin" not in events.path.read_text(encoding="utf-8")


def test_mail_staff_still_delivers(world, clock, events, tmp_path):
    """The event is a side note; the letterbox is the point."""
    mail = _mail_store(tmp_path)
    s = FakeSession(world, name="Wren", clock=clock, events=events, mail=mail)
    dispatch(s, ".mail_staff something is wrong at the well")
    assert mail.count(STAFF_BOX) == 1
    assert "the well" in mail.read(STAFF_BOX)[0].body
    assert "Left with the staff." in s.text


def test_a_full_letterbox_records_nothing(world, clock, events, tmp_path):
    """⚠ Recorded on DELIVERY, not on the attempt: nothing was stored, so no
    contact was made, and a tally that counted it would be wrong."""
    mail = MailStore(tmp_path / "mailboxes.json", tmp_path / "mail.key", cap=1)
    mail.deliver(STAFF_BOX, "Someone", "first")
    s = FakeSession(world, name="Wren", clock=clock, events=events, mail=mail)
    dispatch(s, ".mail_staff this will not fit")
    assert "letterbox is full" in s.text
    assert events.read(types=("staff_mail",)) == []


def test_the_editor_path_records_too(world, clock, events, tmp_path):
    """A bare `.mail_staff` opens the editor; the fact is recorded where the
    delivery happens, so both routes in are covered by one call site."""
    mail = _mail_store(tmp_path)
    s = FakeSession(world, name="Wren", clock=clock, events=events, mail=mail)
    dispatch(s, ".mail_staff")
    s.feed_editor("a longer note")
    s.feed_editor(".end")
    assert [e["account"] for e in events.read(types=("staff_mail",))] == ["Wren"]


# ---------------------------------------------------------------------------
# `.security` — the counts overview (part 2)
# ---------------------------------------------------------------------------

def _seed_security(events, wall=None):
    """One of each countable thing, in different numbers so a row that counted
    its neighbour would be caught."""
    for _ in range(14):
        events.record("auth_fail", ip=IP, phase="login")
    for _ in range(3):
        events.record("login_timeout", ip=IP)
    for _ in range(2):
        events.record("account_security", account="Ada",
                      event="password_reset_recovery")
    events.record("account_security", account="Bob",
                  event="recovery_codes_regenerated")
    for _ in range(3):
        events.record("account_deleted", name="Ghost")
    # Not counted, and the reason the exclusion exists: an agreement edit puts
    # the whole house through this.
    for _ in range(40):
        events.record("account_security", ip=IP, event="reconsent")


def test_security_seeds_to_arch_and_is_assignable(world, clock, events):
    assert command_seed()["security"] == OWNER
    assert C._visible("security", DEFAULT_THEME.owner_rank, DEFAULT_THEME)
    assert not C._visible("security", DEFAULT_THEME.staff_rank, DEFAULT_THEME)


@pytest.mark.parametrize("level", ["resident", "wizard"])
def test_security_is_invisible_below_the_arch(world, clock, events, level):
    s = _session(world, clock, events, name="Corvid", level=level)
    assert "Unknown command" in _run(s, ".security")


def test_security_counts_each_row(world, clock, events):
    _seed_security(events)
    out = _run(_session(world, clock, events), ".security")
    assert _count_for(out, "failed login attempts") == 14
    assert _count_for(out, "login timeouts") == 3
    assert _count_for(out, "password resets (recovery code)") == 2
    assert _count_for(out, "recovery codes regenerated") == 1
    assert _count_for(out, "session takeovers") == 0
    assert _count_for(out, "accounts self-deleted") == 3


def test_the_auth_ceiling_row_counts_TRIPS_ONLY(world, clock, events):
    """v1.53.0. The ceiling's trip is a security signal; its clear is the
    recovery, and counting both would double every incident on a screen whose
    whole job is "is anything happening?"."""
    for _ in range(3):
        events.record("auth_ceiling", phase="tripped", threshold=50,
                      window=60.0, cooldown=120.0)
    for _ in range(3):
        events.record("auth_ceiling", phase="cleared")
    out = _run(_session(world, clock, events), ".security")
    assert _count_for(out, "auth ceiling trips") == 3
    assert "cleared" not in out


def test_the_auth_ceiling_row_shows_no_numbers_from_the_event(world, clock, events):
    """⚠ The row is a COUNT like every other row here. The threshold/window/
    cooldown live in the event for whoever reads the log; this screen answers
    "how often", and putting the dials on it would make one row look like a
    different kind of thing."""
    events.record("auth_ceiling", phase="tripped", threshold=77,
                  window=61.0, cooldown=123.0)
    out = _run(_session(world, clock, events), ".security")
    assert _count_for(out, "auth ceiling trips") == 1
    for n in ("77", "61", "123"):
        assert n not in out


def test_a_takeover_counts(world, clock, events):
    events.record("takeover", account="Ada", from_ip=IP)
    events.record("takeover", account="Bob", from_ip=IP)
    out = _run(_session(world, clock, events), ".security")
    assert _count_for(out, "session takeovers") == 2


def test_reconsent_is_excluded(world, clock, events):
    """It fires for everyone on any agreement change; counting it would bury
    the two rows that mean something."""
    for _ in range(50):
        events.record("account_security", ip=IP, event="reconsent")
    out = _run(_session(world, clock, events), ".security")
    assert _count_for(out, "password resets (recovery code)") == 0
    assert _count_for(out, "recovery codes regenerated") == 0
    assert "reconsent" not in out
    assert "50" not in out


def test_an_empty_log_is_a_screen_of_zeroes(world, clock, events):
    """Not a "nothing recorded" line: a screen of zeroes IS the answer, and the
    answer is the good one."""
    out = _run(_session(world, clock, events), ".security")
    for _t, _e, wide, _n in SECURITY_ROWS:
        assert _count_for(out, wide) == 0


def test_security_shows_no_name_and_no_address(world, clock, events):
    _seed_security(events)
    events.record("takeover", account="Ada", from_ip=IP)
    out = _run(_session(world, clock, events), ".security")
    assert IP not in out
    for name in ("Ada", "Bob", "Ghost"):
        assert name not in out
    assert "see the server log" in out            # where detail actually lives


def test_the_window_is_honoured(world, clock, events, wall):
    """Two failures a week apart, a 24-hour window: one of them is history."""
    events.record("auth_fail", ip=IP, phase="login")
    wall.t += 7 * 86400
    events.record("auth_fail", ip=IP, phase="login")
    cfg = Config(wrap_columns=1000, event_retention_days=30.0,
                 security_window_hours=24.0)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    out = _run(s, ".security")
    assert _count_for(out, "failed login attempts") == 1
    assert "last 24 hours" in out


def test_a_wider_window_sees_more(world, clock, events, wall):
    events.record("auth_fail", ip=IP, phase="login")
    wall.t += 3 * 86400
    events.record("auth_fail", ip=IP, phase="login")
    cfg = Config(wrap_columns=1000, event_retention_days=30.0,
                 security_window_hours=24 * 7)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    out = _run(s, ".security")
    assert _count_for(out, "failed login attempts") == 2
    assert "last 168 hours" in out


def test_the_window_is_capped_at_retention(world, clock, events, wall):
    """A screen cannot show what the store no longer holds, so a longer setting
    just means "everything retained" — and says so in the title."""
    cfg = Config(wrap_columns=1000, event_retention_days=2.0,
                 security_window_hours=24 * 365)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    assert "last 48 hours" in _run(s, ".security")


def test_a_zero_window_means_everything_retained(world, clock, events, wall):
    """The other end of the same rule: no setting yields an empty screen."""
    events.record("auth_fail", ip=IP, phase="login")
    cfg = Config(wrap_columns=1000, event_retention_days=3.0,
                 security_window_hours=0.0)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    out = _run(s, ".security")
    assert "last 72 hours" in out
    assert _count_for(out, "failed login attempts") == 1


def test_with_retention_off_a_zero_window_keeps_everything(world, clock, events,
                                                           wall):
    store = EventStore(events.path, retention_days=0.0, wall_clock=wall)
    store.record("auth_fail", ip=IP, phase="login")
    wall.t += 400 * 86400
    cfg = Config(wrap_columns=1000, event_retention_days=0.0,
                 security_window_hours=0.0)
    s = _session(world, clock, store, config=cfg, wall_clock=wall)
    out = _run(s, ".security")
    assert "everything kept" in out
    assert _count_for(out, "failed login attempts") == 1


def test_security_fits_at_forty(world, clock, events):
    _seed_security(events)
    out = _run(_session(world, clock, events, width=40), ".security")
    for line in out.split("\n"):
        assert len(line) <= 40, repr(line)
    assert "failed logins" in out                 # the hand-set narrow label
    assert "see the server log" in out            # the pointer survives the width


def test_security_fits_at_seventy_eight(world, clock, events):
    _seed_security(events)
    out = _run(_session(world, clock, events, width=78), ".security")
    for line in out.split("\n"):
        assert len(line) <= 78, repr(line)
    assert "failed login attempts" in out


# ---------------------------------------------------------------------------
# `.staff_calls` — the per-resident tally (part 3)
# ---------------------------------------------------------------------------

def _seed_calls(events):
    for _ in range(3):
        events.record("staff_call", account="Bramble")
    events.record("staff_mail", account="Bramble")
    events.record("staff_call", account="Corvid")
    for _ in range(2):
        events.record("staff_mail", account="Wren")


def test_staff_calls_seeds_to_staff_and_is_assignable(world, clock, events):
    assert command_seed()["staff_calls"] == STAFF
    assert C._visible("staff_calls", DEFAULT_THEME.staff_rank, DEFAULT_THEME)
    assert C._visible("staff_calls", DEFAULT_THEME.owner_rank, DEFAULT_THEME)
    assert not C._visible("staff_calls", DEFAULT_THEME.resident_rank, DEFAULT_THEME)


def test_a_resident_cannot_see_it(world, clock, events):
    s = _session(world, clock, events, name="Corvid", level="resident")
    assert "Unknown command" in _run(s, ".staff_calls")


def test_a_keeper_can(world, clock, events):
    _seed_calls(events)
    s = _session(world, clock, events, name="Wizzy", level="wizard")
    assert "Unknown command" not in _run(s, ".staff_calls")


def test_it_tallies_both_doors_per_person(world, clock, events):
    _seed_calls(events)
    out = _run(_session(world, clock, events, width=78), ".staff_calls")
    rows = {l.split()[0]: l.split()[1:] for l in _body(out) if not l.startswith("  Who")}
    assert rows["Bramble"] == ["3", "1"]
    assert rows["Corvid"] == ["1", "0"]
    assert rows["Wren"] == ["0", "2"]


def test_it_is_sorted_busiest_first(world, clock, events):
    """So the person who has called eleven times since breakfast is the first
    line, not something you notice on the third read."""
    _seed_calls(events)
    out = _run(_session(world, clock, events, width=78), ".staff_calls")
    names = [l.split()[0] for l in _body(out)][1:]        # drop the header row
    assert names == ["Bramble", "Wren", "Corvid"]         # 4, 2, 1


def test_a_tie_is_broken_by_name_not_by_luck(world, clock, events):
    events.record("staff_call", account="Zed")
    events.record("staff_call", account="Alma")
    out = _run(_session(world, clock, events, width=78), ".staff_calls")
    names = [l.split()[0] for l in _body(out)][1:]
    assert names == ["Alma", "Zed"]


def test_two_spellings_are_one_person(world, clock, events):
    events.record("staff_call", account="Bramble")
    events.record("staff_mail", account="bramble")
    out = _run(_session(world, clock, events, width=78), ".staff_calls")
    assert len([l for l in _body(out)][1:]) == 1
    assert "1 resident, 2 contacts" in out


def test_the_footer_counts_people_and_contacts(world, clock, events):
    _seed_calls(events)
    out = _run(_session(world, clock, events), ".staff_calls")
    assert "3 residents, 7 contacts" in out


def test_a_quiet_day_says_so(world, clock, events):
    out = _run(_session(world, clock, events), ".staff_calls")
    assert "No staff calls or mail" in out


def test_it_counts_only_its_two_types(world, clock, events):
    """The filter is the command, exactly as `.staff_actions` established."""
    events.record("staff_action", actor="Wizzy", action="gag", target="Bob")
    events.record("login", account="Ada", ip=IP)
    events.record("staff_call", account="Bramble")
    out = _run(_session(world, clock, events), ".staff_calls")
    assert "Bramble" in out
    for other in ("Wizzy", "Bob", "Ada", "gag"):
        assert other not in out


def test_staff_calls_honours_its_window(world, clock, events, wall):
    events.record("staff_call", account="Bramble")
    wall.t += 7 * 86400
    events.record("staff_call", account="Bramble")
    cfg = Config(wrap_columns=1000, event_retention_days=30.0,
                 staff_calls_window_hours=24.0)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    out = _run(s, ".staff_calls")
    assert "1 resident, 1 contact" in out
    assert "last 24 hours" in out


def test_staff_calls_window_is_capped_at_retention(world, clock, events, wall):
    events.record("staff_call", account="Bramble")
    cfg = Config(wrap_columns=1000, event_retention_days=2.0,
                 staff_calls_window_hours=24 * 365)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    assert "last 48 hours" in _run(s, ".staff_calls")


def test_the_two_windows_are_independent(world, clock, events, wall):
    """Two keys, two questions. Moving one must not move the other."""
    cfg = Config(wrap_columns=1000, event_retention_days=30.0,
                 security_window_hours=6.0, staff_calls_window_hours=48.0)
    s = _session(world, clock, events, config=cfg, wall_clock=wall)
    events.record("staff_call", account="Bramble")
    assert "last 6 hours" in _run(s, ".security")
    assert "last 48 hours" in _run(s, ".staff_calls")


def test_staff_calls_fits_at_forty(world, clock, events):
    _seed_calls(events)
    events.record("staff_call", account="Bartholomewestershire")
    out = _run(_session(world, clock, events, width=40), ".staff_calls")
    for line in out.split("\n"):
        assert len(line) <= 40, repr(line)
    assert "called 3, mailed 1" in out
    assert "last 24 hours" in out                 # the window survives the width


def test_staff_calls_fits_at_seventy_eight(world, clock, events):
    _seed_calls(events)
    out = _run(_session(world, clock, events, width=78), ".staff_calls")
    for line in out.split("\n"):
        assert len(line) <= 78, repr(line)
    assert "Who" in out and "Called" in out and "Mailed" in out


# ---------------------------------------------------------------------------
# ⛔ No address reaches either screen
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("width", [40, 78])
def test_no_ip_appears_in_either_view(world, clock, events, width):
    """The v1.33.0 rule, swept over both new screens: every type that carries an
    address is recorded with one, and neither view may print it."""
    _seed_security(events)
    _seed_calls(events)
    events.record("takeover", account="Ada", from_ip=IP)
    events.record("connection", dir="open", ip=IP)
    events.record("refused", ip=IP, reason="ip_banned")
    events.record("flood_disconnect", ip=IP)
    events.record("login", account="Bramble", ip=IP)
    s = _session(world, clock, events, width=width)
    transcript = _run(s, ".security") + "\n" + _run(s, ".staff_calls")
    assert IP not in transcript
    assert "203.0.113" not in transcript
