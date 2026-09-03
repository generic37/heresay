# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Re-consent: the agreement is versioned by its content, and consent is tied
to the version.

Edit the file, and everyone is asked once, at their next login, before they are
let back in-world. Declining costs nothing but this connection.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import (
    AGREE_PROMPT, REVISED_NOTICE, TalkerServer, agreement_version, declined_msg,
)

REPO = Path(__file__).resolve().parent.parent
WORLD = REPO / "tests" / "fixtures" / "house" / "world"
REAL_AGREEMENT = REPO / "tests" / "fixtures" / "house" / "agreement.txt"
# The room TITLE, not the words "atrium" — the agreement text
# itself mentions the room, so only the title proves arrival.
IN_WORLD = "[ atrium ]"


# ---- the version function itself ----------------------------------------

def test_version_is_the_sha256_of_the_text():
    import hashlib
    text = REAL_AGREEMENT.read_text(encoding="utf-8").rstrip("\n")
    assert agreement_version(text) == hashlib.sha256(text.encode()).hexdigest()
    assert len(agreement_version(text)) == 64


def test_any_edit_is_a_new_version_even_whitespace():
    a = agreement_version("hello")
    assert agreement_version("hello ") != a          # a stray space counts
    assert agreement_version("hello\n") != a
    assert agreement_version("hello") == a           # and it is stable


# ---- a fixture whose agreement file we can edit --------------------------

def _cfg(tmp_path, agreement, grace=0.25):
    return Config(
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        agreement_file=str(agreement),
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
        disconnect_grace_seconds=grace,
        owner_name="Generic",
    )


@pytest.fixture
def agreement(tmp_path):
    """A private copy of the real agreement, so a test may revise it."""
    path = tmp_path / "agreement.txt"
    shutil.copy(REAL_AGREEMENT, path)
    return path


@pytest.fixture
async def server(tmp_path, agreement):
    srv = TalkerServer(_cfg(tmp_path, agreement))
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


@pytest.fixture
async def slow_grace_server(tmp_path, agreement):
    """A grace window comfortably longer than a login handshake takes, so a
    reattach test is testing the reattach and not the clock."""
    srv = TalkerServer(_cfg(tmp_path, agreement, grace=30.0))
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


def revise(agreement: Path, note="\n9. And one more thing, added later.\n"):
    """Edit the file the way an operator would."""
    agreement.write_text(agreement.read_text(encoding="utf-8") + note,
                         encoding="utf-8")


async def restart(srv, tmp_path, agreement):
    """A new process over the same state — how a revision actually reaches people."""
    await srv.aclose()
    fresh = TalkerServer(_cfg(tmp_path, agreement))
    await fresh.start()
    return fresh, fresh._servers[0].sockets[0].getsockname()[1]


# ---- wire helpers --------------------------------------------------------

async def collect(reader, timeout=0.5):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(reader.read(4096), timeout)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def send(w, text):
    w.write((text + "\n").encode())
    await w.drain()


async def create(port, name, pw="swordfish-x9"):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r)
    for line in (name, "yes", pw, pw, "y"):
        await send(w, line)
        await collect(r, 1.5)
    return r, w


