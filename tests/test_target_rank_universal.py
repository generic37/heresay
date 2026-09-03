# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The target-rank rule is UNIVERSAL (v1.42.0) — every person-acting command.

v1.41.0 put `_may_act_on` on seven commands and left six alone: `.ban`,
`.ban_ip`, `.unban`, `.unban_ip`, `.promote` and `.demote` still checked only
that the target was not the arch. The principle does not have a reason to stop
there — **acting on someone at or above your own rank is the thing being ruled
out**, and a ban is as much an act on a person as a gag is. So a staff can no
longer ban, unban, promote or demote a peer staff or anyone above them.

Two things here are more than a repeat of v1.41.0's shape:

  * **`.free` gains the rule and keeps ONE exception** — the arch freeing
    THEMSELVES, which is the escape hatch that stops a jailed arch being locked
    in. Freeing anybody else is strictly-below like the rest, so a staff cannot
    undo a release decision made above them (the `.ungag` argument exactly).

  * **`.ban_ip` cannot take staff down INDIRECTLY.** Naming a resident who
    shares an address with a staff would otherwise ban the staff too, and the
    target-rank rule cannot see it — the named account really is below the
    caller. So the whole affected set is checked, and a non-arch caller is
    refused ANONYMOUSLY: no name, no count, nothing identifying. That silence is
    load-bearing. A named refusal would make `.ban_ip` an oracle for "who shares
    an address with whom" — and it holds even when the staff account caught is
    the caller's OWN, so nobody can confirm their own co-tenancy by probing.
"""

import asyncio
import inspect
from pathlib import Path

import pytest

from heresay.commands import BAN_IP, dispatch
from heresay.commands import staff as S
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"

# Room above and below the actor, which is what makes the rule testable at all.
LADDER = ('[ranks]\norder = ["citizen", "warden", "High Warden", "Steward"]\n'
          'resident = "citizen"\nstaff = "warden"\n')


def _cfg(tmp_path, **over):
    base = dict(
        owner_name="Generic", house_dir=str(REPO / "tests" / "fixtures" / "house"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        reload_poll_seconds=0, wrap_columns=1000,
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
def srv(tmp_path):
    (tmp_path / "theme.toml").write_text(LADDER, encoding="utf-8")
    # Every one of the six assigned DOWN to the staff rung — the configuration
    # v1.38.0 exists to make possible, and the only one in which the rule can be
    # observed at all.
    (tmp_path / "permissions.toml").write_text(
        '[commands]\npromote = "warden"\ndemote = "warden"\n'
        'ban = "warden"\nban_ip = "warden"\nunban = "warden"\n'
        'unban_ip = "warden"\nfree = "warden"\n', encoding="utf-8")
    s = TalkerServer(_cfg(tmp_path, theme_file=str(tmp_path / "theme.toml"),
                          permissions_file=str(tmp_path / "permissions.toml")))
    for name, rank in (("Generic", "Steward"), ("Chief", "High Warden"),
                       ("Warden1", "warden"), ("Peer", "warden"),
                       ("Cit", "citizen")):
        seed_account(s.accounts, name, "pw-one-two-three")
        if name != "Generic":
            s.accounts.set_rank(name, rank)
    return s


def _sess(srv, name, rank, ip="127.0.0.1"):
    s = FakeSession(srv.world, name=name, rank=rank, state=srv.state,
                    accounts=srv.accounts, config=srv.config, mail=srv.mail,
                    events=srv.events, bans=srv.bans, theme=srv.theme,
                    ban_history=srv.ban_history)
    # ⚠ Distinct addresses on purpose. Everyone on loopback would make the
    # staff-collision guard fire on every `.ban_ip` in this file — which is
    # correct behaviour and the wrong thing to be testing here.
    s.peer_ip = ip
    return s


def _run(session, line):
    session.clear()
    session.pending_target = None
    session.pending_ban_ip = None
    return dispatch(session, line), session.text


# ---------------------------------------------------------------------------
# 1. The six — strictly below, never the arch
# ---------------------------------------------------------------------------

SIX = (".ban", ".ban_ip", ".unban", ".unban_ip", ".promote", ".demote")


@pytest.mark.parametrize("verb", SIX)
def test_the_arch_is_never_a_valid_target(srv, verb):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Generic", "Steward", ip="203.0.113.2")
    _, out = _run(w, f"{verb} Generic")
    assert "Steward" in out                    # named as the rank, and refused
    assert "can't" in out or "fixed in config" in out
    assert w.pending_ban_ip is None
    assert not srv.bans.list_active(now=0.0)
    # ⚠ The arch's STORED rank is the floor — the top rank is an identity from
    # config, not a rung they hold — so "unchanged" is the assertion, not "the
    # top". That distinction is exactly why `_may_act_on` checks the NAME too.
    assert srv.accounts.get("Generic").rank == srv.theme.resident_rank


@pytest.mark.parametrize("verb", SIX)
def test_a_target_above_the_caller_is_refused(srv, verb):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Chief", "High Warden", ip="203.0.113.3")
    _, out = _run(w, f"{verb} Chief")
    assert "below your own rank" in out
    assert w.pending_ban_ip is None
    assert not srv.bans.list_active(now=0.0)
    assert srv.accounts.get("Chief").rank == "High Warden"


@pytest.mark.parametrize("verb", SIX)
def test_a_PEER_is_refused_too(srv, verb):
    """⚠ Strictly below, not at-or-below. A staff moderating another staff is
    a policy a house should choose, not one it inherits from an omission."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Peer", "warden", ip="203.0.113.4")
    _, out = _run(w, f"{verb} Peer")
    assert "below your own rank" in out
    assert w.pending_ban_ip is None
    assert not srv.bans.list_active(now=0.0)
    assert srv.accounts.get("Peer").rank == "warden"


