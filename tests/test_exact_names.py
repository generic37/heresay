# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""⛔ A name is a complete match or it is not a match (v1.52.0).

Abbreviated name matching is gone. Case is still free — `wren`, `Wren` and
`WREN` are one person — but `.tell wr` no longer finds Wren.

**Why.** A mis-resolved name is not a typo, it is an action taken on the wrong
person: a `.tell` is a private message delivered to somebody who was never meant
to read it — a privacy incident with no undo — and on `.ban` / `.boot` / `.jail`
/ `.promote` it is moderation applied to a stranger. The maintainer's call. It costs almost
nothing to give up: `>` repeats to a sticky correspondent, so the common case of
talking to one person was already abbreviation-free.

⛔ **Fixed at the two RESOLVERS, not at the ~13 call sites** — so every command
inherits it at once, and a command written next year cannot reintroduce fuzzy
matching by forgetting to ask for exactness. The guard test at the bottom is the
whole point of having done it that way.
"""

import inspect

import pytest

from heresay.accounts import AccountStore
from heresay.commands import dispatch

from conftest import FakeSession, seed_account


@pytest.fixture
def accounts(tmp_path):
    store = AccountStore(tmp_path / "accounts.json")
    for n in ("Wren", "Corvid", "Wrenfield", "Bob"):
        seed_account(store, n, "correct horse battery")
    return store


# ---------------------------------------------------------------------------
# 1. AccountStore.resolve — the offline half
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("typed", ["Wren", "wren", "WREN", "wReN", "  wren  "])
def test_an_exact_name_resolves_in_any_case(accounts, typed):
    """Case-insensitivity is kept in full — it is abbreviation that went."""
    assert accounts.resolve(typed) == ("Wren", False)


@pytest.mark.parametrize("typed", ["w", "wr", "wre", "corv", "wrenfiel"])
def test_a_PREFIX_no_longer_resolves(accounts, typed):
    """⛔ The change. Every one of these used to find somebody."""
    assert accounts.resolve(typed) == (None, False)


def test_a_prefix_of_ONE_account_is_still_refused(accounts):
    """⚠ Even where it is unambiguous. "Unambiguous" was the old rule and it is
    not the new one — `corv` names nobody, and the fact that only one person
    could have been meant does not make it a name."""
    assert accounts.resolve("corv") == (None, False)
    assert accounts.resolve("Corvid") == ("Corvid", False)


def test_a_longer_name_sharing_a_prefix_is_untouched(accounts):
    """`Wren` and `Wrenfield` coexist; each answers to itself and nothing else.
    Under the old resolver `wren` was an exact hit and `wrenf` an unambiguous
    prefix — now only the full names work."""
    assert accounts.resolve("Wren") == ("Wren", False)
    assert accounts.resolve("Wrenfield") == ("Wrenfield", False)
    assert accounts.resolve("wrenf") == (None, False)


def test_the_ambiguous_flag_is_now_always_False(accounts):
    """⚠ The tuple shape is KEPT so the ~13 callers are untouched, and the flag
    can no longer be True: an exact casefold key is unique by construction,
    because the store is keyed on it.

    The callers' "Who do you mean by X?" branches therefore remain, correct
    against the declared contract and simply never reached. That is deliberate —
    rewriting thirteen call sites to drop a flag would be a far larger change
    than the fix, and would leave them wrong if this ever returned True again.
    """
    for typed in ("", "  ", "w", "wren", "WREN", "nobody", "wrenf", "Wrenfield"):
        _name, ambiguous = accounts.resolve(typed)
        assert ambiguous is False, typed


def test_an_empty_or_missing_name_is_a_plain_miss(accounts):
    assert accounts.resolve("") == (None, False)
    assert accounts.resolve("   ") == (None, False)
    assert accounts.resolve("Nobody") == (None, False)


# ---------------------------------------------------------------------------
# 2. World.resolve_resident — the live half
# ---------------------------------------------------------------------------

def test_resolve_resident_is_exact_and_case_insensitive(world, clock):
    FakeSession(world, name="Corvid", clock=clock)
    FakeSession(world, name="Wren", clock=clock)
    for typed in ("Wren", "wren", "WREN"):
        res, ambiguous = world.resolve_resident(typed)
        assert res is not None and res.name == "Wren", typed
        assert ambiguous is False
    for typed in ("w", "wr", "wre", "corv"):
        assert world.resolve_resident(typed) == (None, False), typed


def test_a_disconnected_resident_still_does_not_resolve(world, clock):
    """⚠ Unchanged. The old prefix scan filtered on `connected` too, so a
    disconnected exact match was never findable here either."""
    s = FakeSession(world, name="Wren", clock=clock)
    s.resident.session = None
    assert world.resolve_resident("Wren") == (None, False)


# ---------------------------------------------------------------------------
# 3. End to end — the two commands where getting it wrong costs most
# ---------------------------------------------------------------------------

def test_tell_reaches_an_exact_name_and_NOT_a_prefix(world, clock, accounts):
    """⛔ The privacy case. A mis-delivered `.tell` has no undo."""
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    b = FakeSession(world, name="Wren", clock=clock, accounts=accounts)

    dispatch(a, ".tell Wren the exact one")
    assert ">> Corvid says: the exact one" in b.text

    # A fresh sender, so there is no sticky slot to fall back to and the
    # abbreviation is judged entirely on its own.
    c = FakeSession(world, name="Corvid2", clock=clock, accounts=accounts)
    b.clear()
    dispatch(c, ".tell wr the abbreviated one")
    assert ">> Corvid2 says: the abbreviated one" not in b.text, \
        "a prefix still delivered a private message"
    assert not b.text, "a prefix reached Wren by some other route"


def test_a_prefix_FALLS_THROUGH_to_the_sticky_correspondent(world, clock,
                                                            accounts):
    """⚠⚠ THE RESIDUAL, WRITTEN DOWN RATHER THAN LEFT TO BE DISCOVERED.

    `.tell` falls back to the sticky correspondent when the first word is not a
    name — that is what makes `.t hello there` work. A word that names NOBODY
    is indistinguishable from the start of a sentence, so an abbreviation now
    lands there instead of resolving.

    In this test the correspondent IS the person who was meant, so nothing goes
    astray. It goes astray only when the sticky slot holds somebody ELSE — and
    the mitigation for that is the guard above (a first word that names a real
    account never falls through) plus the fact that abbreviations no longer
    appear to work, so the habit goes.

    ⛔ Recorded as a KNOWN limit of the fallback, not as an accident. If it is
    ever judged too sharp, the fix is to drop the fallback for name-shaped first
    words — which would cost `.t will you come`, and is the maintainer's call, not a
    silent one.
    """
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    b = FakeSession(world, name="Wren", clock=clock, accounts=accounts)
    dispatch(a, ".tell Wren hello")            # slot = Wren
    b.clear()
    dispatch(a, ".tell wr and this")
    # The whole thing arrives as text — NOT parsed as a name.
    assert ">> Corvid says: wr and this" in b.text


def test_the_miss_reads_naturally_and_does_not_hint_at_prefixes(world, clock,
                                                                accounts):
    """⚠ The wording matters: somebody who typed an abbreviation must not be
    told to try a shorter one, and must not learn that it ever worked."""
    s = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    FakeSession(world, name="Wren", clock=clock, accounts=accounts)
    for cmd in (".mute wr", ".examine wr", ".tell wr hello"):
        s.clear()
        dispatch(s, cmd)
        said = s.text.lower()
        for leak in ("prefix", "abbrevi", "be more specific", "unambiguous",
                     "full name", "type more", "shorter"):
            assert leak not in said, f"{cmd} hinted at {leak!r}: {said!r}"
    # And the plain miss, where a miss is what happens, reads naturally.
    s.clear()
    dispatch(s, ".examine wr")
    assert "no one called" in s.text.lower()


def test_ban_will_not_act_on_a_prefix(world, clock, accounts, tmp_path):
    """⛔ The moderation case: acting on somebody you did not name.

    ⚠ The property is "Wren is not banned", not "the command refuses". `.ban`
    deliberately accepts a name with no account behind it — that is how a name
    somebody keeps re-registering gets banned before they take it again
    (`subject = display or subject`, and it predates this release). So `.ban wr`
    bans the literal string `wr`, which belongs to nobody. What matters, and
    what used to be false, is that it no longer reaches Wren.
    """
    from heresay.bans import BanStore
    bans = BanStore(tmp_path / "bans.json")
    arch = FakeSession(world, name="Generic", clock=clock, accounts=accounts,
                       bans=bans, level="arch")
    dispatch(arch, ".ban wr 1")
    # ⚠ Asserted on what the command DID, not on `is_banned_name` — a
    # FakeSession's monotonic clock and the store's wall clock are different
    # epochs, so a freshly-placed ban reads as long expired here.
    assert "Banned Wren" not in arch.text, "a prefix banned somebody"
    assert "Banned wr" in arch.text        # it banned the string, i.e. nobody
    arch.clear()
    dispatch(arch, ".ban Wren 1")
    assert "Banned Wren" in arch.text


def test_mute_will_not_act_on_a_prefix(world, clock, accounts):
    """The same shape as `.ban`: `.mute wr` mutes a string nobody answers to,
    and Wren goes on being heard."""
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    b = FakeSession(world, name="Wren", clock=clock, accounts=accounts)
    dispatch(a, ".mute wr")
    assert "wren" not in a.resident.muted, "a prefix muted somebody"
    a.clear()
    dispatch(a, ".mute Wren")
    assert "wren" in a.resident.muted


# ---------------------------------------------------------------------------
# 4. ⛔ The guard — a future change cannot quietly restore prefix matching
# ---------------------------------------------------------------------------

def test_the_resolvers_contain_no_prefix_matching():
    """⛔ THE POINT OF FIXING IT CENTRALLY.

    Both resolvers are read as source and must contain no prefix-shaped match.
    A behavioural test only catches the cases somebody thought to write down;
    this catches `startswith` coming back at all, in either resolver, however it
    is reintroduced.
    """
    from heresay.accounts import AccountStore as A
    from heresay.world import World

    for fn in (A.resolve, World.resolve_resident):
        src = inspect.getsource(fn)
        body = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        # Strip the docstring, which legitimately discusses prefixes.
        if '"""' in body:
            head, _, rest = body.partition('"""')
            _doc, _, tail = rest.partition('"""')
            body = head + tail
        assert "startswith" not in body, f"{fn.__qualname__} matches prefixes again"
        assert ".casefold()" in body, f"{fn.__qualname__} stopped folding case"


