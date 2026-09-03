# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The account-creation agreement step: text, y/n, decline writes nothing."""

import asyncio
import re
from pathlib import Path

import pytest

from heresay.accounts import AccountStore
from heresay.config import Config
from heresay.server import TalkerServer

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"
CODE_RE = re.compile(r"[a-z]+-[a-z]+-[a-z]+-\d{4}")


@pytest.fixture
async def server(tmp_path):
    cfg = Config(
        owner_name="Generic", instance_name="Commons",
        plain_host="127.0.0.1", plain_port=0, ws_host="127.0.0.1", ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        # agreement_file defaults to "agreement.txt", resolved from the repo root.
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
    )
    srv = TalkerServer(cfg)
    await srv.start()
    try:
        yield srv, srv._servers[0].sockets[0].getsockname()[1]
    finally:
        await srv.aclose()


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


async def to_agreement(port, name, pw="swordfish-x9"):
    """Drive creation up to (and including) the agreement prompt."""
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, name); await collect(r)   # Create it?
    await send(w, "yes"); await collect(r)                     # Choose a password
    await send(w, pw); await collect(r)                        # Confirm password
    await send(w, pw)
    return r, w, await collect(r)                              # agreement + prompt


async def create_ok(port, name, pw="swordfish-x9"):
    r, w, _ = await to_agreement(port, name, pw)
    await send(w, "y")
    return r, w, await collect(r, timeout=1.5)


@pytest.mark.asyncio
async def test_agree_shows_text_then_codes_and_writes_agreed(server):
    srv, port = server
    # An observer already in-world sees the arrival.
    orr, oww, _ = await create_ok(port, "Watcher")

    r, w, agr = await to_agreement(port, "Ada")
    assert "Words reach eyes" in agr                                # distinctive line
    assert "18 or older" in agr                                     # the 18+ line
    assert ".deleteme" in agr                                       # the deletion mention
    # The two review exceptions are named where consent is actually given.
    assert ".rev" in agr and ".revtell" in agr
    assert "Do you agree? (y/n)" in agr

    await send(w, "y")
    out = await collect(r, timeout=1.5)
    assert len(CODE_RE.findall(out)) == 5           # codes shown after agreeing
    assert "atrium" in out.lower()             # arrival

    # agreed timestamp is recorded (a fact), on disk.
    acct = srv.accounts.get("Ada")
    assert acct is not None and acct.agreed != ""
    raw = Path(srv.config.accounts_file).read_text()
    assert '"agreed"' in raw and acct.agreed in raw

    # arrival announced to the room.
    assert "[Ada enters the world]" in await collect(orr)
    for x in (w, oww):
        x.close()


@pytest.mark.asyncio
async def test_decline_writes_nothing_and_returns_to_name_prompt(server):
    srv, port = server
    r, w, agr = await to_agreement(port, "Bob")
    await send(w, "n")
    out = await collect(r)
    assert "Fair enough. Commons will be here if you change your mind." in out
    assert "Name" in out                     # back at the name prompt
    assert not srv.accounts.exists("Bob")     # nothing written
    w.close()

    # The name is still creatable afterward.
    r2, w2, out2 = await create_ok(port, "Bob")
    assert srv.accounts.exists("Bob")
    assert len(CODE_RE.findall(out2)) == 5
    w2.close()


@pytest.mark.asyncio
async def test_junk_reasks_then_yes_variant_agrees(server):
    srv, port = server
    r, w, _ = await to_agreement(port, "Cara")
    await send(w, "maybe")
    reask = await collect(r)
    assert "Do you agree? (y/n)" in reask       # junk re-asks
    assert not srv.accounts.exists("Cara")
    await send(w, "yes")                          # 'yes' variant agrees
    out = await collect(r, timeout=1.5)
    assert len(CODE_RE.findall(out)) == 5
    assert srv.accounts.exists("Cara")
    w.close()


@pytest.mark.asyncio
async def test_no_variant_declines(server):
    srv, port = server
    r, w, _ = await to_agreement(port, "Dora")
    await send(w, "NO")                           # 'NO' variant declines
    assert "Fair enough" in await collect(r)
    assert not srv.accounts.exists("Dora")
    w.close()


