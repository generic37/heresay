# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.logoutall` — close the house, and go with it (v1.47.0, audit F1).

⚠ **This is the one command that is deliberately NOT the target-rank rule**, and
the reason is worth stating before the tests: `.logoutall` is not aimed at a
person. It means *close the house*. A version of it that spared everyone at or
above the caller would leave exactly the people it exists to clear, so it takes
the opposite shape — **it hits every rank**, and ⛔ **a non-arch caller goes with
them.**

That inclusion is what makes it safe to leave outside `_may_act_on`. Before
v1.47.0 the caller was always spared, so a staff handed this command could
disconnect their seniors and keep the room to themselves. Now the only person
who can run it and still be standing is the arch — who is also the one account
it can never disconnect, because an arch shut out of their own house has no way
back in.

The completeness sweep in test_target_rank_universal.py names `.logoutall` in
its EXPLICIT exemption set for exactly this reason. ⚠ Being exempt is now a
decision somebody wrote down, not a criterion nothing happened to match — which
is what F1 actually was.
"""

import asyncio
import pathlib

import pytest

from heresay.config import Config
from heresay.server import TalkerServer

from conftest import seed_account

REPO = pathlib.Path(__file__).resolve().parent.parent
HOUSE = REPO / "tests" / "fixtures" / "house"
LADDER = ('[ranks]\norder = ["citizen", "warden", "High Warden", "Steward"]\n'
          'resident = "citizen"\nstaff = "warden"\n')


def _cfg(tmp_path, **over):
    base = dict(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        owner_name="Generic", house_dir=str(HOUSE),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        forbidden_names_file=str(tmp_path / "forbidden_names.txt"),
        control_socket_file=str(tmp_path / "control.sock"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000, reload_poll_seconds=0,
        max_conns_per_ip=20, churn_max_in_window=60)
    base.update(over)
    return Config(**base)


async def _collect(r, t=0.35):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(4096), t)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _login(port, name, pw="correct horse battery"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r)
    w.write(name.encode() + b"\n"); await w.drain()
    await _collect(r)
    w.write(pw.encode() + b"\n"); await w.drain()
    out = await _collect(r)
    # A custom ladder changes {arch} in the agreement, so the seeded hash no
    # longer matches and re-consent is asked. Not what these tests are about.
    if "Do you agree" in out:
        w.write(b"y\n"); await w.drain()
        await _collect(r)
    return r, w


async def _house(tmp_path, assign_to=None):
    """A four-rung house, optionally with `.logoutall` handed below arch."""
    (tmp_path / "theme.toml").write_text(LADDER, encoding="utf-8")
    if assign_to:
        (tmp_path / "permissions.toml").write_text(
            f'[commands]\nlogoutall = "{assign_to}"\n', encoding="utf-8")
    srv = TalkerServer(_cfg(tmp_path, theme_file=str(tmp_path / "theme.toml")))
    await srv.start()
    for name, rank in (("Generic", "Steward"), ("Chief", "High Warden"),
                       ("W1", "warden"), ("Cit", "citizen")):
        seed_account(srv.accounts, name, "correct horse battery")
        if name != "Generic":
            srv.accounts.set_rank(name, rank)
    return srv, srv._servers[0].sockets[0].getsockname()[1]


def _connected(srv):
    return sorted(r.name for r in srv.world.all_connected()
                  if r.session is not None)


# ---------------------------------------------------------------------------
# 1. ⛔ A non-arch caller goes with everyone else
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_non_arch_caller_logs_THEMSELVES_out_too(tmp_path):
    """⛔ THE F1 FIX. Before this, the caller was spared — so a warden handed
    `.logoutall` could disconnect a High Warden and keep the house. Now running
    it costs them their own session, which is what makes "close the house"
    unusable as "remove a rival"."""
    srv, port = await _house(tmp_path, assign_to="warden")
    try:
        rc, wc = await _login(port, "Chief")     # a rank ABOVE the caller
        ri, wi = await _login(port, "Cit")
        rw, ww = await _login(port, "W1")        # the caller: staff
        assert _connected(srv) == ["Chief", "Cit", "W1"]

        ww.write(b".logoutall\n"); await ww.drain()
        asked = await _collect(rw)
        assert "including yourself" in asked, asked
        ww.write(b"y\n"); await ww.drain()
        await _collect(rw)
        await asyncio.sleep(0.4)

        assert _connected(srv) == [], _connected(srv)   # the caller too
        for w in (wc, wi, ww):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_prompt_counts_the_caller_and_says_so(tmp_path):
    """The number has to include them, or the sentence is a lie about what is
    about to happen."""
    srv, port = await _house(tmp_path, assign_to="warden")
    try:
        rc, wc = await _login(port, "Chief")
        ri, wi = await _login(port, "Cit")
        rw, ww = await _login(port, "W1")
        ww.write(b".logoutall\n"); await ww.drain()
        asked = await _collect(rw)
        # Chief + Cit + the caller = 3, not 2.
        assert "Log out 3 account(s), including yourself? (y/n)" in asked, asked
        # ⚠ "account(s)", not "resident(s)": it reaches every non-arch rank.
        assert "resident(s)" not in asked
        ww.write(b"n\n"); await ww.drain()
        assert "Never mind" in await _collect(rw)
        assert _connected(srv) == ["Chief", "Cit", "W1"]   # nothing happened
        for w in (wc, wi, ww):
            w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 2. The arch — as caller, and as target
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_arch_running_it_STAYS_and_clears_the_rest(tmp_path):
    srv, port = await _house(tmp_path)
    try:
        rc, wc = await _login(port, "Chief")
        ri, wi = await _login(port, "Cit")
        ra, wa = await _login(port, "Generic")
        wa.write(b".logoutall\n"); await wa.drain()
        asked = await _collect(ra)
        assert "Log out 2 account(s)? (y/n)" in asked, asked
        assert "including yourself" not in asked
        wa.write(b"y\n"); await wa.drain()
        assert "Logged out 2 account(s)." in await _collect(ra)
        await asyncio.sleep(0.4)
        assert _connected(srv) == ["Generic"]
        for w in (wc, wi, wa):
            w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_arch_is_never_a_TARGET_whoever_runs_it(tmp_path):
    """The older half of the rule, unchanged: an arch shut out of their own
    house has no way back in."""
    srv, port = await _house(tmp_path, assign_to="warden")
    try:
        ra, wa = await _login(port, "Generic")
        rw, ww = await _login(port, "W1")
        ww.write(b".logoutall\n"); await ww.drain()
        asked = await _collect(rw)
        assert "Log out 1 account(s), including yourself?" in asked, asked
        ww.write(b"y\n"); await ww.drain()
        await _collect(rw)
        await asyncio.sleep(0.4)
        assert _connected(srv) == ["Generic"]      # the arch stands
        for w in (wa, ww):
            w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 3. The fact, and the covenant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_fact_is_recorded_BEFORE_the_caller_is_dropped(tmp_path):
    """⚠ Order matters: a non-arch caller is about to disconnect themselves, and
    the audit row must not depend on the actor still being there to write it.
    A COUNT, never a roll call."""
    srv, port = await _house(tmp_path, assign_to="warden")
    try:
        rc, wc = await _login(port, "Chief")
        rw, ww = await _login(port, "W1")
        ww.write(b".logoutall\n"); await ww.drain()
        await _collect(rw)
        ww.write(b"y\n"); await ww.drain()
        await _collect(rw)
        await asyncio.sleep(0.4)
        rows = [e for e in srv.events.read(types=("staff_action",))
                if e["action"] == "logoutall"]
        assert len(rows) == 1
        assert rows[0]["actor"] == "W1"
        assert rows[0]["target"] == "2 session(s)"      # includes the caller
        assert "Chief" not in str(rows[0])              # no roll call
        for w in (wc, ww):
            w.close()
    finally:
        await srv.aclose()


def test_the_sweep_names_logoutall_as_an_EXPLICIT_exemption():
    """⛔ The shape of the guarantee, which is the real F1 fix. It is not "the
    commands somebody thought of reach `_may_act_on`" — it is "every
    person-acting command reaches it EXCEPT this named, reasoned set". A silent
    skip is what let `.logoutall` through."""
    import inspect
    from tests import test_target_rank_universal as T   # noqa: F401
    src = inspect.getsource(
        T.test_every_gated_command_that_names_a_person_checks_the_target)
    assert '"logoutall"' in src
    # A reason, not just a name — the exemption has to say WHY beside itself.
    # Flatten the comment block: the reason wraps across lines, and each
    # continuation carries its own "#", which would otherwise land mid-phrase.
    flat = " ".join(src.replace("#", " ").split())
    assert "INCLUDING the caller" in flat
    assert "not a per-target moderation command" in flat
    assert 'EXEMPT == {"theme", "logoutall", "report"}' in src