def test_no_command_handler_prefix_matches_a_FOLDED_name():
    """⛔ THE GAP THE GUARD ABOVE COULD NOT SEE (v1.52.4).

    That sweep reads the two RESOLVERS, which is right — they are where the ~13
    call sites get their answer. But a handler can match a name WITHOUT asking a
    resolver, and one did: `cmd_join` compared the typed text against the
    inviter's own name with `inv.inviter.casefold().startswith(name.casefold())`,
    so `.join corv` teleported you to Corvid for four releases after prefix
    matching was supposedly gone.

    So the command layer is swept too, for the idiom that means "match a name
    loosely": a `startswith` on the same line as a `casefold`. Case-folding is
    what makes a comparison a NAME comparison here — `line.startswith(".")` and
    `low.startswith(".afk ")` are about command syntax and are untouched by it.
    """
    import pkgutil
    from pathlib import Path

    import heresay.commands as pkg

    root = Path(pkg.__path__[0])
    # ⚠ `iter_modules` does not list `__init__.py`, and `cmd_help` lives there —
    # a sweep of "the command layer" that skipped the module holding a handler
    # would be exactly the shape of hole this test exists to close.
    names = [m.name for m in pkgutil.iter_modules(pkg.__path__)] + ["__init__"]
    offenders = []
    for name in names:
        src = (root / f"{name}.py").read_text(encoding="utf-8")
        for n, line in enumerate(src.splitlines(), 1):
            bare = line.strip()
            if bare.startswith("#") or bare.startswith("*"):
                continue                       # a comment discussing the rule
            if "startswith" in line and "casefold" in line:
                offenders.append(f"{name}.py:{n}: {bare}")
    assert not offenders, ("a handler matches a name by prefix again:\n"
                           + "\n".join(offenders))