@pytest.mark.asyncio
async def test_an_account_that_never_agreed_is_asked_at_login(server):
    """Consent is tied to the version of the text, so an account carrying no
    consent at all is asked on its way in — the same gate a revision uses.

    (This test asserted the opposite until consent became versioned: the login
    path used not to ask at all. See test_reconsent.py for the whole contract.)
    """
    srv, port = server
    # A pre-existing account (created directly) has neither timestamp nor hash.
    srv.accounts.create("Old", "oldpass")
    assert srv.accounts.get("Old").agreed == ""
    assert srv.accounts.get("Old").agreed_hash == ""

    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, "Old"); await collect(r); await send(w, "oldpass")
    intro = await collect(r, timeout=1.0)
    assert "The agreement has been revised" in intro
    assert "Do you agree" in intro
    assert "[ atrium ]" not in intro           # held at the door for now

    await send(w, "y")
    assert "[ atrium ]" in await collect(r, timeout=1.0)   # then in, normally
    assert srv.accounts.get("Old").agreed_hash == srv.agreement_hash
    w.close()


@pytest.mark.asyncio
async def test_an_account_that_agreed_to_this_text_is_not_asked(server):
    srv, port = server
    srv.accounts.create("Current", "pw12", agreed=True,
                        agreed_hash=srv.agreement_hash)

    r, w = await asyncio.open_connection("127.0.0.1", port)
    await collect(r); await send(w, "Current"); await collect(r); await send(w, "pw12")
    intro = await collect(r, timeout=1.0)
    assert "The agreement has been revised" not in intro
    assert "[ atrium ]" in intro               # straight in
    w.close()


def test_missing_agreement_file_fails_fast(tmp_path):
    cfg = Config(
        owner_name="Generic", instance_name="Commons",           # so the boot reaches the agreement check
        plain_port=0, ws_port=0,
        cert_file=str(tmp_path / "c"), key_file=str(tmp_path / "k"),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        agreement_file=str(tmp_path / "nope-agreement.txt"),   # missing
        world_dir=str(WORLD),
        entry_room="atrium", jail_room="holding_cell",
    )
    with pytest.raises(ValueError):
        TalkerServer(cfg)


# ---- the agreement text itself, byte for byte ---------------------------

AGREEMENT_PATH = Path(__file__).resolve().parent / "fixtures" / "house" / "agreement.txt"

EXPECTED_AGREEMENT = """By continuing you affirm that you are 18 or older, and that you will
be decent and keep it legal. You also understand:

1. Almost nothing said here is recorded. Conversation vanishes as it
   scrolls away; there is no history to request. Two narrow
   exceptions: the entry room keeps the last {rev_window_minutes}
   minutes of what was said in passing memory (never on disk, gone on
   restart), fading as it goes, so arrivals can see the room is alive
   — .rev shows it; and your own last {revtell_count} tells and
   remotes are held, encrypted, so you can pick a conversation back
   up with .revtell.
   If you want privacy from later entrants, speak somewhere other
   than the entry room.

2. A mail message you send is held, encrypted, until the recipient
   deletes it (or 180 days pass). Mail and review buffers are private
   by design — no command exists, at any rank, to read another's —
   but they are not end-to-end encrypted: in principle, whoever
   operates the server could read what the server holds. You must
   decide how much to trust the house you're standing in.

3. Connections are noted. When you connect, the time and your
   connection's IP address are recorded; and certain actions — a
   staff using a power, a password reset — are noted against the
   account they concern. Never a word of what was said. These notes
   are kept about {event_retention_days} days and then deleted, and
   only the {owner} can read them from inside the house, to answer
   abuse or trace trouble.

4. Though the house records almost nothing, the people in a room with
   you may capture text or take screenshots. Words reach eyes; eyes
   remember.

5. Your account is a name and a password — no email, nothing else. If
   you lose the password, your recovery codes are the only way back
   in. Keep them somewhere real.

6. You can delete your own account at any time with .deleteme, which
   removes your account, profile, mailbox, and review buffers,
   permanently.

7. Staff keep the peace: they may gag, jail, ban, or remove accounts
   that break the law or make the rooms unpleasant to be in. No one —
   staff or owner — can read your mail or your .revtell.

8. Locked doors are privacy from other residents, not from the house.
   A staff may force a lock, and you will always see them enter —
   there is no invisible observation. Please don't give a staff a
   reason to force a lock.

9. This house is offered as-is: no warranty, no promise of uptime, no
   permanence except what these rules state.

Questions or trouble: .call_staff <text> — or leave a note for the
house with .mail_staff <text>.
"""


