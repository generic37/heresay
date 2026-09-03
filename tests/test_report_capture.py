# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The report capture file — the record, on the operator's disk (v1.58.0).

⛔ **The inversion under test.** A report used to OUTLIVE ITS OWN EVIDENCE: the
box held it 100 days while the context aged out of `events.jsonl` at ten. The
fix captures everything to disk at the instant of filing and then lets the
in-system copy expire like everything else — so the engine goes back to holding
nothing durable about a report, and the operator's disk holds the record.

The tests below are about the four things that makes true:

  * the capture is written **at filing**, self-contained, 0600;
  * ⛔ **nothing in the engine reads it back** — a source sweep, because this is
    the guarantee a future command could quietly take away;
  * a **stale** report (nothing in the window) still writes a valid file;
  * it is **pruned by file age** on its own much longer clock.

⚠ Everything asserted about CONTENT is seeded through the REAL `.report`
command against a REAL server with distinctive sentinels, never a hand-built
fixture: the question is whether the daemon captures what it knows, and only
the real path can answer that.
"""

import asyncio
import inspect
import pathlib
import re
import time

import pytest

from heresay import capture
from heresay.commands import REPORT_BOX
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import seed_account

REPO = pathlib.Path(__file__).resolve().parent.parent
HOUSE = REPO / "tests" / "fixtures" / "house"
PW = "correct horse battery"

# Distinctive on purpose: these are grepped out of a whole file tree, so a
# substring that could occur naturally would make a pass meaningless.
SAID = "SENTINELBODY-parapet-transept-quartz"
REPORTER = "Marram"
ACCUSED = "Thistledown"


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
        wrap_columns=100, reload_poll_seconds=0,
        max_conns_per_ip=40, churn_max_in_window=200,
        disconnect_grace_seconds=0,
    )
    base.update(over)
    return Config(**base)


async def _house(tmp_path, **over):
    srv = TalkerServer(_cfg(tmp_path, **over))
    await srv.start()
    for name in ("Generic", REPORTER, ACCUSED):
        seed_account(srv.accounts, name, PW)
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def _collect(r, t=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(65536), t)
        except asyncio.TimeoutError:
            return buf.decode("utf-8", "replace")
        if not chunk:
            return buf.decode("utf-8", "replace")
        buf += chunk


async def _login(port, name):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r)
    w.write((name + "\n").encode()); await w.drain(); await _collect(r)
    w.write((PW + "\n").encode()); await w.drain()
    return r, w, await _collect(r)


async def _send(w, r, line, t=0.4):
    w.write((line + "\n").encode()); await w.drain()
    return await _collect(r, t)


def _captures(srv):
    return sorted(srv.config.report_capture_path().glob("report-*.txt"))


# ---------------------------------------------------------------------------
# 1. It is written, at filing, and it is self-contained
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_filing_writes_ONE_capture_holding_both_names_and_the_full_text(
        tmp_path):
    """⛔ THE HEART OF IT. Filed through the real command; the file must stand
    alone once the box copy is gone, so everything the reader would need is in
    it: who filed, who it is about, the whole text, and the filing time."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, REPORTER)
        await _send(w, r, f".report {ACCUSED} {SAID}")
        await asyncio.sleep(0.3)

        files = _captures(srv)
        assert len(files) == 1, "exactly one file per report"
        text = files[0].read_text(encoding="utf-8")

        assert REPORTER in text, "the reporter is named"
        assert ACCUSED in text, "the accused is named"
        assert SAID in text, "the full text, in the clear"
        # The filing time, in the file AND in its name, so a capture can be
        # matched against the date `.read_report` showed.
        assert re.search(r"Filed\s+\d{4}-\d{2}-\d{2}T", text)
        assert re.fullmatch(r"report-\d{8}-\d{6}(-\d+)?\.txt", files[0].name)
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_capture_is_0600_in_a_0700_directory(tmp_path):
    """⚠ NOT root-owned, and the module says why: the unit runs as
    `User=heresay` with an empty `CapabilityBoundingSet`, so the daemon cannot
    chown to root and must not try. What it CAN guarantee is the mode and the
    directory — and `StateDirectoryMode=0700` does the rest."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, REPORTER)
        await _send(w, r, f".report {ACCUSED} {SAID}")
        await asyncio.sleep(0.3)
        path = _captures(srv)[0]
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_capture_carries_the_ACCOUNT_RECORDS_of_both(tmp_path):
    """Created date, rank, ban standing — the file has to answer "who are these
    two?" after the accounts themselves may have changed or gone."""
    srv, port = await _house(tmp_path)
    try:
        srv.accounts.set_rank(ACCUSED, "staff")
        r, w, _ = await _login(port, REPORTER)
        await _send(w, r, f".report {ACCUSED} {SAID}")
        await asyncio.sleep(0.3)
        text = _captures(srv)[0].read_text(encoding="utf-8")

        assert f"ACCOUNT: {REPORTER}" in text and "the reporter" in text
        assert f"ACCOUNT: {ACCUSED}" in text and "reported" in text
        assert "staff" in text, "the accused's rank is captured"
        assert "created" in text and "active bans" in text and "ban history" in text
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_capture_carries_the_EVENT_ROWS_for_both_accounts(tmp_path):
    """⛔ The half that decays. Both accounts have logged in, so the log holds a
    `login` for each — with the address — and those rows are what vanish at ten
    days while the report used to sit for a hundred. They are dumped WHOLE."""
    srv, port = await _house(tmp_path)
    try:
        # Both log in, so both have a login row with an address.
        br, bw, _ = await _login(port, ACCUSED)
        r, w, _ = await _login(port, REPORTER)
        await _send(w, r, f".report {ACCUSED} {SAID}")
        await asyncio.sleep(0.3)
        text = _captures(srv)[0].read_text(encoding="utf-8")

        assert "EVENT LOG" in text
        rows = [l for l in text.split("\n") if '"account"' in l or '"ip"' in l]
        assert rows, "the log rows are in the file"
        blob = "\n".join(rows)
        assert f'"account": "{REPORTER}"' in blob
        assert f'"account": "{ACCUSED}"' in blob
        # ⚠ The ADDRESS is captured too — a `connection` row carries no name, so
        # a name-only sweep would drop the entire connection history and leave
        # the capture unable to answer "was he even here that day?".
        assert '"ip": "127.0.0.1"' in blob
        assert '"type": "connection"' in blob or "connection " in text
        bw.close(); w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_a_STALE_report_still_writes_a_valid_file_saying_so(tmp_path):
    """⚠ AN EMPTY WINDOW IS A CORRECT RESULT, not a failure. A report about
    something weeks old genuinely has no context left; saying so is the honest
    capture, and refusing to write would lose the report TEXT as well — the part
    that cannot be recovered from anywhere."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, REPORTER)
        # Empty the log AFTER login, so the sweep finds nothing about either.
        srv.events.path.write_text("", encoding="utf-8")
        await _send(w, r, f".report {ACCUSED} {SAID}")
        await asyncio.sleep(0.3)

        text = _captures(srv)[0].read_text(encoding="utf-8")
        assert SAID in text, "the report itself is still captured"
        assert REPORTER in text and ACCUSED in text
        assert "nothing" in text.lower() and "stale" in text.lower()
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_TWO_reports_in_one_second_are_TWO_files(tmp_path):
    """⚠ The filename carries the filing SECOND, so a collision is ordinary.
    Losing one report to another would be the worst kind of bug here — silent,
    and only discovered when the record was needed."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, REPORTER)
        await _send(w, r, f".report {ACCUSED} {SAID} one")
        await _send(w, r, f".report {ACCUSED} {SAID} two")
        await asyncio.sleep(0.3)
        files = _captures(srv)
        assert len(files) == 2, [f.name for f in files]
        both = "".join(f.read_text(encoding="utf-8") for f in files)
        assert f"{SAID} one" in both and f"{SAID} two" in both
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_ORDINARY_TALK_writes_no_capture_at_all(tmp_path):
    """⛔ WHAT KEEPS THE EXCEPTION NARROW. The capture is the one place the
    engine writes content to disk in the clear, and the only thing that reaches
    it is a filed report. Speech, emotes, thinks, topics and tells must leave
    the directory empty — if this ever needs relaxing, the exception has stopped
    being narrow."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, REPORTER)
        for line in (f"{SAID} aloud", f".e emotes {SAID}", f".think {SAID}",
                     f".topic {SAID}", f".shout {SAID}",
                     f".mail {ACCUSED} {SAID}", f".mail_staff {SAID}",
                     f".call_staff {SAID}", f".desc {SAID}"):
            await _send(w, r, line, t=0.15)
        await asyncio.sleep(0.3)
        assert _captures(srv) == [], "only a REPORT writes a capture"
        w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 2. ⛔ Nothing in the engine reads it back
# ---------------------------------------------------------------------------

def test_NOTHING_IN_THE_ENGINE_READS_THE_CAPTURE_DIRECTORY():
    """⛔⛔ THE GUARANTEE A FUTURE COMMAND COULD QUIETLY TAKE AWAY.

    The capture is write-only from the engine's side: no command, no rank, no
    `.security` row, no `.load_report` line, no Phase-E hook. It is read with
    `less` over ssh by somebody who is on the box.

    ⚠ This is a SOURCE sweep rather than a behavioural test, because the thing
    being guarded is the ABSENCE of a code path — and you cannot drive an
    absence. Every module in the package is read: the only permitted mentions of
    `report_capture_path` are the definition in `config.py`, the write in
    `commands/staff.py`, and the prune in `server.py`, and `capture.py` itself
    may contain no read verb at all beyond the prune's `stat`/`unlink`.
    """
    pkg = pathlib.Path(inspect.getfile(capture)).parent
    allowed = {"config.py", "staff.py", "server.py", "capture.py"}
    for path in sorted(pkg.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "report_capture_path" in src:
            assert path.name in allowed, (
                f"{path.name} reaches for the capture directory")

    # ⛔ And no READ verb anywhere near it, in any of them.
    for path in sorted(pkg.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for line in src.split("\n"):
            if "report_capture_path" not in line:
                continue
            for verb in ("read_text", "read_bytes", "open(", "glob",
                         "iterdir", "rglob"):
                assert verb not in line, (path.name, line.strip())

    # The module itself: it writes and it prunes. `render` is pure, `prune`
    # stats and unlinks. Nothing hands a capture's CONTENTS back to a caller.
    cap_src = pathlib.Path(inspect.getfile(capture)).read_text(encoding="utf-8")
    body = "\n".join(l for l in cap_src.split("\n")
                     if not l.strip().startswith("#"))
    for verb in ("read_text(", "read_bytes(", ".readlines(", "open(path)"):
        assert verb not in body, verb


def test_no_COMMAND_mentions_the_capture_at_all():
    """A second, blunter sweep: the command layer may WRITE one (that is
    `_capture_report`) and must never name the directory anywhere else — no
    `.captures` verb, no listing, no count on a screen."""
    pkg = pathlib.Path(inspect.getfile(capture)).parent / "commands"
    for path in sorted(pkg.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if path.name == "staff.py":
            # ⚠ EXACTLY TWO mentions, and both are named: the `hasattr` guard
            # that lets a stripped-down harness file a report without a capture,
            # and the write itself. A third would be a read.
            lines = [l.strip() for l in src.split("\n")
                     if "report_capture_path" in l]
            assert len(lines) == 2, lines
            assert any(l.startswith("if cfg is None or not hasattr") for l in lines)
            assert any(l.startswith("cfg.report_capture_path()") for l in lines)
            continue
        assert "report_capture_path" not in src, path.name
        assert "capture." not in src.replace("capture.py", ""), path.name


# ---------------------------------------------------------------------------
# 3. Pruning — its own, much longer clock
# ---------------------------------------------------------------------------

def test_prune_removes_an_OLD_capture_and_keeps_a_RECENT_one(tmp_path):
    """⚠ BY FILE AGE, which is why this is one file per report rather than one
    appended log: an appended file has a single age and can only be kept whole
    or deleted whole."""
    d = tmp_path / "reports"
    d.mkdir()
    now = time.time()
    old = d / "report-20240101-000000.txt"
    new = d / "report-20260101-000000.txt"
    for p in (old, new):
        p.write_text("x", encoding="utf-8")
    import os
    os.utime(old, (now - 400 * 86400, now - 400 * 86400))
    os.utime(new, (now - 10 * 86400, now - 10 * 86400))

    assert capture.prune(d, 375, now=now) == 1
    assert not old.exists() and new.exists()


def test_prune_with_ZERO_keeps_everything(tmp_path):
    """`0` keeps everything, matching `event_retention_days`' own convention —
    so an operator who wants the record for ever says so with a zero rather than
    by finding a large number."""
    d = tmp_path / "reports"
    d.mkdir()
    p = d / "report-20200101-000000.txt"
    p.write_text("x", encoding="utf-8")
    import os
    os.utime(p, (0, 0))
    assert capture.prune(d, 0) == 0
    assert p.exists()


def test_the_default_capture_life_is_375_days():
    """A year of coverage counted from the EVENT rather than the filing, since
    the capture already reaches ten days back."""
    assert Config().report_capture_days == 375


@pytest.mark.asyncio
async def test_the_server_PRUNES_captures_at_startup(tmp_path):
    """A retention nobody applies is a comment."""
    d = tmp_path / "reports"
    d.mkdir()
    stale = d / "report-20200101-000000.txt"
    stale.write_text("x", encoding="utf-8")
    import os
    os.utime(stale, (0, 0))
    srv, _port = await _house(tmp_path, report_capture_days=375)
    try:
        assert not stale.exists(), "startup prunes the capture directory"
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 4. The box's own cap, separate from mail's
# ---------------------------------------------------------------------------

def test_the_report_box_cap_defaults_to_1000_and_mailbox_cap_is_untouched():
    """⚠ Reports inheriting `mailbox_cap`'s 50 was an ACCIDENT of sharing the
    store. A mailbox cap bounds how much one person can be SENT; the report box
    is the whole house's single channel for saying something is wrong, and fifty
    junk reports could wedge it shut."""
    cfg = Config()
    assert cfg.report_box_cap == 1000
    assert cfg.mailbox_cap == 50


@pytest.mark.asyncio
async def test_the_report_box_takes_more_than_fifty_and_mail_still_does_not(
        tmp_path):
    """⛔ The two caps are genuinely independent, driven through the store."""
    srv, _port = await _house(tmp_path, report_box_cap=60)
    try:
        assert srv.mail.cap_for(REPORT_BOX) == 60
        assert srv.mail.cap_for("marram") == srv.config.mailbox_cap == 50
        for i in range(60):
            assert srv.mail.deliver(REPORT_BOX, REPORTER, f"r{i}"), i
        # 61st is refused — its OWN cap, not mail's.
        assert not srv.mail.deliver(REPORT_BOX, REPORTER, "over")
        # ...while an ordinary mailbox still stops at 50.
        for i in range(50):
            assert srv.mail.deliver("marram", ACCUSED, f"m{i}"), i
        assert not srv.mail.deliver("marram", ACCUSED, "over")
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_report_box_expires_on_the_TEN_day_default(tmp_path):
    """The box copy is the working copy now; the capture is the record."""
    srv, _port = await _house(tmp_path)
    try:
        assert srv.mail.purge_days_for(REPORT_BOX) == 10
        assert srv.mail.purge_days_for("marram") == srv.config.mail_purge_days
        srv.mail.deliver(REPORT_BOX, REPORTER, "old",
                         timestamp=time.time() - 11 * 86400)
        srv.mail.deliver(REPORT_BOX, REPORTER, "new")
        assert srv.mail.purge() == 1
        assert len(srv.mail.read(REPORT_BOX)) == 1
    finally:
        await srv.aclose()
