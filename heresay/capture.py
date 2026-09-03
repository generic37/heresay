# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The report capture file — the one place the engine writes a report to disk.

⛔⛔ READ THIS BEFORE CHANGING ANYTHING HERE. This module is a DELIBERATE,
NAMED, BOUNDED EXCEPTION to the rule that the engine writes facts and never
content, and it is the operator's own deliberate decision. It is not a precedent
for anything.

**The problem it solves, and the inversion that solves it.** A report used to
OUTLIVE ITS OWN EVIDENCE: the box held it 100 days while the context — who
connected, from where, what staff did — aged out of `events.jsonl` at ten. So
at day eleven you could read *"one resident says another did X on the 3rd"*
and no record was left of whether the accused had even been connected that
day. Ninety
days of an accusation with nothing to check it against.

The fix inverts the two: **capture EVERYTHING to disk at the instant of filing,
then let the in-system copy expire like everything else.** The engine goes back
to holding nothing durable about a report; the operator's disk holds the
record. `report_retention_days` drops to ten to match the log, because a house
whose reports are preserved at filing has no reason to hoard them in the box.

**Why AT FILING, in-daemon, and nowhere else.** `report_filed` deliberately
carries no names (see events.py), so the daemon is the only thing that knows
WHO to capture — and by the time any external watcher or human review looked,
the ten-day window would already have moved. There is exactly one moment when
the whole picture exists, and this is it.

⛔ **NOTHING IN THE ENGINE EVER READS THIS BACK.** No command, no rank, no
`.security` row, no `.load_report` line, no future admin panel. Write-only from
the engine's side, read with `less` over ssh by somebody who is on the box. A
test greps the engine for any read path into this directory, so a later command
cannot quietly gain one.

⚠ **WHAT THIS FILE CONTAINS, stated plainly rather than softened:** an
accusation in PLAINTEXT, naming both people, with the full text, plus every
address either account was seen on inside the window. The mail store's
encryption protects a report from THE SYSTEM — from a stolen disk, a casual
file read, a backup gone astray. This file is protected by a different thing:
being 0600 inside a 0700 state directory on a box only the operator reaches.
Those are different guarantees and the second one is weaker in one specific
way, which the release notes state: **the nightly tarball carries this
directory** (it is the record, so it must), and that tarball leaves the machine
weekly. The ciphertext in it is inert without `mail.key`; these files are not.

⚠ **0600 AND OWNED BY THE DAEMON USER — not root.** The unit runs as
`User=heresay`, with an empty `CapabilityBoundingSet`, so the daemon cannot
chown to root and must not try. What it can guarantee is the mode and the
directory, and `StateDirectoryMode=0700` does the rest: root and the `heresay`
user, nobody else. The operator reads it with `sudo less`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("heresay")

DAY_SECONDS = 86400

#: Directory mode. The state dir is already 0700 under systemd; this is the
#: belt for a hand-made one, and for `make run-dev`.
DIR_MODE = 0o700
#: File mode. ⚠ Set with `os.open(..., 0o600)` rather than a later `chmod`, so
#: the file is never briefly readable — the same reasoning as `mail.key`.
FILE_MODE = 0o600

_RULE = "-" * 74


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _stamp(ts: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(ts))


def _section(title: str) -> str:
    """A rule with the title set into it — the frame convention, in a file."""
    head = f"---- {title} "
    return head + "-" * max(4, 78 - len(head))


def _names_in(row: dict) -> set[str]:
    """Every ACCOUNT NAME a row mentions, casefolded.

    ⚠ The name-bearing fields differ by type and always have (see events.py's
    table): `account`, `name`, `actor`, `target`. Listing them is deliberate —
    a scan of every string value would also match an IP-shaped or reason-shaped
    value and quietly widen what lands in the file.
    """
    out = set()
    for key in ("account", "name", "actor", "target"):
        v = row.get(key)
        if isinstance(v, str) and v:
            out.add(v.casefold())
    return out


