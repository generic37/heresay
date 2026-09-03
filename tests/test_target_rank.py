# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The target-rank rule (v1.41.0) — you may act only on ranks strictly below
your own, and never on the arch.

⚠ **What this closes, and why it was reachable.** Every command checked whether
the ACTOR could run it; only some checked whether the TARGET could be acted on.
That gap was nearly unreachable while the ladder was three rungs and the account
powers were arch-only — and v1.38.0, which made every command assignable to any
rung, turned it into a live question. A focused audit found:

  * `.promote` had no target check at all: a holder assigned it below arch
    promoted THEMSELVES, one call per rung, to the seat under the arch;
  * `.resetpassword` refused only the arch (v1.33.0's fix), so a warden could
    reset a High Warden's password — with their OWN password — and walk in as
    them. v1.33.0's reasoning did not stop at the arch; it stopped wherever
    there is a rank above the holder, and multi-rung ladders put one there;
  * `.gag`/`.jail`/`.boot` had no check whatsoever: a staff could gag, jail and
    boot the config-named arch, and each other.

ONE rule, ONE helper (`_may_act_on`), because four near-identical checks is how
the fifth command gets added without one.
"""

import pathlib
import tempfile
from pathlib import Path

import pytest

from heresay.commands import MAILPURGE, dispatch
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"

# A ladder with room above and below the actor, which is what makes the rule
# testable at all: `citizen` < `warden` < `High Warden` < `Steward` (arch).
LADDER = ('[ranks]\norder = ["citizen", "warden", "High Warden", "Steward"]\n'
          'resident = "citizen"\nstaff = "warden"\n')


@pytest.fixture
def srv(tmp_path):
    (tmp_path / "theme.toml").write_text(LADDER, encoding="utf-8")
    # Every account power assigned DOWN to the staff rung — the configuration
    # v1.38.0 exists to make possible, and the one the audit probed.
    (tmp_path / "permissions.toml").write_text(
        '[commands]\npromote = "warden"\ndemote = "warden"\n'
        'resetpassword = "warden"\ndeleteuser = "warden"\nmailpurge = "warden"\n',
        encoding="utf-8")
    cfg = Config(
        owner_name="Generic", house_dir=str(REPO / "tests" / "fixtures" / "house"),
        theme_file=str(tmp_path / "theme.toml"),
        permissions_file=str(tmp_path / "permissions.toml"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        reload_poll_seconds=0, wrap_columns=1000,
    )
    s = TalkerServer(cfg)
    for name, rank in (("Generic", "Steward"), ("Chief", "High Warden"),
                       ("Warden1", "warden"), ("Peer", "warden"),
                       ("Cit", "citizen")):
        seed_account(s.accounts, name, "pw-one-two-three")
        if name != "Generic":
            s.accounts.set_rank(name, rank)
    return s


def _sess(srv, name, rank):
    return FakeSession(srv.world, name=name, rank=rank, state=srv.state,
                       accounts=srv.accounts, config=srv.config, mail=srv.mail,
                       events=srv.events, bans=srv.bans, theme=srv.theme)


def _run(session, line):
    session.clear()
    session.pending_target = None
    return dispatch(session, line), session.text


# ---------------------------------------------------------------------------
# 1. `.promote` — the RESULT may not exceed the caller's own rank
# ---------------------------------------------------------------------------

def test_a_holder_cannot_promote_themselves(srv):
    """⛔ The escalation the audit found: assigned to `warden`, one call reached
    the rung below arch.

    ⚠ v1.42.0 refuses it EARLIER and for a plainer reason — you are not below
    yourself, so you are not a target you may act on. The result cap that caught
    it in v1.41.0 is still there behind that.
    """
    w = _sess(srv, "Warden1", "warden")
    _, out = _run(w, ".promote Warden1")
    assert "below your own rank" in out
    assert srv.accounts.get("Warden1").rank == "warden"


def test_a_holder_cannot_promote_a_peer(srv):
    """The target rule, not the result cap: a peer is not below you."""
    w = _sess(srv, "Warden1", "warden")
    _, out = _run(w, ".promote Peer")             # peer is already a warden
    assert "below your own rank" in out
    assert srv.accounts.get("Peer").rank == "warden"


def test_the_result_cap_survives_behind_the_target_rule(srv):
    """⚠ The v1.41.0 cap (`result <= caller`) is now UNREACHABLE through
    `.promote`, and that is arithmetic rather than luck: a target strictly below
    you sits at most one rung under you, so one step can only ever reach your own
    level. It is kept as the second lock — if the target rule were ever loosened
    to at-or-below, this is what would still stop a rank appearing above the
    person who granted it. Pinned so a tidy-up has to be deliberate."""
    import inspect
    from heresay.commands import staff
    src = inspect.getsource(staff._step_rank)
    assert "step > 0 and target > mine" in src


def test_a_holder_can_promote_up_to_their_own_rank(srv):
    """The cap is `<=`, not `<`: lifting a junior to your own level is exactly
    what the power is for."""
    w = _sess(srv, "Warden1", "warden")
    _, out = _run(w, ".promote Cit")
    assert "Cit's rank is now warden" in out
    assert srv.accounts.get("Cit").rank == "warden"


def test_the_arch_still_cannot_reach_arch(srv):
    """The older cap stands: `.promote` tops out at `grantable_rank`, because
    arch is granted in config and never by a command."""
    arch = _sess(srv, "Generic", "Steward")
    srv.accounts.set_rank("Chief", "High Warden")
    _, out = _run(arch, ".promote Chief")
    assert srv.accounts.get("Chief").rank == "High Warden"     # unchanged
    assert "already" in out and "highest" in out


def test_demote_is_unaffected_and_floors_at_the_bottom(srv):
    w = _sess(srv, "Warden1", "warden")
    _run(w, ".demote Cit")
    assert srv.accounts.get("Cit").rank == "citizen"           # already the floor
    arch = _sess(srv, "Generic", "Steward")
    _run(arch, ".demote Chief")
    assert srv.accounts.get("Chief").rank == "warden"


# ---------------------------------------------------------------------------
# 2. The six account-acting commands — strictly below, never the arch
# ---------------------------------------------------------------------------

ONLINE = (".gag", ".jail", ".boot")
OFFLINE = (".mailpurge", ".resetpassword", ".deleteuser")


@pytest.mark.parametrize("verb", ONLINE + OFFLINE)
def test_the_arch_is_never_a_valid_target(srv, verb):
    w = _sess(srv, "Warden1", "warden")
    _sess(srv, "Generic", "Steward")              # connect them, for the online three
    _, out = _run(w, f"{verb} Generic")
    assert "can't" in out and "Steward" in out
    assert w.pending_target is None


@pytest.mark.parametrize("verb", ONLINE + OFFLINE)
def test_a_target_above_the_caller_is_refused(srv, verb):
    w = _sess(srv, "Warden1", "warden")
    _sess(srv, "Chief", "High Warden")
    _, out = _run(w, f"{verb} Chief")
    assert "below your own rank" in out
    assert w.pending_target is None


@pytest.mark.parametrize("verb", ONLINE + OFFLINE)
def test_a_PEER_is_refused_too(srv, verb):
    """⚠ Strictly below, not at-or-below. Peer moderation is a policy a house
    should choose deliberately, not inherit from an omission."""
    w = _sess(srv, "Warden1", "warden")
    _sess(srv, "Peer", "warden")
    _, out = _run(w, f"{verb} Peer")
    assert "below your own rank" in out
    assert w.pending_target is None


def test_a_strictly_below_target_is_allowed(srv):
    w = _sess(srv, "Warden1", "warden")
    cit = _sess(srv, "Cit", "citizen")
    _run(w, ".gag Cit")
    assert cit.resident.gagged is True
    _run(w, ".jail Cit")
    assert srv.state.is_jailed("Cit") is True
    for verb in OFFLINE:
        _, out = _run(w, f"{verb} Cit")
        assert w.pending_target == "Cit", verb          # staged, gates still to come


def test_an_arch_caller_keeps_full_reach(srv):
    """"Strictly below the top" is everyone else, so the arch loses nothing."""
    arch = _sess(srv, "Generic", "Steward")
    chief = _sess(srv, "Chief", "High Warden")
    _run(arch, ".gag Chief")
    assert chief.resident.gagged is True
    _run(arch, ".jail Chief")
    assert srv.state.is_jailed("Chief") is True
    for verb in OFFLINE:
        _run(arch, f"{verb} Chief")
        assert arch.pending_target == "Chief", verb


def test_the_arch_can_still_free_itself(srv):
    """⚠ THE ESCAPE HATCH, and since v1.42.0 the only thing `.free` leaves
    ungated: strictly-below would refuse the arch freeing THEMSELVES (nobody is
    below the top), and a `.jail` that cannot be undone is a worse failure than
    a staff undoing one. Freeing anyone ELSE takes the rule like the rest."""
    arch = _sess(srv, "Generic", "Steward")
    srv.state.set_jailed("Generic")
    arch.resident.jailed = True
    arch.resident.room_id = srv.world.jail_room
    _, out = _run(arch, ".free Generic")
    assert "You free Generic" in out
    assert srv.state.is_jailed("Generic") is False


def test_ungag_is_gated_the_same_way(srv):
    """The mirror of `.gag`: without it a staff could undo a owner's gag,
    which is acting on a decision above their rank."""
    arch = _sess(srv, "Generic", "Steward")
    w = _sess(srv, "Warden1", "warden")
    chief = _sess(srv, "Chief", "High Warden")
    _run(arch, ".gag Chief")
    _, out = _run(w, ".ungag Chief")
    assert "below your own rank" in out
    assert chief.resident.gagged is True


# ---------------------------------------------------------------------------
# 3. `.mailpurge` — the caller's own password
# ---------------------------------------------------------------------------

def test_mailpurge_stages_rather_than_purging(srv):
    """⛔ It destroys someone's correspondence — the one thing the covenant lets
    the server keep — and a single mistyped line used to finish it."""
    srv.mail.deliver("Cit", "Someone", "a letter")
    w = _sess(srv, "Warden1", "warden")
    result, _ = _run(w, ".mailpurge Cit")
    assert result == MAILPURGE
    assert w.pending_target == "Cit"
    assert srv.mail.count("Cit") == 1              # nothing gone yet


# ⚠ The password gate itself is exercised over the REAL socket door, beside the
# other two staged commands — see tests/test_arch_accounts.py. A gate that only
# a fake has ever answered is a gate nobody has tested.


# ---------------------------------------------------------------------------
# 4. Preserved — the always-on gates and the arch invariants
# ---------------------------------------------------------------------------

def test_the_always_on_gates_are_untouched(srv):
    """§5: the retype + own-password gates are rank-INDEPENDENT and live in the
    session loop, so no permissions assignment can reach them. The target-rank
    rule is an addition, not a replacement."""
    import inspect
    for meth in (srv._deleteuser, srv._resetpassword):
        src = inspect.getsource(meth)
        assert "averify(session.resident.name" in src      # the CALLER's own
    assert "casefold" in inspect.getsource(srv._deleteuser)  # retype the target


def test_ban_and_ban_ip_still_refuse_the_arch(srv):
    """They always did; v1.42.0 layers the rest of the ladder on top —
    see tests/test_target_rank_universal.py."""
    w = _sess(srv, "Warden1", "warden")
    for verb in (".ban", ".ban_ip"):
        _, out = _run(w, f"{verb} Generic")
        assert "Steward" in out


def test_commons_is_unaffected(tmp_path):
    """⛔ On the shipped three-rung ladder the arch is the only holder of these
    commands and their rank is the top, so every new check binds nothing that
    was not already bound."""
    cfg = Config(
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
        reload_poll_seconds=0, wrap_columns=1000)
    s = TalkerServer(cfg)
    for n in ("Generic", "Wizzy", "Bob"):
        seed_account(s.accounts, n, "pw-one-two-three")
    s.accounts.set_rank("Wizzy", "staff")
    arch = _sess(s, "Generic", "owner")
    _sess(s, "Bob", "resident")
    staff = _sess(s, "Wizzy", "staff")

    _run(arch, ".promote Wizzy")           # arch promotes: unchanged
    assert s.accounts.get("Wizzy").rank == "staff"    # already grantable's top
    _run(staff, ".gag Bob")               # staff moderates a resident: unchanged
    assert s.world.get_resident("Bob").gagged is True
    _, out = _run(staff, ".gag Generic")  # ...but not the owner
    assert "owner" in out