def test_join_needs_the_whole_inviter_name(world, clock):
    """The behavioural half of it, beside the sweep — because a guard that reads
    source proves the idiom is gone, not that the command is right."""
    from heresay.commands import dispatch as d
    a = FakeSession(world, name="Corvid", room="oak", clock=clock)
    b = FakeSession(world, name="Wren", clock=clock)
    d(a, ".invite Wren")
    d(b, ".join corv")
    assert b.resident.room_id == "atrium", "a prefix teleported somebody"
    d(b, ".join Corvid")
    assert b.resident.room_id == "oak"


def test_the_unrelated_resolve_methods_were_NOT_touched():
    """⚠ Three methods share the name and only two are about people.

    `config.resolve` turns a configured path into an absolute one, and
    `ResumeStore.resolve` looks a token hash up. Neither has anything to do with
    names, and neither should have been anywhere near this change.
    """
    from heresay.config import Config
    from heresay.resume import ResumeStore

    cfg_src = inspect.getsource(Config.resolve)
    assert "Path" in cfg_src or "path" in cfg_src
    assert "casefold" not in cfg_src

    res_src = inspect.getsource(ResumeStore.resolve)
    assert "token_hash" in res_src


def test_the_other_name_paths_were_already_exact_and_remain_so(accounts):
    """`accounts.get` / `.exists` and `world.get_resident` have always been
    exact casefold lookups — the fix did not need to reach them, and this says
    so in a way that would notice if one drifted."""
    assert accounts.get("wren") is not None
    assert accounts.get("wr") is None
    assert accounts.exists("WREN") and not accounts.exists("wre")


