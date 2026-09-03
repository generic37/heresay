# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

""".staff_actions — the first view over the event store (v1.30.0).

Deliberately a thin command: `events.read(types=("staff_action",),
newest_first=True)` and a layout. What these tests protect is that it stays thin
and stays arch-only — every future view is the same reader with a different
filter, so the shape set here is the shape they will all copy.
"""

from pathlib import Path

import pytest

import heresay.commands as C
from heresay.commands import dispatch
from heresay.config import Config
from heresay.events import EventStore
from heresay.commands.staff import STAFF_ACTIONS_MAX

from conftest import FakeSession

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"


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


def _arch(world, clock, events, **kw):
    return FakeSession(world, name="Generic", clock=clock, level="arch",
                       events=events, **kw)


def _screen(session):
    session.clear()
    dispatch(session, ".staff_actions")
    return session.text


# ---- the gate --------------------------------------------------------------

def test_a_resident_cannot_see_it_at_all(world, clock, events):
    """Invisible below its rank, like every gated verb: the same reply a typo
    gets, never a permissions hint."""
    r = FakeSession(world, name="Corvid", clock=clock, events=events)
    r.clear()
    dispatch(r, ".staff_actions")
    assert "Unknown command" in r.text


def test_a_keeper_cannot_see_it_either(world, clock, events):
    k = FakeSession(world, name="Wizzy", clock=clock, level="wizard", events=events)
    k.clear()
    dispatch(k, ".staff_actions")
    assert "Unknown command" in k.text


def test_the_arch_can(world, clock, events):
    assert "Unknown command" not in _screen(_arch(world, clock, events))


def test_it_seeds_to_arch_and_is_assignable(world, clock, events):
    """Rank comes from the command table, like `.deleteuser` — so a house can
    hand it to staff without a code change."""
    from heresay.commands.registry import OWNER, command_seed
    assert command_seed()["staff_actions"] == OWNER
    t = C.DEFAULT_THEME if hasattr(C, "DEFAULT_THEME") else None
    from heresay.theme import DEFAULT_THEME
    assert C._visible("staff_actions", DEFAULT_THEME.owner_rank, DEFAULT_THEME)
    assert not C._visible("staff_actions", DEFAULT_THEME.staff_rank, DEFAULT_THEME)


# ---- what it shows ---------------------------------------------------------

def test_nothing_recorded_says_so(world, clock, events):
    assert "No staff actions recorded." in _screen(_arch(world, clock, events))


def test_it_shows_actions_newest_first(world, clock, events, wall):
    for i, action in enumerate(("gag", "jail", "ban")):
        wall.t += 60
        events.record("staff_action", actor="Wizzy", action=action,
                      target=f"Bob{i}")
    out = _screen(_arch(world, clock, events))
    assert out.index("ban") < out.index("jail") < out.index("gag")


def test_it_shows_when_who_did_and_to_whom(world, clock, events):
    events.record("staff_action", actor="Wizzy", action="jail", target="Bob")
    out = _screen(_arch(world, clock, events))
    assert "Wizzy" in out and "jail" in out and "Bob" in out
    assert "2023" in out or "2024" in out or "20" in out      # a rendered date
    assert "Staff actions" in out                              # framed like a listing


def test_it_shows_only_wizard_actions(world, clock, events):
    """The filter is the command. A connection or an auth failure is a different
    view's business."""
    events.record("connection", dir="open", ip="203.0.113.9")
    events.record("auth_fail", ip="203.0.113.9", phase="login")
    events.record("staff_action", actor="Wizzy", action="gag", target="Bob")
    out = _screen(_arch(world, clock, events))
    assert "203.0.113.9" not in out
    assert "Wizzy" in out


def test_the_real_commands_land_in_it(world, clock, events, tmp_path):
    """End to end: a staff gags someone, the arch reads it back. This is the
    whole feature in four lines."""
    from heresay.state import StateStore
    from heresay.commands.registry import command_seed
    state = StateStore(tmp_path / "state.json", command_seed=command_seed())
    victim = FakeSession(world, name="Bob", clock=clock, events=events)
    wiz = FakeSession(world, name="Wizzy", clock=clock, level="wizard",
                      events=events, state=state)
    dispatch(wiz, ".gag Bob")
    dispatch(wiz, ".ungag Bob")

    out = _screen(_arch(world, clock, events))
    actions = [e["action"] for e in
               events.read(types=("staff_action",), newest_first=True)]
    assert actions == ["ungag", "gag"]                 # newest first, both there
    assert "ungag" in out and "Wizzy" in out and "Bob" in out