def test_a_strictly_below_target_is_allowed_for_all_six(srv):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Cit", "citizen", ip="203.0.113.5")

    _, out = _run(w, ".ban Cit")
    assert "Banned Cit" in out
    _, out = _run(w, ".unban Cit")
    assert "Unbanned Cit" in out

    result, _ = _run(w, ".ban_ip Cit")
    assert result == BAN_IP and w.pending_ban_ip[0] == "Cit"

    srv.bans.ban("203.0.113.5", "ip", hours=1, now=0.0)
    _, out = _run(w, ".unban_ip Cit")
    assert "Lifted the IP ban" in out

    _, out = _run(w, ".promote Cit")
    assert srv.accounts.get("Cit").rank == "warden"      # up to the caller's level

    # `.demote` needs a caller with a rung to spare above the target: a warden's
    # only strictly-below target is a citizen, who is already on the floor.
    chief = _sess(srv, "Chief", "High Warden", ip="203.0.113.3")
    _run(chief, ".demote Cit")
    assert srv.accounts.get("Cit").rank == "citizen"


def test_an_arch_caller_keeps_full_reach(srv):
    """"Strictly below the top" is everyone else, so the arch loses nothing."""
    arch = _sess(srv, "Generic", "Steward", ip="203.0.113.2")
    _sess(srv, "Chief", "High Warden", ip="203.0.113.3")
    _, out = _run(arch, ".ban Chief")
    assert "Banned Chief" in out
    _, out = _run(arch, ".unban Chief")
    assert "Unbanned Chief" in out
    _run(arch, ".demote Chief")
    assert srv.accounts.get("Chief").rank == "warden"
    _run(arch, ".promote Chief")
    assert srv.accounts.get("Chief").rank == "High Warden"


# ---------------------------------------------------------------------------
# 1a. `.promote` / `.demote` semantics
# ---------------------------------------------------------------------------