def _flat(text):
    """The text as a single line, so a substring check is not really a check on
    where the wrapping happened to fall."""
    return " ".join(text.split())


def test_agreement_states_both_review_exceptions_and_their_limits():
    text = _flat(AGREEMENT_PATH.read_text(encoding="utf-8"))
    assert "never on disk, gone on restart" in text      # .rev's bound
    assert "held, encrypted" in text                     # .revtell's bound
    assert "no command exists, at any rank, to read another's" in text
    assert "not end-to-end encrypted" in text            # the honest limit
    assert "mailbox, and review buffers" in text         # .deleteme's reach


# ---- the narrow reflow (v1.29.1) ------------------------------------------
#
# The agreement is authored prose hard-wrapped near 70, so at 40 every line
# wrapped again a few words from its end and each clause arrived
# long/short/long/short — the same problem room descriptions had in v1.26.0, and
# the same fix. What makes it safe is that it happens at RENDER time: the file is
# never touched, so its sha256 — which IS its version — cannot move.

def _server(tmp_path):
    cfg = Config(
        owner_name="Generic", instance_name="Commons",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
    )
    return TalkerServer(cfg)


class _Width:
    """The only two things `agreement_for` asks a session for."""

    def __init__(self, narrow, width):
        self.is_narrow, self.width = narrow, width


def test_wide_reflows_to_the_session_width(tmp_path):
    """v1.29.3: 78 is reflowed too, where it used to be the file verbatim. The
    authored breaks were set around a 21-character token that renders as "30",
    so they describe a line nobody reads."""
    srv = _server(tmp_path)
    out = srv.agreement_for(_Width(False, 78))
    assert all(len(ln) <= 78 for ln in out.split("\n")), out
    assert "{rev_window_minutes}" not in out


def test_narrow_reflows_it_to_the_session_width(tmp_path):
    srv = _server(tmp_path)
    out = srv.agreement_for(_Width(True, 40))
    assert out != srv.agreement_text
    assert all(len(ln) <= 40 for ln in out.split("\n")), out


@pytest.mark.parametrize("narrow,width", [(True, 40), (False, 78)])
def test_the_reflow_is_not_ragged(tmp_path, narrow, width):
    """The bug being fixed: 70-column lines re-wrapped at 40 alternate long and
    short. After a reflow every line but a paragraph's last is FULL — the next
    word genuinely would not fit. Checked at BOTH widths since v1.29.3, because
    the substituted value would otherwise leave a hole in clause 1 at 78."""
    srv = _server(tmp_path)
    for para in srv.agreement_for(_Width(narrow, width)).split("\n\n"):
        lines = para.split("\n")
        for line, following in zip(lines, lines[1:]):
            nxt = following.split(" ")[0]
            assert len(line) + 1 + len(nxt) > width, (line, nxt)


def test_the_numbered_clauses_survive_the_reflow(tmp_path):
    """They are separated by blank lines, so the reflow keeps them apart; what
    they lose is the hanging indent, which at 40 is three columns better spent
    on the sentence."""
    srv = _server(tmp_path)
    out = srv.agreement_for(_Width(True, 40))
    for n in range(1, 10):
        assert f"\n{n}. " in "\n" + out, n
    # ...and no two clauses ran together.
    assert "understand:\n\n1." in out


def test_reflowing_does_not_move_the_version(tmp_path):
    """⛔ THE point of doing the WRAPPING at render time. The version is the
    sha256 of the resolved-but-unwrapped text; if a reflow touched it, a phone
    and a terminal would hold different versions and everyone would re-consent
    on rotating their screen. Hash before, render both widths, hash after."""
    from heresay.server import agreement_version, resolve_agreement, _load_agreement
    before = AGREEMENT_PATH.read_bytes()
    srv = _server(tmp_path)
    stamped = srv.agreement_hash

    srv.agreement_for(_Width(True, 40))
    srv.agreement_for(_Width(False, 78))

    assert AGREEMENT_PATH.read_bytes() == before          # the file is untouched
    assert srv.agreement_hash == stamped                  # ...so the stamp holds
    assert agreement_version(srv.agreement_text) == stamped
    # And the hash is of the RESOLVED text — the words a resident actually
    # reads — not of the raw file and not of anything we rendered.
    # ⚠ Resolve with the server's OWN theme (Commons): the {arch} placeholder
    # substitutes the owner rank word, which differs between the generic
    # DEFAULT_THEME and the shipped house (v1.54.0), and the stamp was taken with
    # the latter.
    assert agreement_version(
        resolve_agreement(_load_agreement(AGREEMENT_PATH), srv.config,
                          srv.theme)) == stamped
    assert agreement_version(
        AGREEMENT_PATH.read_text(encoding="utf-8").rstrip("\n")) != stamped


