# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.deleteuser` and `.resetpassword` — the owner's two account powers.

Both are arch-only, both take TWO gates, and both reuse what `.deleteme` already
proved: an echo-off prompt driven by the session loop and `accounts.averify` on
the ACTOR's own password. These tests drive the real doors, because the prompts
only exist there.

The scope of the deletion is the interesting part and is asserted explicitly:
account + mailbox + revtells go; mail the deleted person SENT to others, and any
ban on their name, stay.
"""

import asyncio
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import CURRENT_AGREEMENT_HASH, seed_account

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
ARCH_PW = "brass-otter-hum-9"
USER_PW = "swordfish-x9-plum"


def _cfg(tmp_path, **over):
    base = dict(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        owner_name="Boss",
        max_conns_per_ip=20, churn_max_in_window=50,
    )
    base.update(over)
    return Config(**base)


@pytest.fixture
async def server(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    seed_account(store, "Boss", ARCH_PW)
    seed_account(store, "Corvid", USER_PW)
    seed_account(store, "Wren", USER_PW)
    store.flush()
    srv = TalkerServer(_cfg(tmp_path))
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1], tmp_path
    finally:
        await srv.aclose()


async def collect(r, timeout=0.5):
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


async def send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def login(port, name, pw):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r)
    await send(w, name)
    await collect(r)
    await send(w, pw)
    out = await collect(r, timeout=1.5)
    return r, w, out


async def create(port, name, pw):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r)
    await send(w, "yes"); await collect(r); await send(w, pw); await collect(r)
    await send(w, pw); await collect(r)
    await send(w, "y")
    out = await collect(r, timeout=1.5)
    return r, w, out


# ---- gating ---------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["deleteuser", "resetpassword"])
async def test_refused_below_arch(server, verb):
    _srv, port, _tmp = server
    r, w, _ = await login(port, "Corvid", USER_PW)
    try:
        await send(w, f".{verb} Wren")
        out = await collect(r)
        assert f"Unknown command: .{verb}" in out
    finally:
        w.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["deleteuser", "resetpassword"])
async def test_unknown_name_is_refused(server, verb):
    _srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, f".{verb} Nobody")
        out = await collect(r)
        assert "There's no account called Nobody." in out
    finally:
        w.close()


@pytest.mark.asyncio
async def test_the_arch_account_cannot_be_deleted(server):
    """Freeing that name would hand arch to the next registrant."""
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Boss")
        out = await collect(r)
        assert "You can't delete the owner." in out
        assert srv.accounts.get("Boss") is not None
    finally:
        w.close()


# ---- .deleteuser: the two gates ------------------------------------------

@pytest.mark.asyncio
async def test_a_mistyped_username_cancels(server):
    """Gate 1 exists to catch 'wrong person' — the mistake that actually
    happens here — so a near-miss must delete nothing."""
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Corvid")
        out = await collect(r)
        assert "cannot be undone" in out
        await send(w, "Corvi")                    # one character short
        out = await collect(r)
        assert "doesn't match" in out
        assert srv.accounts.get("Corvid") is not None
    finally:
        w.close()


@pytest.mark.asyncio
async def test_a_wrong_arch_password_cancels(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Corvid")
        await collect(r)
        await send(w, "Corvid")                   # gate 1 passed
        await collect(r)
        await send(w, "not-the-arch-password")    # gate 2 failed
        out = await collect(r, timeout=1.5)
        assert "nothing was deleted" in out
        assert srv.accounts.get("Corvid") is not None
    finally:
        w.close()


@pytest.mark.asyncio
async def test_the_username_retype_is_case_insensitive(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Corvid")
        await collect(r)
        await send(w, "  cOrViD  ")               # casefold + surrounding space
        await collect(r)
        await send(w, ARCH_PW)
        out = await collect(r, timeout=1.5)
        assert "is gone" in out
        assert srv.accounts.get("Corvid") is None
    finally:
        w.close()


@pytest.mark.asyncio
async def test_the_full_delete_removes_account_mailbox_and_revtells(server):
    srv, port, tmp_path = server
    # Give Corvid something in every store the delete is supposed to clear...
    srv.mail.deliver("Corvid", "Wren", "a letter to Corvid")
    srv.revtells.record("Corvid", ">> Wren says: hello")
    # ...and something that must SURVIVE: mail Corvid sent to somebody else.
    srv.mail.deliver("Wren", "Corvid", "a letter from Corvid")
    assert srv.mail.read("Corvid") and srv.revtells.read("Corvid")

    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Corvid")
        await collect(r)
        await send(w, "Corvid")
        await collect(r)
        await send(w, ARCH_PW)
        out = await collect(r, timeout=1.5)
        assert "Corvid is gone. The name is free again." in out

        assert srv.accounts.get("Corvid") is None          # the account
        assert srv.mail.read("Corvid") == []               # their mailbox
        assert srv.revtells.read("Corvid") == []           # their private lines
        # Mail they SENT lives in the RECIPIENT's box and is the recipient's.
        bodies = [m.body for m in srv.mail.read("Wren")]
        assert "a letter from Corvid" in bodies
    finally:
        w.close()


@pytest.mark.asyncio
async def test_a_ban_on_the_name_survives_deletion(server):
    """Deletion must not be a way around a ban."""
    srv, port, _tmp = server
    srv.bans.ban("Corvid", "name", 24.0)
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Corvid")
        await collect(r)
        await send(w, "Corvid"); await collect(r)
        await send(w, ARCH_PW); await collect(r, timeout=1.5)
        assert srv.accounts.get("Corvid") is None
        assert srv.bans.is_banned_name("Corvid")           # still barred
    finally:
        w.close()


@pytest.mark.asyncio
async def test_the_name_is_free_for_re_registration(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    await send(w, ".deleteuser Corvid")
    await collect(r)
    await send(w, "Corvid"); await collect(r)
    await send(w, ARCH_PW); await collect(r, timeout=1.5)

    r2, w2, out = await create(port, "Corvid", "a-brand-new-passphrase-7")
    try:
        assert "atrium" in out.lower() or "[ " in out   # they arrived
        assert srv.accounts.get("Corvid") is not None
    finally:
        w.close(); w2.close()


@pytest.mark.asyncio
async def test_an_online_target_is_disconnected(server):
    srv, port, _tmp = server
    rc, wc, _ = await login(port, "Corvid", USER_PW)
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".deleteuser Corvid")
        await collect(r)
        await send(w, "Corvid"); await collect(r)
        await send(w, ARCH_PW); await collect(r, timeout=1.5)
        victim = await collect(rc, timeout=1.5)
        assert "deleted" in victim.lower()
        assert srv.world.get_resident("Corvid") is None
    finally:
        w.close(); wc.close()


@pytest.mark.asyncio
async def test_the_deletion_persists_across_a_restart(server, tmp_path):
    srv, port, tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    await send(w, ".deleteuser Corvid")
    await collect(r)
    await send(w, "Corvid"); await collect(r)
    await send(w, ARCH_PW); await collect(r, timeout=1.5)
    w.close()
    srv.accounts.flush()
    reloaded = AccountStore(tmp / "accounts.json")          # a restart
    assert reloaded.get("Corvid") is None
    assert reloaded.get("Wren") is not None                 # and nobody else went


# ---- .resetpassword -------------------------------------------------------

@pytest.mark.asyncio
async def test_a_weak_new_password_is_refused(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".resetpassword Corvid")
        out = await collect(r)
        assert "Setting a new password for Corvid" in out
        await send(w, "short")
        out = await collect(r)
        assert "too short" in out.lower()
    finally:
        w.close()


@pytest.mark.asyncio
async def test_a_common_new_password_is_refused(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".resetpassword Corvid")
        await collect(r)
        await send(w, "qwertyuiop")
        out = await collect(r)
        assert "too common" in out.lower()
    finally:
        w.close()


@pytest.mark.asyncio
async def test_a_mismatched_confirmation_cancels(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".resetpassword Corvid")
        await collect(r)
        await send(w, "a-perfectly-fine-one-1"); await collect(r)
        await send(w, "a-different-one-entirely-2")
        out = await collect(r)
        assert "didn't match" in out
    finally:
        w.close()


@pytest.mark.asyncio
async def test_a_wrong_arch_password_cancels_the_reset(server):
    srv, port, _tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".resetpassword Corvid")
        await collect(r)
        NEW = "a-perfectly-fine-one-1"
        await send(w, NEW); await collect(r)
        await send(w, NEW); await collect(r)
        await send(w, "not-the-arch-password")
        out = await collect(r, timeout=1.5)
        assert "nothing was changed" in out
    finally:
        w.close()
    # ...and the old password still works.
    r2, w2, out = await login(port, "Corvid", USER_PW)
    try:
        assert "[ " in out           # they got in
    finally:
        w2.close()


@pytest.mark.asyncio
async def test_the_user_logs_in_with_the_new_password(server):
    srv, port, _tmp = server
    NEW = "a-brand-new-passphrase-7"
    r, w, _ = await login(port, "Boss", ARCH_PW)
    await send(w, ".resetpassword Corvid")
    await collect(r)
    await send(w, NEW); await collect(r)
    await send(w, NEW); await collect(r)
    await send(w, ARCH_PW)
    out = await collect(r, timeout=1.5)
    assert "password is set" in out
    assert "recovery codes are untouched" in out
    w.close()

    r2, w2, out2 = await login(port, "Corvid", NEW)
    try:
        assert "[ " in out2                       # in, with the new one
    finally:
        w2.close()


@pytest.mark.asyncio
async def test_recovery_codes_survive_a_reset(server):
    """The reset is a backstop, not a revocation: a resident who later finds
    their codes can still use them."""
    srv, port, _tmp = server
    srv.accounts.set_recovery("Corvid", ["word-word-word-1234"])
    assert srv.accounts.recovery_count("Corvid") == 1
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        NEW = "a-brand-new-passphrase-7"
        await send(w, ".resetpassword Corvid")
        await collect(r)
        await send(w, NEW); await collect(r)
        await send(w, NEW); await collect(r)
        await send(w, ARCH_PW); await collect(r, timeout=1.5)
        assert srv.accounts.recovery_count("Corvid") == 1
    finally:
        w.close()


@pytest.mark.asyncio
async def test_the_reset_persists_across_a_restart(server, tmp_path):
    srv, port, tmp = server
    NEW = "a-brand-new-passphrase-7"
    r, w, _ = await login(port, "Boss", ARCH_PW)
    await send(w, ".resetpassword Corvid")
    await collect(r)
    await send(w, NEW); await collect(r)
    await send(w, NEW); await collect(r)
    await send(w, ARCH_PW); await collect(r, timeout=1.5)
    w.close()
    srv.accounts.flush()
    reloaded = AccountStore(tmp / "accounts.json")          # a restart
    assert reloaded.verify("Corvid", NEW)
    assert not reloaded.verify("Corvid", USER_PW)


# ---- .mailpurge: the third staged command (v1.41.0) -----------------------
#
# ⛔ It destroys a whole mailbox irreversibly — the one thing the covenant lets
# the server keep — and until v1.41.0 a single mistyped line finished the job.
# It stages like the two above now, and the session loop asks for the CALLER's
# OWN password. ⚠ Always the caller's: never the target's, never "the arch's".
# That is what keeps the gate correct when a house reassigns the command.

@pytest.mark.asyncio
async def test_mailpurge_needs_the_callers_own_password(server):
    srv, port, tmp = server
    srv.mail.deliver("Corvid", "Wren", "a letter")
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".mailpurge Corvid")
        out = await collect(r)
        assert "permanently" in out and "password" in out.lower()
        assert srv.mail.count("Corvid") == 1          # nothing gone yet

        await send(w, "not-the-right-password")
        out = await collect(r)
        assert "not the right password" in out
        assert srv.mail.count("Corvid") == 1          # still nothing
    finally:
        w.close()


@pytest.mark.asyncio
async def test_mailpurge_with_the_right_password_purges(server):
    srv, port, tmp = server
    srv.mail.deliver("Corvid", "Wren", "a letter")
    srv.mail.deliver("Corvid", "Wren", "another")
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".mailpurge Corvid")
        await collect(r)
        await send(w, ARCH_PW)
        out = await collect(r)
        assert "Purged 2" in out
        assert srv.mail.count("Corvid") == 0
        # ⛔ ...and not a word of the mail was read on the way past.
        assert "a letter" not in out and "another" not in out
    finally:
        w.close()


@pytest.mark.asyncio
async def test_mailpurge_refuses_the_arch(server):
    srv, port, tmp = server
    r, w, _ = await login(port, "Boss", ARCH_PW)
    try:
        await send(w, ".mailpurge Boss")
        out = await collect(r)
        assert "owner" in out
        assert "password" not in out.lower()          # never even staged
    finally:
        w.close()