async def login(port, name, pw="swordfish-x9"):
    """Up to and including the password; returns whatever came back next."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r)
    await send(w, name)
    await collect(r)
    await send(w, pw)
    return r, w, await collect(r, 1.2)


# ---- creation stores the hash; an unchanged text never prompts -----------

@pytest.mark.asyncio
async def test_creation_stores_the_current_hash(server):
    srv, port = server
    r, w = await create(port, "Corvid")
    acct = srv.accounts.get("Corvid")
    assert acct.agreed_hash == srv.agreement_hash
    assert acct.agreed                                  # timestamp too
    w.close()


@pytest.mark.asyncio
async def test_immediate_relogin_does_not_prompt(server):
    srv, port = server
    r, w = await create(port, "Corvid")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.4)                            # let the grace lapse

    r2, w2, out = await login(port, "Corvid")
    assert REVISED_NOTICE not in out
    assert IN_WORLD in out                              # straight in
    w2.close()


@pytest.mark.asyncio
async def test_declining_at_creation_is_unchanged(server):
    srv, port = server
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r)
    for line in ("Corvid", "yes", "swordfish-x9", "swordfish-x9"):
        await send(w, line)
        await collect(r)
    await send(w, "n")
    out = await collect(r)
    assert declined_msg(srv.theme) in out
    assert not srv.accounts.exists("Corvid")
    w.close()


# ---- the revision ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_revision_prompts_with_the_exact_line_and_full_text(
        server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Corvid")
    await send(w, ".quit"); await collect(r); w.close()

    revise(agreement)
    srv, port = await restart(srv, tmp_path, agreement)
    try:
        r2, w2, out = await login(port, "Corvid")
        assert REVISED_NOTICE in out
        assert "18 or older" in out                     # the full text follows
        assert "And one more thing, added later." in out    # the NEW text
        assert AGREE_PROMPT.strip() in out
        assert IN_WORLD not in out                      # NOT in-world yet
        w2.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_agreeing_updates_the_hash_and_lets_them_in(
        server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Corvid")
    before = srv.accounts.get("Corvid").agreed
    await send(w, ".quit"); await collect(r); w.close()

    revise(agreement)
    srv, port = await restart(srv, tmp_path, agreement)
    try:
        r2, w2, out = await login(port, "Corvid")
        assert REVISED_NOTICE in out
        await send(w2, "y")
        arrival = await collect(r2, 1.2)
        assert IN_WORLD in arrival                      # arrival proceeds as normal
        acct = srv.accounts.get("Corvid")
        assert acct.agreed_hash == srv.agreement_hash   # the new version
        assert acct.agreed != before                    # a fresh timestamp
        await send(w2, ".quit"); await collect(r2); w2.close()
        await asyncio.sleep(0.4)

        # ...and it is asked at most once per revision.
        r3, w3, out3 = await login(port, "Corvid")
        assert REVISED_NOTICE not in out3
        assert IN_WORLD in out3
        w3.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_declining_says_so_closes_the_socket_and_changes_nothing(
        server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Corvid")
    old_hash = srv.accounts.get("Corvid").agreed_hash
    old_stamp = srv.accounts.get("Corvid").agreed
    await send(w, ".quit"); await collect(r); w.close()

    revise(agreement)
    srv, port = await restart(srv, tmp_path, agreement)
    try:
        r2, w2, out = await login(port, "Corvid")
        assert REVISED_NOTICE in out
        await send(w2, "n")
        bye = await collect(r2, 1.0)
        assert declined_msg(srv.theme) in bye
        # The socket is closed: a read returns EOF rather than another prompt.
        assert await asyncio.wait_for(r2.read(4096), 2.0) == b""
        w2.close()

        acct = srv.accounts.get("Corvid")
        assert acct.agreed_hash == old_hash             # untouched
        assert acct.agreed == old_stamp
        assert srv.accounts.exists("Corvid")            # still an account

        # They can come back any time and agree.
        r3, w3, out3 = await login(port, "Corvid")
        assert REVISED_NOTICE in out3
        await send(w3, "y")
        assert IN_WORLD in await collect(r3, 1.2)
        assert srv.accounts.get("Corvid").agreed_hash == srv.agreement_hash
        w3.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_junk_answers_re_ask(server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Corvid")
    await send(w, ".quit"); await collect(r); w.close()

    revise(agreement)
    srv, port = await restart(srv, tmp_path, agreement)
    try:
        r2, w2, out = await login(port, "Corvid")
        assert REVISED_NOTICE in out
        for junk in ("maybe", "", "sure?", "42"):
            await send(w2, junk)
            again = await collect(r2, 0.8)
            assert AGREE_PROMPT.strip() in again, junk
            assert IN_WORLD not in again               # still held at the door
        await send(w2, "yes")                            # the long form works
        assert IN_WORLD in await collect(r2, 1.2)
        w2.close()
    finally:
        await srv.aclose()


# ---- the migration: an account from before the versioning ----------------

@pytest.mark.asyncio
async def test_a_legacy_account_with_no_hash_is_prompted(server):
    """Every account on disk today has `agreed` but no `agreed_hash`. This is
    the migration path: they are asked once, at their next login."""
    srv, port = server
    r, w = await create(port, "Corvid")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.4)

    # Rewrite the account the way it looked before this feature existed.
    acct = srv.accounts.get("Corvid")
    acct.agreed_hash = ""
    srv.accounts._save()
    reloaded = AccountStore(srv.accounts.path).get("Corvid")
    assert reloaded.agreed and not reloaded.agreed_hash   # legacy shape on disk

    r2, w2, out = await login(port, "Corvid")
    assert REVISED_NOTICE in out
    await send(w2, "y")
    assert IN_WORLD in await collect(r2, 1.2)
    assert srv.accounts.get("Corvid").agreed_hash == srv.agreement_hash
    w2.close()


# ---- no exemptions --------------------------------------------------------

@pytest.mark.asyncio
async def test_staff_re_consent_like_anyone(server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Wizzy")
    srv.accounts.set_rank("Wizzy", "staff")
    await send(w, ".quit"); await collect(r); w.close()

    revise(agreement)
    srv, port = await restart(srv, tmp_path, agreement)
    try:
        assert srv.accounts.get("Wizzy").rank == "staff"
        r2, w2, out = await login(port, "Wizzy")
        assert REVISED_NOTICE in out                     # no rank exemption
        await send(w2, "y")
        assert IN_WORLD in await collect(r2, 1.2)
        w2.close()
    finally:
        await srv.aclose()


# ---- where in the login it runs -------------------------------------------

@pytest.mark.asyncio
async def test_takeover_prompts_before_the_takeover_offer(
        server, tmp_path, agreement):
    """A self-takeover IS a fresh authenticated login, so it re-consents — and
    the agreement comes first, before the 'take over that session?' question."""
    srv, port = server
    r, w = await create(port, "Corvid")

    revise(agreement)
    srv2, port2 = await restart(srv, tmp_path, agreement)
    try:
        # The first connection died with the old server; make a live one again.
        ra, wa, out_a = await login(port2, "Corvid")
        assert REVISED_NOTICE in out_a
        await send(wa, "y")
        await collect(ra, 1.2)                           # in-world now

        # A second login for the same account, on a server whose text moved on
        # from what THIS connection agreed to... it already agreed, so it goes
        # straight to the takeover offer.
        rb, wb, out_b = await login(port2, "Corvid")
        assert REVISED_NOTICE not in out_b
        assert "Take over that session?" in out_b
        await send(wb, "n")
        await collect(rb)
        wa.close(); wb.close()
    finally:
        await srv2.aclose()


@pytest.mark.asyncio
async def test_a_stale_hash_is_asked_before_the_takeover_question(
        server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Corvid")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.4)

    revise(agreement)
    srv2, port2 = await restart(srv, tmp_path, agreement)
    try:
        # Get one connection in-world by agreeing.
        ra, wa, out_a = await login(port2, "Corvid")
        await send(wa, "y")
        await collect(ra, 1.2)
        # Now put the stored hash back to the old version, so the SECOND login
        # is both stale and already-connected. The agreement must come first.
        srv2.accounts.get("Corvid").agreed_hash = "stale"
        srv2.accounts._save()

        rb, wb, out_b = await login(port2, "Corvid")
        assert REVISED_NOTICE in out_b
        assert "Take over that session?" not in out_b    # not yet — agree first
        await send(wb, "y")
        offer = await collect(rb, 1.0)
        assert "Take over that session?" in offer        # NOW the offer
        await send(wb, "n")
        await collect(rb)
        wa.close(); wb.close()
    finally:
        await srv2.aclose()


@pytest.mark.asyncio
async def test_grace_reattach_never_prompts(slow_grace_server, tmp_path, agreement):
    """A reattach is resuming a live session, not a login. Even with a stale
    hash, it must not be interrupted."""
    srv, port = slow_grace_server
    r, w = await create(port, "Corvid")
    await collect(r)
    srv.accounts.get("Corvid").agreed_hash = "stale"     # as if the text moved
    srv.accounts._save()

    w.close()                                            # an unexpected drop
    await asyncio.sleep(0.1)                             # ...inside the grace window
    res = srv.world.get_resident("Corvid")
    assert res is not None and res.in_grace

    r2, w2, out = await login(port, "Corvid")
    assert REVISED_NOTICE not in out
    assert "Reconnected." in out
    w2.close()


@pytest.mark.asyncio
async def test_recovery_login_also_checks(server, tmp_path, agreement):
    """The recovery-code path lands in the same gate: after the password reset,
    before arrival."""
    srv, port = server
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r)
    for line in ("Corvid", "yes", "swordfish-x9", "swordfish-x9"):
        await send(w, line)
        await collect(r)
    await send(w, "y")
    codes_out = await collect(r, 1.5)
    import re
    codes = re.findall(r"[a-z]+-[a-z]+-[a-z]+-\d{4}", codes_out)
    assert codes
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.4)

    revise(agreement)
    srv2, port2 = await restart(srv, tmp_path, agreement)
    try:
        r2, w2 = await asyncio.open_connection("127.0.0.1", port2)
        await collect(r2)
        await send(w2, "Corvid"); await collect(r2)
        await send(w2, "recover"); await collect(r2)
        await send(w2, codes[0]); await collect(r2)
        await send(w2, "newpassword"); await collect(r2)
        await send(w2, "newpassword")
        out = await collect(r2, 1.5)
        assert "Password reset." in out
        assert REVISED_NOTICE in out                     # ...then the agreement
        assert IN_WORLD not in out                       # not in yet
        await send(w2, "y")
        assert IN_WORLD in await collect(r2, 1.2)
        assert srv2.accounts.get("Corvid").agreed_hash == srv2.agreement_hash
        w2.close()
    finally:
        await srv2.aclose()


@pytest.mark.asyncio
async def test_mail_notice_comes_after_re_consent(server, tmp_path, agreement):
    srv, port = server
    r, w = await create(port, "Corvid")
    await send(w, ".quit"); await collect(r); w.close()
    await asyncio.sleep(0.4)
    srv.mail.deliver("Corvid", "Wren", "a letter")

    revise(agreement)
    srv2, port2 = await restart(srv, tmp_path, agreement)
    try:
        r2, w2, out = await login(port2, "Corvid")
        assert REVISED_NOTICE in out
        assert "You have mail." not in out               # not before agreeing
        await send(w2, "y")
        after = await collect(r2, 1.2)
        assert "You have mail." in after                 # part of normal arrival
        w2.close()
    finally:
        await srv2.aclose()