def test_promote_raises_a_below_target_up_to_the_callers_level_and_no_further(srv):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _run(w, ".promote Cit")
    assert srv.accounts.get("Cit").rank == "warden"      # the caller's own level
    _, out = _run(w, ".promote Cit")                     # ...and now a peer
    assert "below your own rank" in out
    assert srv.accounts.get("Cit").rank == "warden"


def test_demote_cannot_reach_a_peer_or_above(srv):
    """⚠ The half v1.41.0's result cap could never have caught: demoting is
    always downward, so no cap on the RESULT says anything about whether you
    were entitled to touch that person."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    for name in ("Peer", "Chief"):
        _, out = _run(w, f".demote {name}")
        assert "below your own rank" in out
    assert srv.accounts.get("Peer").rank == "warden"
    assert srv.accounts.get("Chief").rank == "High Warden"


def test_promote_still_cannot_reach_arch(srv):
    """The older cap stands beside the new one: arch is granted in config."""
    arch = _sess(srv, "Generic", "Steward", ip="203.0.113.2")
    _, out = _run(arch, ".promote Chief")
    assert srv.accounts.get("Chief").rank == "High Warden"
    assert "already" in out and "highest" in out


# ---------------------------------------------------------------------------
# 2. `.free` — the one command with a self exception
# ---------------------------------------------------------------------------

def _jail(srv, sess, name):
    srv.state.set_jailed(name)
    sess.resident.jailed = True
    sess.resident.room_id = srv.world.jail_room


def test_freeing_someone_else_obeys_strictly_below(srv):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    chief = _sess(srv, "Chief", "High Warden", ip="203.0.113.3")
    _jail(srv, chief, "Chief")
    _, out = _run(w, ".free Chief")
    assert "below your own rank" in out
    assert srv.state.is_jailed("Chief") is True


def test_freeing_a_peer_is_refused(srv):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    peer = _sess(srv, "Peer", "warden", ip="203.0.113.4")
    _jail(srv, peer, "Peer")
    _, out = _run(w, ".free Peer")
    assert "below your own rank" in out
    assert srv.state.is_jailed("Peer") is True


def test_freeing_someone_below_still_works(srv):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    cit = _sess(srv, "Cit", "citizen", ip="203.0.113.5")
    _jail(srv, cit, "Cit")
    _, out = _run(w, ".free Cit")
    assert "You free Cit" in out
    assert srv.state.is_jailed("Cit") is False


def test_a_non_arch_cannot_free_themselves(srv):
    """Otherwise `.jail` would be undoable by anyone holding `.free` — which,
    once the command is assignable, is most of the staff."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _jail(srv, w, "Warden1")
    _, out = _run(w, ".free Warden1")
    assert "can't free yourself" in out.lower()
    assert srv.state.is_jailed("Warden1") is True


def test_the_arch_can_still_free_itself(srv):
    """⚠ THE ESCAPE HATCH. Strictly-below would refuse it (nobody is below the
    top), and a `.jail` that cannot be undone is the worse failure."""
    arch = _sess(srv, "Generic", "Steward", ip="203.0.113.2")
    _jail(srv, arch, "Generic")
    _, out = _run(arch, ".free Generic")
    assert "You free Generic" in out
    assert srv.state.is_jailed("Generic") is False


# ---------------------------------------------------------------------------
# 3. `.ban_ip` must never ban staff — directly or indirectly
# ---------------------------------------------------------------------------

SHARED = "198.51.100.20"