def test_a_count_argument_limits_it(world, clock, events, wall):
    for i in range(5):
        wall.t += 60
        events.record("staff_action", actor="Wizzy", action=f"act{i}", target="B")
    a = _arch(world, clock, events)
    a.clear(); dispatch(a, ".staff_actions 2")
    assert "act4" in a.text and "act3" in a.text
    assert "act2" not in a.text


@pytest.mark.parametrize("arg", ["0", "-1", "abc", str(STAFF_ACTIONS_MAX + 1), "9" * 40])
def test_a_bad_count_gets_the_usage_line(world, clock, events, arg):
    a = _arch(world, clock, events)
    a.clear(); dispatch(a, f".staff_actions {arg}")
    assert "Usage: .staff_actions" in a.text


def test_the_footer_names_the_retention_window(world, clock, events):
    events.record("staff_action", actor="Wizzy", action="gag", target="Bob")
    cfg = Config(wrap_columns=1000, event_retention_days=3.0)
    a = _arch(world, clock, events, config=cfg)
    out = _screen(a)
    assert "last 3 days" in out          # what is shown is bounded, and says so


# ---- narrow ----------------------------------------------------------------

def test_it_fits_at_forty(world, clock, events, wall):
    for i in range(3):
        wall.t += 60
        events.record("staff_action", actor="Corvidus", action="mailpurge",
                      target=f"Somebody{i}")
    a = _arch(world, clock, events, width=40)
    out = _screen(a)
    for line in out.split("\n"):
        assert len(line) <= 40, repr(line)


def test_narrow_leads_with_the_action_not_the_clock(world, clock, events):
    """A narrow screen is scanned top-down, so the thing being read comes first
    and the timestamp sits beneath it — the reverse of the wide grid."""
    events.record("staff_action", actor="Wizzy", action="jail", target="Bob")
    a = _arch(world, clock, events, width=40)
    body = [l for l in _screen(a).split("\n") if l.strip() and "***" not in l]
    lead = next(l for l in body if "Wizzy" in l)
    assert lead.strip().startswith("Wizzy jail")
    assert ":" in body[body.index(lead) + 1]           # the time, indented under it


def test_wide_is_a_grid_with_headers(world, clock, events):
    events.record("staff_action", actor="Wizzy", action="jail", target="Bob")
    out = _screen(_arch(world, clock, events, width=78))
    assert "When" in out and "Who" in out and "Did" in out and "To" in out
    for line in out.split("\n"):
        assert len(line) <= 78, repr(line)


# ---- the outcome (v1.33.1) -------------------------------------------------
#
# `.ban_ip` is audited whether it is confirmed or cancelled, so two rows can
# share an actor, an action and a target and mean opposite things. Without the
# outcome the log reads as though every attempt landed.

def _both_outcomes(events):
    events.record("staff_action", actor="Generic", action="ban_ip[24h]",
                  target="Mallory", outcome="confirmed")
    events.record("staff_action", actor="Generic", action="ban_ip[24h]",
                  target="Carol", outcome="cancelled")


def test_wide_shows_confirmed_and_cancelled(world, clock, events):
    _both_outcomes(events)
    out = _screen(_arch(world, clock, events, width=78))
    assert "Outcome" in out                       # the column appears...
    line = next(l for l in out.split("\n") if "Mallory" in l)
    assert line.rstrip().endswith("confirmed")
    line = next(l for l in out.split("\n") if "Carol" in l)
    assert line.rstrip().endswith("cancelled")


def test_the_two_ban_ip_rows_are_distinguishable(world, clock, events):
    """The point of the whole change: same actor, same action, same shape —
    only the outcome says which one became a ban."""
    _both_outcomes(events)
    out = _screen(_arch(world, clock, events, width=78))
    a = next(l for l in out.split("\n") if "Mallory" in l).split()
    b = next(l for l in out.split("\n") if "Carol" in l).split()
    assert a[:-2] == b[:-2]                        # when, who, did: identical
    assert a[-1] == "confirmed" and b[-1] == "cancelled"