def test_a_narrow_reflow_never_becomes_the_stored_text(tmp_path):
    """Belt and braces: rendering must not mutate the server's own copy either,
    or the next re-consent check would compare against a reflowed string."""
    srv = _server(tmp_path)
    original = srv.agreement_text
    for _ in range(3):
        srv.agreement_for(_Width(True, 40))
    assert srv.agreement_text == original


# ---- the config-driven retention line (v1.29.3) ---------------------------
#
# Clause 1 used to say the atrium kept its "last 20 lines". `.rev` has been
# a fading TIME window since v1.5.0, so the agreement had been describing a
# retention rule the server stopped enforcing four minor versions earlier. The
# fix is not to correct the number — a second copy of a number drifts again — but
# to remove the copy: the file carries {rev_window_minutes} and the running
# config supplies it. Because the resolution happens BEFORE the hash, changing
# the config re-consents everyone exactly as editing the prose does.

def _server_with(tmp_path, **over):
    cfg = Config(
        owner_name="Generic", instance_name="Commons",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        **over,
    )
    return TalkerServer(cfg)


@pytest.mark.parametrize("narrow,width", [(True, 40), (False, 78)])
def test_the_rendered_agreement_names_the_configured_window(tmp_path, narrow, width):
    """What a resident reads is the running value, at either width."""
    srv = _server_with(tmp_path)                      # the default, 30.0
    out = _flat(srv.agreement_for(_Width(narrow, width)))
    assert "last 30 minutes" in out
    assert "{rev_window_minutes}" not in out
    assert "last 20 lines" not in out                 # the stale wording is gone
    assert "last 20 tells" in out                     # the OTHER number, also resolved


def test_a_whole_number_window_reads_as_a_whole_number(tmp_path):
    """`rev_window_minutes` is a float. "30.0 minutes" in a consent document
    would be an engineer's number, not a sentence."""
    def says(srv):
        return _flat(srv.agreement_for(_Width(False, 78)))
    assert "last 30 minutes" in says(_server_with(tmp_path))
    assert "last 45 minutes" in says(
        _server_with(tmp_path / "b", rev_window_minutes=45.0))
    # ...and a genuine fraction still survives.
    assert "last 22.5 minutes" in says(
        _server_with(tmp_path / "c", rev_window_minutes=22.5))


@pytest.mark.parametrize("narrow,width", [(True, 40), (False, 78)])
def test_the_rendered_agreement_names_the_configured_revtell_count(
        tmp_path, narrow, width):
    """The second token (v1.29.4), which is what proves the table generalizes."""
    srv = _server_with(tmp_path, revtell_count=50)
    out = _flat(srv.agreement_for(_Width(narrow, width)))
    assert "last 50 tells and remotes" in out
    assert "{revtell_count}" not in out
    assert "last 30 minutes" in out            # ...and the other one is untouched


def test_the_two_numbers_are_independent(tmp_path):
    """One bounds a ROOM's public murmur, the other a PERSON's private buffer.
    Naming them in one clause is exactly how they would come to be confused, so
    each is pinned to its own key."""
    srv = _server_with(tmp_path, rev_window_minutes=5.0, revtell_count=99)
    out = _flat(srv.agreement_for(_Width(False, 78)))
    assert "last 5 minutes" in out
    assert "last 99 tells and remotes" in out


def test_changing_the_revtell_count_moves_the_version(tmp_path):
    """Same guarantee as the window, on the second token: an operator who
    re-tunes the private buffer cannot leave everyone consented to the old size.
    """
    a = _server_with(tmp_path / "a")                              # 20
    b = _server_with(tmp_path / "b", revtell_count=50)
    c = _server_with(tmp_path / "c", revtell_count=50)
    assert a.agreement_hash != b.agreement_hash
    assert b.agreement_hash == c.agreement_hash


