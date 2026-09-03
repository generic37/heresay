# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`forbidden_names.txt` — names the operator will not have registered.

**Whose list this is.** The engine already reserves names it cannot let anyone
take: every rank on the ladder, because a rank word IS an identity here
(`.call_staff` means the rank, the shared letterbox is addressed to it), and the
arch account's name. Those are INTEGRITY reservations — the house would be
incoherent without them — and they are built in and non-removable.

This file is the other kind: the operator's own taste and their own trouble.
Impersonation bait, `admin` and `root`, slurs, a brand they would rather not host,
the name of somebody who keeps coming back. The engine has no opinion about any
of it and does not need one; it reads the list and enforces it.

⛔ **ADDITIVE, and only additive.** `forbidden(theme, path)` is *the file ∪ every
rank name*. Nothing in the file can UNRESERVE a rank — an operator editing a text
file must not be able to make a RANK NAME registrable and hand the next registrant a
word the house uses to mean something.

**The format is deliberately the dullest one available:** one name per line,
`#` comments, blank lines ignored, whitespace stripped, casefolded on load. Not
TOML, because there is no structure here to express — a list of words wants to be
a list of words, editable by somebody who has never seen a config format. An
unparseable line does not exist: everything that is not a comment is a name.

⛔ **THE DAEMON NEVER WRITES IT** — the same rule, and the same production
incident behind it, as `permissions.toml`: `/etc` is mounted read-only for the
service, and a daemon that must be able to write its own configuration is one
whose configuration is not protected. Absent is simply the empty list.

⚠ **The collision rule here is SOFT, and it is not the rank↔account rule.** A
rank name colliding with an existing account is a hard boot failure (the house
cannot mean two things by one word). A FORBIDDEN name colliding with an existing
account is not: the holder is **grandfathered**, keeps their name, and only new
registrations are blocked. The reasons are different in kind —

* the rank collision breaks the engine's own vocabulary, and there is no reading
  of the house in which both can stand;
* this one is the operator changing their mind about what they want hosted,
  which is a decision about the FUTURE. Renaming or deleting somebody because a
  word was added to a text file is a far bigger act than the file is asking for,
  and it is not the file's to take.

⛔ **The grandfather warning is a COUNT, never a name.** "N existing account(s)
hold a now-forbidden name" is the fact an operator needs — it tells them the edit
has landed on somebody — and *which* somebody is a thing to go and look up
deliberately, not something a log line hands over on the way past. Same posture
as `.security`'s six numbers.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("heresay")


def parse(text: str) -> set[str]:
    """The file's text → a set of casefolded names.

    Effectively cannot fail, and that is the design: every non-comment line is a
    name, so there is no syntax to get wrong and no reason this source could
    ever reject a reload.
    """
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            # ⚠ casefold(), not lower() (v1.47.0). The registration check has
            # always been `name.casefold()`, and these two sets are compared
            # against it — identical on the strict-ASCII alphabet `valid_name`
            # allows, and NOT identical outside it ("Straße".lower() is not
            # "Straße".casefold()). No bug today; one vocabulary so that
            # widening `valid_name` later cannot open a bypass in silence.
            names.add(line.casefold())
    return names


def load(path) -> set[str]:
    """Read the file if it is there. **Reads only; never writes, ever.**

    A missing file is the empty set — an operator who never creates one simply
    has no list of their own, which is exactly what the house did before the
    file existed. An UNREADABLE file (a mode, a race) is reported and also reads
    as empty: a blocklist the daemon cannot open must not be the reason a house
    will not start.
    """
    p = Path(path)
    if not p.exists():
        return set()
    try:
        return parse(p.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("%s: could not read (%s) — no operator name blocks", p, exc)
        return set()


def rank_names(theme) -> set[str]:
    """The built-in half: every rank on the ladder, casefolded.

    ⚠ Non-removable, and not the operator's to edit. A rank word is an identity
    in this engine — the shared letterbox is addressed to a class of people and
    `.call_staff` pages one — so a house running with an account whose NAME is a
    rank is a house where naming that class no longer names only the class.
    """
    return {r.casefold() for r in getattr(theme, "order", ())}


def forbidden(theme, operator_names=()) -> set[str]:
    """Everything that may not be registered: the operator's list ∪ the ranks."""
    return set(operator_names) | rank_names(theme)


def grandfathered(names, accounts) -> int:
    """How many EXISTING accounts hold a name on the list.

    A count, deliberately — see the module note. Returned rather than logged so
    the caller decides where it is said (boot log, `.load_report`, a reload
    diff); this function knows nothing about any of them.
    """
    if not names:
        return 0
    return sum(1 for a in accounts.all() if a.name.casefold() in names)