def test_rows_without_an_outcome_show_none(world, clock, events):
    """Only `.ban_ip` carries the field; a jail or a boot on the same screen
    must not sprout an empty word or a stray marker."""
    _both_outcomes(events)
    events.record("staff_action", actor="Wizzy", action="jail", target="Bob")
    out = _screen(_arch(world, clock, events, width=78))
    line = next(l for l in out.split("\n") if "Bob" in l)
    assert line.rstrip().endswith("Bob")
    assert "confirmed" not in line and "cancelled" not in line


def test_a_log_with_no_outcomes_is_unchanged(world, clock, events):
    """⚠ The column is added only when something fills it, so an ordinary
    moderation log renders byte for byte as it did before v1.33.1 — no empty
    fifth column, no trailing spaces, no header nobody needs."""
    for action, target in (("jail", "Bob"), ("boot", "Dave")):
        events.record("staff_action", actor="Wizzy", action=action,
                      target=target)
    out = _screen(_arch(world, clock, events, width=78))
    assert "Outcome" not in out
    header = next(l for l in out.split("\n") if "When" in l)
    assert header.rstrip().endswith("To")
    for line in out.split("\n"):
        assert line == line.rstrip(), repr(line)     # nothing trails


def test_narrow_shows_the_outcome_and_still_fits(world, clock, events):
    _both_outcomes(events)
    events.record("staff_action", actor="Wizzy", action="jail", target="Bob")
    out = _screen(_arch(world, clock, events, width=40))
    assert "confirmed" in out and "cancelled" in out
    for line in out.split("\n"):
        assert len(line) <= 40, repr(line)


def test_narrow_puts_the_outcome_on_the_time_line(world, clock, events):
    """⚠ Not on the lead line, where it reads more naturally: `ban_ip[24h]` is
    long enough that at 40 the lead line is already near full for exactly the
    rows that carry an outcome, and appending there wrapped a bare
    `(confirmed)` down to column zero — indistinguishable from the next entry.
    Indented under its own lead line it cannot wrap and cannot be misread."""
    _both_outcomes(events)
    a = _arch(world, clock, events, width=40)
    body = [l for l in _screen(a).split("\n") if l.strip() and "***" not in l]
    lead = next(l for l in body if "Mallory" in l)
    assert "confirmed" not in lead                    # not on the lead line
    detail = body[body.index(lead) + 1]
    assert detail.startswith(" ") and detail.rstrip().endswith("confirmed")
    assert ":" in detail                              # beside the time


def test_narrow_rows_without_an_outcome_are_a_bare_time(world, clock, events):
    _both_outcomes(events)
    events.record("staff_action", actor="Wizzy", action="jail", target="Bob")
    a = _arch(world, clock, events, width=40)
    body = [l for l in _screen(a).split("\n") if l.strip() and "***" not in l]
    lead = next(l for l in body if "Bob" in l)
    detail = body[body.index(lead) + 1]
    assert detail == detail.rstrip()
    assert "confirmed" not in detail and "cancelled" not in detail


# ---- end to end ------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("answer,shown,hidden", [
    ("y", "confirmed", "cancelled"),
    ("n", "cancelled", "confirmed"),
])
async def test_a_real_ban_ip_reads_back_with_its_outcome(tmp_path, answer,
                                                         shown, hidden):
    """The whole loop, over a real socket: the arch runs `.ban_ip`, answers the
    collateral confirm, then reads `.staff_actions` and is told which it was.
    IP unchanged — the target is the account name and no address is printed."""
    from test_ban_refinement import _cfg, _start, _login, send, collect, IP
    from conftest import seed_account

    srv, port = await _start(_cfg(tmp_path))
    try:
        seed_account(srv.accounts, "Generic", "correct horse battery")
        seed_account(srv.accounts, "Bob", "correct horse battery")
        srv.events.record("login", account="Bob", ip=IP)

        r, w = await _login(port, "Generic")
        await send(w, ".ban_ip Bob")
        await collect(r, timeout=0.5)
        await send(w, answer)
        await collect(r, timeout=0.5)

        await send(w, ".staff_actions")
        out = await collect(r, timeout=0.5)
        assert shown in out and hidden not in out
        assert "Outcome" in out and "Bob" in out
        assert IP not in out                       # the rule, still
        w.close()
    finally:
        await srv.aclose()