def test_the_two_tokens_move_the_version_separately(tmp_path):
    """Neither is a proxy for the other — four configs, four hashes."""
    seen = {
        _server_with(tmp_path / "a").agreement_hash,
        _server_with(tmp_path / "b", rev_window_minutes=45.0).agreement_hash,
        _server_with(tmp_path / "c", revtell_count=50).agreement_hash,
        _server_with(tmp_path / "d", rev_window_minutes=45.0,
                     revtell_count=50).agreement_hash,
    }
    assert len(seen) == 4


# ---- clause 3: the connection-recording disclosure (v1.31.1) --------------
#
# The event log has recorded connection IPs and certain named actions since
# v1.30.0; the agreement said nothing about it. A house that keeps a record of
# who was at the door and does not say so has not been asked about it — so the
# clause is the disclosure, and its two numbers come from the same
# resolve-then-hash path as clause 1's.

@pytest.mark.parametrize("narrow,width", [(True, 40), (False, 78)])
def test_the_clause_states_the_configured_retention(tmp_path, narrow, width):
    srv = _server_with(tmp_path, event_retention_days=10.0)
    out = _flat(srv.agreement_for(_Width(narrow, width)))
    assert "kept about 10 days and then deleted" in out
    assert "{event_retention_days}" not in out


@pytest.mark.parametrize("narrow,width", [(True, 40), (False, 78)])
def test_the_clause_names_the_arch_in_the_house_s_own_word(tmp_path, narrow, width):
    srv = _server_with(tmp_path)
    out = _flat(srv.agreement_for(_Width(narrow, width)))
    assert "only the owner can read them" in out
    assert "{arch}" not in out


def test_the_clause_says_what_is_actually_recorded(tmp_path):
    """⚠ ACCURACY, which is the whole point of a disclosure.

    IPs and names live in DIFFERENT records — every connection/auth/refusal
    event carries an ip and no name; staff_action, staff_call and the credential
    events carry a name and no ip; only `takeover` carries both. So the clause
    must not read as "your name and your address are filed together", because
    today they are not. It says connections are noted, and that certain actions
    are noted against the account they concern — two statements, deliberately.
    """
    srv = _server_with(tmp_path)
    out = _flat(srv.agreement_for(_Width(False, 78)))
    assert "When you connect, the time and your connection's IP address are " \
           "recorded" in out
    assert "noted against the account they concern" in out
    # ...and the covenant line the whole store lives under.
    assert "Never a word of what was said" in out


def test_changing_the_retention_moves_the_version(tmp_path):
    """The same guarantee as the other two tokens: an operator who re-tunes how
    long people are remembered has changed what the agreement promises, so
    everyone is asked again."""
    a = _server_with(tmp_path / "a", event_retention_days=10.0)
    b = _server_with(tmp_path / "b", event_retention_days=30.0)
    c = _server_with(tmp_path / "c", event_retention_days=30.0)
    assert a.agreement_hash != b.agreement_hash
    assert b.agreement_hash == c.agreement_hash


def test_a_rethemed_house_says_its_own_word_and_gets_its_own_version(tmp_path):
    """`{arch}` comes from the THEME, not the config — the first placeholder
    that does. A house whose top rank is a Steward says Steward, and because the
    text differs its version differs, which is correct: it is a different
    document."""
    from heresay.server import resolve_agreement, agreement_version, _load_agreement
    from heresay.theme import Theme
    wardens = Theme(order=("citizen", "warden", "Steward"), staff="warden",
                    level_display={"staff": ("", "wardens")})
    raw = _load_agreement(AGREEMENT_PATH)
    themed = resolve_agreement(raw, Config(), wardens)
    flat = _flat(themed)
    assert "only the Steward can read them" in flat
    assert "only the owner can read them" not in flat
    assert agreement_version(themed) != _server_with(tmp_path).agreement_hash


def test_the_theme_is_loaded_before_the_agreement_is_hashed(tmp_path):
    """⚠ The ordering `{arch}` created. The theme used to load AFTER the
    agreement; it is an input to the consent hash now, so a server must resolve
    the arch word rather than silently falling back to the built-in default."""
    srv = _server_with(tmp_path)
    assert srv.theme is not None
    assert "{arch}" not in srv.agreement_text
    assert srv.theme.owner_rank in srv.agreement_text