def test_room_names_still_accept_a_prefix():
    """⚠ Deliberately NOT changed, and the distinction is the argument.

    `World.resolve_room` is a different resolver for a different kind of thing.
    Going to the wrong room is a navigation annoyance you see immediately and
    walk back from; naming the wrong person sends them a private message or bans
    them. `.go tea` still reaches the parlour, and `help/go.txt` still says so.
    """
    import pathlib

    from heresay.world import load_world
    world = load_world(
        pathlib.Path(__file__).resolve().parent / "fixtures" / "house" / "world",
        "atrium", "holding_cell")
    room_id, ambiguous = world.resolve_room("parl")
    assert room_id is not None and not ambiguous


# ---------------------------------------------------------------------------
# 5. ⛔ The no-body case the v1.52.0 guard did NOT cover (external review, r3)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb", [".tell", ".remote"])
def test_naming_an_OFFLINE_account_with_NO_BODY_never_reaches_the_correspondent(
        world, clock, accounts, verb):
    """⛔ THE GAP THE REVIEWER FOUND, and it is mine: v1.52.0's guard read
    `if named is not None and rest:` — narrowing it to messages that carry a
    BODY.

    So `.tell Wren` on its own, with Wren a real account that is offline and a
    sticky correspondent already set, skipped the guard entirely and fell
    through — privately sending Bob the bare word "Wren". The person typing it
    was addressing Wren; somebody else received it.

    ⚠ It is also inconsistent with the ONLINE path, which answers a bodyless
    `.tell Wren` with "Tell Wren what?" rather than sending anything anywhere.

    ⚠ DISTINCT from the documented residual below: this first word names a REAL
    ACCOUNT. A first word naming nobody still falls through, deliberately.

    `.tell` and `.remote` both route through `_private_command`, so one guard
    covers both — parametrised here so neither can regress alone.
    """
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    bob = FakeSession(world, name="Bob", clock=clock, accounts=accounts)
    # Wren is registered in `accounts` but never connected — offline.
    assert accounts.get("Wren") is not None
    assert world.resolve_resident("Wren") == (None, False)

    dispatch(a, ".tell Bob hello")             # sticky correspondent = Bob
    a.clear(); bob.clear()

    dispatch(a, f"{verb} Wren")                # no body at all
    assert "no one called Wren online" in a.text, a.text
    assert not bob.text, f"the bare name reached the correspondent: {bob.text!r}"


def test_the_bodyless_guard_did_not_disturb_the_ONLINE_no_body_answer(
        world, clock, accounts):
    """⚠ The neighbouring behaviour, pinned. Naming somebody who IS online with
    no body still asks what to say — the guard must not have swallowed that."""
    a = FakeSession(world, name="Corvid", clock=clock, accounts=accounts)
    FakeSession(world, name="Bob", clock=clock, accounts=accounts)
    dispatch(a, ".tell Bob")
    assert "Tell Bob what?" in a.text