def test_a_non_arch_is_refused_anonymously_when_a_peer_shares_the_address(srv):
    """⛔ The indirect route: the NAMED account is a resident and perfectly
    bannable, but the address also carries a staff."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Peer", "warden", ip=SHARED)
    _sess(srv, "Cit", "citizen", ip=SHARED)
    result, out = _run(w, ".ban_ip Cit")
    assert result is None
    assert "would affect staff" in out
    assert w.pending_ban_ip is None
    # ⛔ ANONYMOUS. No name, no count, nothing that says which account — the
    # refusal must not become the lookup the command refuses to perform.
    assert "Peer" not in out
    assert "1" not in out and "one" not in out.lower()


def test_the_refusal_stays_anonymous_when_the_staff_caught_is_the_caller(srv):
    """⚠ The probe this closes: if the notice were different when it is YOU, a
    staff member could learn their own co-tenancy — and then, by elimination, other
    people's — one `.ban_ip` at a time."""
    w = _sess(srv, "Warden1", "warden", ip=SHARED)
    _sess(srv, "Cit", "citizen", ip=SHARED)
    result, mine = _run(w, ".ban_ip Cit")
    assert result is None and "would affect staff" in mine
    assert "Warden1" not in mine

    # ...and byte for byte what a staff who is NOT on the address is told.
    other = _sess(srv, "Peer", "warden", ip="203.0.113.9")
    _sess(srv, "Chief", "High Warden", ip=SHARED)   # some staff, not the caller
    _, theirs = _run(other, ".ban_ip Cit")
    assert mine == theirs


def test_naming_a_staff_account_directly_is_refused_by_the_rank_rule_first(srv):
    """Direct is the target-rank rule's job; the collision guard is for what it
    cannot see."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Chief", "High Warden", ip=SHARED)
    _, out = _run(w, ".ban_ip Chief")
    assert "below your own rank" in out


def test_an_address_with_no_staff_on_it_behaves_exactly_as_before(srv):
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Cit", "citizen", ip=SHARED)
    result, out = _run(w, ".ban_ip Cit")
    assert result == BAN_IP
    assert w.pending_ban_ip == ("Cit", SHARED, srv.config.ban_ip_default_hours)
    assert "would affect staff" not in out


def test_the_archs_own_presence_on_the_address_never_triggers_it(srv):
    """The arch is exempt from IP enforcement at login, so they are not among
    the affected — counting them would invent collateral that cannot happen."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Generic", "Steward", ip=SHARED)
    _sess(srv, "Cit", "citizen", ip=SHARED)
    result, out = _run(w, ".ban_ip Cit")
    assert result == BAN_IP
    assert "would affect staff" not in out
    assert S.ban_ip_staff_caught(w, SHARED, "Cit") == []


def test_the_caught_set_includes_the_named_account_itself(srv):
    """"Affected" is the named account PLUS the co-tenants — a ban placed on
    someone offline and unseen on the address still lands on them."""
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    assert S.ban_ip_staff_caught(w, "198.51.100.77", "Chief") == ["Chief"]
    assert S.ban_ip_staff_caught(w, "198.51.100.77", "Cit") == []


def test_offline_staff_seen_on_the_address_count_too(srv):
    """The window's whole purpose: someone not here now is still shut out."""
    srv.events.record("login", account="Chief", ip=SHARED)
    w = _sess(srv, "Warden1", "warden", ip="203.0.113.1")
    _sess(srv, "Cit", "citizen", ip=SHARED)
    result, out = _run(w, ".ban_ip Cit")
    assert result is None and "would affect staff" in out
    assert "Chief" not in out


# --- the arch's branch: named, and a second question -----------------------