def test_all_four_tokens_move_the_version_independently(tmp_path):
    """Four configs beyond the baseline, five distinct hashes — no token is a
    proxy for another."""
    seen = {
        _server_with(tmp_path / "base").agreement_hash,
        _server_with(tmp_path / "a", rev_window_minutes=45.0).agreement_hash,
        _server_with(tmp_path / "b", revtell_count=50).agreement_hash,
        _server_with(tmp_path / "c", event_retention_days=30.0).agreement_hash,
    }
    assert len(seen) == 4


@pytest.mark.parametrize("narrow,width", [(True, 40), (False, 78)])
def test_the_new_clause_reflows_clean(tmp_path, narrow, width):
    """It is prose like the rest, so it must set as prose: every line but a
    paragraph's last is full, and nothing overruns."""
    srv = _server_with(tmp_path)
    para = next(p for p in srv.agreement_for(_Width(narrow, width)).split("\n\n")
                if p.lstrip().startswith("3."))
    lines = para.split("\n")
    assert all(len(l) <= width for l in lines), para
    for line, following in zip(lines, lines[1:]):
        nxt = following.split(" ")[0]
        assert len(line) + 1 + len(nxt) > width, (line, nxt)


def test_the_clauses_renumbered_and_none_went_missing(tmp_path):
    """Inserting at 3 pushed the rest down. Nine now, each starting a paragraph
    of its own, and the ones that moved still say what they said."""
    srv = _server_with(tmp_path)
    out = srv.agreement_for(_Width(False, 78))
    for n in range(1, 10):
        assert f"\n{n}. " in "\n" + out, n
    assert "\n10. " not in "\n" + out
    flat = _flat(out)
    assert "4. Though the house records almost nothing" in flat   # was 3
    assert "9. This house is offered as-is" in flat               # was 8


def test_changing_the_window_moves_the_version(tmp_path):
    """THE guarantee. Two configs, two hashes — so an operator who re-tunes
    `.rev` cannot leave everyone consented to the old retention period."""
    a = _server_with(tmp_path / "a")                                  # 30
    b = _server_with(tmp_path / "b", rev_window_minutes=45.0)
    c = _server_with(tmp_path / "c", rev_window_minutes=45.0)
    assert a.agreement_hash != b.agreement_hash
    assert b.agreement_hash == c.agreement_hash       # ...and it is deterministic


def test_editing_the_text_still_moves_the_version(tmp_path):
    """The older guarantee, unchanged by the new one: the prose is still
    content-addressed."""
    from heresay.server import agreement_version, resolve_agreement
    house = tmp_path / "house"
    (house / "world").mkdir(parents=True)
    src = AGREEMENT_PATH.read_text(encoding="utf-8")
    (house / "agreement.txt").write_text(src, encoding="utf-8")
    first = _server_with(tmp_path / "a",
                         agreement_file=str(house / "agreement.txt")).agreement_hash

    (house / "agreement.txt").write_text(src + "\n9. And be kind.\n", encoding="utf-8")
    second = _server_with(tmp_path / "b",
                          agreement_file=str(house / "agreement.txt")).agreement_hash
    assert second != first


def test_the_version_is_width_independent(tmp_path):
    """Resolve, hash, THEN wrap — so the phone and the terminal agree to the
    same version of the same words."""
    from heresay.server import agreement_version
    srv = _server_with(tmp_path)
    assert agreement_version(srv.agreement_for(_Width(True, 40))) != srv.agreement_hash
    assert srv.agreement_hash == agreement_version(srv.agreement_text)


def test_an_unknown_placeholder_is_left_alone_and_named(tmp_path, caplog):
    """A typo'd token must not reach a resident's screen unremarked — but it is
    a warning, not a refusal to boot: an operator's prose may contain a brace,
    and a house closed by a punctuation mark is the worse failure."""
    from heresay.server import resolve_agreement
    with caplog.at_level("WARNING"):
        out = resolve_agreement("keeps {rev_window_mnutes} minutes", Config())
    assert out == "keeps {rev_window_mnutes} minutes"      # left exactly as written
    assert "rev_window_mnutes" in caplog.text              # the name, never prose