def _addresses_in(row: dict) -> set[str]:
    out = set()
    for key in ("ip", "from_ip"):
        v = row.get(key)
        if isinstance(v, str) and v:
            out.add(v)
    return out


def relevant_events(rows, names) -> list[dict]:
    """Every row about either account, plus every row on an address they used.

    TWO PASSES, and the second is the one that earns the module its keep:

    1. rows that NAME either account (`login`, `staff_action` as actor AND as
       target, `staff_call`, `staff_mail`, `takeover`, `account_security`,
       `account_deleted`, `bad_frame` when the session had reached a name);
    2. rows carrying an ADDRESS that pass 1 found — which is how `connection`,
       `auth_fail`, `refused`, `flood_disconnect` and `login_timeout` become
       *about* somebody at all. Those types carry no name by design, so a
       name-only sweep would drop the entire connection history and leave the
       capture unable to answer "was he even here that day?".

    ⚠ ORDER IS PRESERVED — the store's own, which breaks a timestamp tie by
    file position. A capture that re-sorted its rows would be answering a
    different question from the one `.staff_actions` answers.
    """
    want = {n.casefold() for n in names if n}
    named = [r for r in rows if _names_in(r) & want]
    addrs: set[str] = set()
    for r in named:
        addrs |= _addresses_in(r)
    if not addrs:
        return named
    keep = []
    seen = set()
    for i, r in enumerate(rows):
        if (_names_in(r) & want) or (_addresses_in(r) & addrs):
            if i not in seen:
                seen.add(i)
                keep.append(r)
    return keep


def _account_block(store, name: str, role: str, bans=None, history=None) -> str:
    """Everything the account store holds, plus this account's ban standing."""
    lines = [_section(f"ACCOUNT: {name}  ({role})"), ""]
    acct = store.get(name) if store is not None else None
    if acct is None:
        lines += ["(no such account — deleted, or never existed)", ""]
        return "\n".join(lines)
    lines += [
        f"name             {acct.name}",
        f"created          {acct.created or '(unrecorded)'}",
        f"rank             {acct.rank or '(the floor)'}",
        f"description      {acct.description or '(none)'}",
        f"agreed           {acct.agreed or '(unrecorded)'}",
        f"agreed_hash      {acct.agreed_hash or '(none — pre-versioning)'}",
        f"recovery codes   {len(acct.recovery)} held",
        f"settings         {json.dumps(acct.settings, sort_keys=True)}",
    ]
    profile = (acct.profile or "").strip()
    lines.append(f"profile          {'(none)' if not profile else ''}")
    if profile:
        lines += ["", *("    " + l for l in profile.split("\n"))]

    active = []
    if bans is not None:
        try:
            active = [b for b in bans.list_active()
                      if str(b.get("subject", "")).casefold() == name.casefold()]
        except Exception:                                   # pragma: no cover
            active = []
    lines += ["", f"active bans      {len(active)}"]
    for b in active:
        lines.append("    " + json.dumps(b, sort_keys=True))

    past = []
    if history is not None:
        try:
            past = [h for h in history.all()
                    if str(h.get("account", "")).casefold() == name.casefold()]
        except Exception:                                   # pragma: no cover
            past = []
    lines += [f"ban history      {len(past)}"]
    for h in past:
        lines.append("    " + json.dumps(h, sort_keys=True))
    lines.append("")
    return "\n".join(lines)