async def _collect(r, timeout=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def _login(port, name, pw="correct horse battery"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r); await _send(w, name)
    await _collect(r); await _send(w, pw)
    await _collect(r, timeout=0.5)
    return r, w


async def _start(tmp_path):
    """A real house on the shipped ladder: arch, one staff, one resident, all
    on loopback — which is exactly the collision."""
    cfg = _cfg(tmp_path, plain_host="127.0.0.1", plain_port=0,
               ws_host="127.0.0.1", ws_port=0,
               cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
               world_dir=str(WORLD), house_dir=str(REPO / "tests" / "fixtures" / "house"))
    srv = TalkerServer(cfg)
    for n in ("Generic", "Wizzy", "Bob"):
        seed_account(srv.accounts, n, "correct horse battery")
    srv.accounts.set_rank("Wizzy", "staff")
    await srv.start()
    return srv, srv._servers[0].sockets[0].getsockname()[1]


@pytest.mark.asyncio
async def test_the_arch_is_told_WHO_and_asked_again(tmp_path):
    """The arch is the one rank allowed to be told who shares an address, so
    they are — by name — and asked a second question, because "this also takes a
    staff off the door" is a different decision from "this catches four
    housemates" and one y/n cannot mean both."""
    srv, port = await _start(tmp_path)
    try:
        rb, wb = await _login(port, "Bob")
        rw, ww = await _login(port, "Wizzy")
        ra, wa = await _login(port, "Generic")
        await _send(wa, ".ban_ip Bob")
        shown = await _collect(ra, timeout=0.6)
        assert "would also ban staff: Wizzy" in shown
        assert "Continue?" in shown
        assert not srv.bans.is_banned_ip("127.0.0.1")    # nothing yet

        await _send(wa, "y")
        assert "Ban it for" in await _collect(ra, timeout=0.6)
        await _send(wa, "y")
        assert "Banned the address" in await _collect(ra, timeout=0.6)
        assert srv.bans.is_banned_ip("127.0.0.1")
        wa.close(); ww.close(); wb.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_declining_the_staff_question_places_nothing_and_is_audited(tmp_path):
    """⚠ Recorded either way, like the main prompt: being SHOWN who shares an
    address is itself the act worth auditing."""
    srv, port = await _start(tmp_path)
    try:
        rb, wb = await _login(port, "Bob")
        rw, ww = await _login(port, "Wizzy")
        ra, wa = await _login(port, "Generic")
        await _send(wa, ".ban_ip Bob")
        await _collect(ra, timeout=0.6)
        await _send(wa, "n")
        assert "Never mind" in await _collect(ra, timeout=0.6)
        assert not srv.bans.is_banned_ip("127.0.0.1")
        rows = [e for e in srv.events.read(types=("staff_action",))
                if e["action"].startswith("ban_ip")]
        assert rows and rows[-1]["outcome"] == "cancelled"
        assert rows[-1]["target"] == "Bob"          # the account, never the address
        wa.close(); ww.close(); wb.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_keeper_gets_the_anonymous_refusal_over_the_real_door(tmp_path):
    """The fake and the real door must agree about a refusal this quiet."""
    srv, port = await _start(tmp_path)
    try:
        rb, wb = await _login(port, "Bob")
        rw, ww = await _login(port, "Wizzy")
        await _send(ww, ".ban_ip Bob")
        shown = await _collect(rw, timeout=0.6)
        assert "would affect staff" in shown
        assert "Wizzy" not in shown and "Continue?" not in shown
        assert not srv.bans.is_banned_ip("127.0.0.1")
        # It never staged, so no attempt was recorded either.
        assert not [e for e in srv.events.read(types=("staff_action",))
                    if e["action"].startswith("ban_ip")]
        wb.close(); ww.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 3a. Completeness — the rule cannot quietly stop covering a command
# ---------------------------------------------------------------------------

def test_every_gated_command_that_names_a_person_checks_the_target():
    """⛔ THE GUARD ON THE RULE ITSELF, and the reason v1.42.0 exists: v1.41.0's
    list was hand-written, so the six it missed were missed silently. This
    derives the list from the REGISTRY instead — every command seeded above the
    floor whose usage takes a `<name>` — and fails on any that does not reach
    `_may_act_on`. A new staff verb that acts on a person now has to be gated or
    to argue its way onto EXEMPT, in the open.
    """
    import heresay.commands as C
    from heresay.commands.registry import COMMANDS

    # ⛔ THE EXEMPTION SET IS EXPLICIT AND REASONED (widened v1.47.0, audit F1).
    # The guarantee this test carries is not "the commands we happened to think
    # of reach `_may_act_on`" — it is **every person-acting command reaches it
    # EXCEPT this named set, each with its reason written down.** A silent skip
    # is exactly what F1 was: `.logoutall` affects every other account, takes no
    # `<name>`, and so fell through a criterion that only looked at usage
    # strings. It is exempt now because somebody decided it should be, in
    # writing, rather than because nothing noticed it.
    EXEMPT = {
        # `.theme` is the browser's colour scheme; its `<name>` is a palette,
        # not a person.
        "theme",
        # `.logoutall` intentionally affects ALL non-arch sessions INCLUDING the
        # caller; it is not a per-target moderation command. Strictly-below would
        # spare exactly the people "close the house" is meant to clear — and the
        # caller's own inclusion is what stops it being usable to remove a rival
        # and remain. See TalkerServer._logoutall and test_logoutall.py.
        "logoutall",
        # `.report <name>` names a person, but it is an INFORMATIONAL REPORT,
        # not an action on the target: nothing happens to them, and the name
        # travels as a subject line into an encrypted mailbox. Requiring
        # strictly-below would invert the command — the people most likely to
        # need reporting are the ones a resident cannot outrank.
        #
        # ⚠ Named even though the floor-anchor check above ALREADY skips it, and
        # that is the point rather than belt-and-braces: being skipped by a
        # criterion is exactly what F1 was. If `.report` is ever re-seeded above
        # the floor, this line is what keeps the decision a decision instead of
        # a silent consequence of a rank change.
        "report",
    }

    checked = []
    for spec in COMMANDS:
        usage = spec.usage if isinstance(spec.usage, (list, tuple)) else (spec.usage,)
        if spec.anchor == "resident" or spec.name in EXEMPT:
            continue
        if not any("<name" in u for u in usage if u):
            continue
        handler = getattr(C, f"cmd_{spec.name}", None)
        assert handler is not None, spec.name
        src = inspect.getsource(handler)
        if "_step_rank" in src:                     # .promote / .demote delegate
            src += inspect.getsource(S._step_rank)
        assert "_may_act_on" in src, spec.name
        checked.append(spec.name)

    # The list is derived, but its SIZE is pinned: a command dropping out of it
    # would otherwise be a test that quietly checks less.
    assert len(checked) == 14, sorted(checked)
    # ...and the exemptions are pinned too, so growing the set is a deliberate
    # edit with a reason beside it rather than a quiet way past this test.
    assert EXEMPT == {"theme", "logoutall", "report"}


# ---------------------------------------------------------------------------
# 4. VERIFY: the arch's address is never refused at login
# ---------------------------------------------------------------------------

def test_the_login_path_actually_consults_the_exemption(tmp_path):
    """⚠ Spec'd in v1.32/v1.33; verified here as WIRED. The staff-collision
    guard leans on it — the arch is left out of the affected set precisely
    because an IP ban cannot shut them out — so if the exemption were not
    consulted at the door, that omission would be wrong."""
    src = inspect.getsource(TalkerServer._serve)
    assert "is_banned_ip" in src and "_ip_ban_exempt" in src


@pytest.mark.asyncio
async def test_the_arch_walks_in_through_a_ban_on_their_own_address(tmp_path):
    """The end-to-end proof, not the source read: ban loopback, then log the
    arch in through it."""
    srv, port = await _start(tmp_path)
    try:
        srv.bans.ban("127.0.0.1", "ip", hours=24)
        assert srv.bans.is_banned_ip("127.0.0.1")
        r, w = await asyncio.open_connection("127.0.0.1", port)
        first = await _collect(r, timeout=0.6)
        assert "refused" not in first.lower()
        await _send(w, "Generic")
        await _collect(r)
        await _send(w, "correct horse battery")
        assert "atrium" in (await _collect(r, timeout=0.6)).lower()
        w.close()
    finally:
        await srv.aclose()