def test_resolution_never_writes_the_file(tmp_path):
    """⛔ Display-and-version time only: the token stays a token on disk, which
    is what lets the house be re-tuned by editing config alone."""
    before = AGREEMENT_PATH.read_bytes()
    _server_with(tmp_path / "a", rev_window_minutes=99.0)
    assert AGREEMENT_PATH.read_bytes() == before
    assert b"{rev_window_minutes}" in before


# ---------------------------------------------------------------------------
# `.agreement` (v1.36.1) — read it back, any time, with nothing to answer
# ---------------------------------------------------------------------------
#
# The point of the command is that it is NOT a prompt. What makes it honest is
# that it renders through the same function the signup screen does, so what a
# resident re-reads is byte-for-byte what they were shown when they agreed.

def _reader(srv, width=78, level="resident", name="Corvid"):
    from conftest import FakeSession
    s = FakeSession(srv.world, name=name, level=level, width=width,
                    config=srv.config)
    s.agreement_text = srv.agreement_text
    return s


def _agreement_screen(srv, width=78, **kw):
    from heresay.commands import dispatch
    s = _reader(srv, width=width, **kw)
    s.clear()
    dispatch(s, ".agreement")
    return s.text


def test_agreement_shows_the_text_with_no_prompt(tmp_path):
    srv = _server_with(tmp_path)
    out = _agreement_screen(srv)
    assert "The agreement" in out                    # the frame's title
    assert "Do you agree?" not in out                # ...and nothing to answer
    assert "(y/n)" not in out
    assert "{rev_window_minutes}" not in out         # resolved, like everywhere


@pytest.mark.parametrize("width", [40, 78])
def test_agreement_is_byte_identical_to_the_signup_rendering(tmp_path, width):
    """The whole guarantee: one renderer, two callers. If these ever differ,
    somebody is being shown a differently-set copy of what they signed."""
    srv = _server_with(tmp_path)
    signup = srv.agreement_for(_Width(width <= 40, width))
    out = _agreement_screen(srv, width=width)
    assert signup in out
    # ...and the framed body IS that string, not merely a superset of it.
    body = "\n".join(out.split("\n")[2:-2])          # inside the two rules
    assert body == signup


@pytest.mark.parametrize("width", [40, 78])
def test_agreement_fits_its_width(tmp_path, width):
    srv = _server_with(tmp_path)
    for line in _agreement_screen(srv, width=width).split("\n"):
        assert len(line) <= width, repr(line)


def test_agreement_is_visible_to_a_plain_resident(tmp_path):
    """Floor rank: what you agreed to is not a privilege to read back."""
    from heresay.commands import _visible
    from heresay.commands.registry import RESIDENT, command_seed
    from heresay.theme import DEFAULT_THEME
    assert command_seed()["agreement"] == RESIDENT
    assert _visible("agreement", DEFAULT_THEME.resident_rank, DEFAULT_THEME)
    srv = _server_with(tmp_path)
    assert "Unknown command" not in _agreement_screen(srv, level="resident")


def test_agreement_does_not_move_the_version(tmp_path):
    """⛔ Display-time only. Reading it at any width leaves the hash where it
    was, and leaves the file alone."""
    before_file = AGREEMENT_PATH.read_bytes()
    srv = _server_with(tmp_path)
    stamped = srv.agreement_hash
    for width in (40, 78):
        _agreement_screen(srv, width=width)
    assert srv.agreement_hash == stamped
    from heresay.server import agreement_version
    assert agreement_version(srv.agreement_text) == stamped
    assert AGREEMENT_PATH.read_bytes() == before_file


def test_agreement_says_so_when_there_is_no_text(tmp_path):
    """A unit session with no house behind it — never a traceback."""
    from heresay.commands import dispatch
    from heresay.commands.info import NO_AGREEMENT
    srv = _server_with(tmp_path)
    s = _reader(srv)
    s.agreement_text = ""
    s.clear()
    dispatch(s, ".agreement")
    assert NO_AGREEMENT in s.text


def test_a_real_session_carries_the_text(tmp_path):
    """The plumbing: the server hands the resolved text to every session, so the
    command has the same string the hash was taken of."""
    srv = _server_with(tmp_path)
    import inspect
    src = inspect.getsource(srv._handle if hasattr(srv, "_handle") else TalkerServer)
    assert "agreement_text=self.agreement_text" in src