def render(reporter: str, accused: str, body: str, filed: float,
           rows, accounts=None, bans=None, history=None,
           window_days: int | None = None) -> str:
    """The whole capture, as text. Pure — a test can read it without a disk."""
    out = [
        "Heresay — report capture",
        "========================",
        "",
        f"Filed        {_iso(filed)}   (epoch {filed:.3f})",
        f"Reporter     {reporter}",
        f"About        {accused}",
        "",
        "This file is THE RECORD. It is written once, at the instant of filing,",
        "and is self-contained: the copy in the report box expires on the",
        "house's ordinary retention and nothing here depends on it.",
        "⛔ Nothing in the engine reads this file back — no command, no rank.",
        "",
        _section("THE REPORT"),
        "",
        body.rstrip("\n"),
        "",
        _account_block(accounts, reporter, "the reporter", bans, history),
        _account_block(accounts, accused, "reported", bans, history),
    ]

    # ⚠ `:g` because the config value is a float: "10.0 day(s)" in a record
    # somebody reads years later is an engineer's number, not a sentence. The
    # same reason the agreement renders its own numbers that way.
    window = ("the event log's whole retention window"
              if window_days is None else f"the last {window_days:g} day(s)")
    out += [
        _section("EVENT LOG"),
        "",
        f"Every row naming either account, and every row on an address either",
        f"was seen on, from {window}. Rows are dumped whole and in the store's",
        "own order — never summarised, never re-sorted.",
        "",
    ]
    if not rows:
        # ⚠ A VALID, CORRECT RESULT — not a failure. A report filed about
        # something that happened weeks ago, or by somebody whose connection
        # records have already aged out, genuinely has no context left. Saying
        # so is the honest capture; refusing to write the file would lose the
        # report text as well, which is the part that cannot be recovered.
        out += ["(nothing — the window holds no row about either account.",
                " A stale report, or an account whose records have aged out.)",
                ""]
    else:
        for r in rows:
            rest = {k: v for k, v in r.items() if k not in ("ts", "type")}
            out.append(f"{_iso(r.get('ts', 0.0))}  {r.get('type','?'):<17} "
                       f"{json.dumps(rest, sort_keys=True)}")
        out += ["", f"({len(rows)} row(s))", ""]
    out += [_RULE, ""]
    return "\n".join(out)


def write_capture(directory, reporter: str, accused: str, body: str,
                  filed: float, rows, accounts=None, bans=None, history=None,
                  window_days: int | None = None) -> Path | None:
    """Write one capture file. Returns its path, or None if it could not be.

    ⛔ NEVER RAISES INTO THE COMMAND PATH. The report itself is already
    delivered and fsynced by the time this runs, so a disk problem here must
    cost the CAPTURE and never the report — and a resident must not learn about
    the operator's filesystem from a traceback. A failure is one log line
    carrying the PATH and nothing else: not the reporter, not the accused, not
    a word of the body.
    """
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, DIR_MODE)
        text = render(reporter, accused, body, filed, rows,
                      accounts=accounts, bans=bans, history=history,
                      window_days=window_days)
        # ⚠ One file per report, named for the filing time so it can be matched
        # against the date `.read_report` showed. A same-second collision gets a
        # counter rather than overwriting: two reports in one second is
        # ordinary, and losing one to the other would be the worst kind of bug
        # here — silent, and only discovered when the record was needed.
        base = _stamp(filed)
        for n in range(1000):
            name = f"report-{base}.txt" if n == 0 else f"report-{base}-{n}.txt"
            path = directory / name
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                             FILE_MODE)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                # The report box is fsynced by `deliver()`; this is the copy
                # that has to outlive it, so it gets the same treatment.
                os.fsync(fh.fileno())
            return path
        log.warning("report capture: no free filename in %s", directory)
    except OSError:
        log.warning("report capture: could not write in %s", directory)
    return None


def prune(directory, days: float, now: float | None = None) -> int:
    """Delete captures older than `days`. Returns how many went.

    ⚠ BY FILE AGE, which is why this is one file per report rather than one
    appended log: an appended file has a single age and can only ever be kept
    whole or deleted whole.

    `days <= 0` keeps everything, matching `event_retention_days`' own
    convention, so an operator who wants to keep the record for ever says so
    with a zero rather than by finding a large number.
    """
    directory = Path(directory)
    if days <= 0 or not directory.is_dir():
        return 0
    now = time.time() if now is None else now
    cutoff = now - days * DAY_SECONDS
    removed = 0
    for p in sorted(directory.glob("report-*.txt")):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:                                     # pragma: no cover
            continue
    return removed
