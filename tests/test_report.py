# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`.report` / `.read_report` — mail, wearing a different label (v1.50.0).

⛔ **The design claim under test is REUSE.** A report is not a new subsystem: it
is a message delivered into a reserved mailbox by the same `MailStore.deliver`
that carries every letter, encrypted with the same key, capped by the same cap,
flagged read by the same flag, aged out by the same purge loop. The only new
things are a box key, its own retention number, and two commands.

So the tests below are mostly about the SEAMS — that the reuse really is reuse
(the covenant guarantees carry over untouched), and about the handful of places
reports deliberately differ from mail:

  * **no delete verb** — a report leaves only by expiring;
  * **its own retention** (`report_retention_days`, 10, not mail's 180);
  * **an immediate live notice** to online readers, where mail waits for a login;
  * **the subject inside the ciphertext**, never a fourth plaintext field.

⚠ Access is ORDINARY PERMISSIONS, not a hardcoded lock. `.report` seeds to the
resident floor and `.read_report` to the OWNER — which keeps reports off staff
eyes, because a report can be *about* a staff account — but an operator moves
either line in `permissions.toml` like any other command, and the tests assert
that too.
"""

import asyncio
import json
import pathlib
import re

import pytest

from heresay import boxfiles
from heresay.commands import REPORT_BOX
from heresay.config import Config
from heresay.mail import MailStore
from heresay.server import TalkerServer

from conftest import seed_account


# ---- what actually reached the disk (v1.52.5: one file per box) ----------

def _mail_dir(tmp_path):
    return boxfiles.box_dir(tmp_path / "mailboxes.json")


def _mail_bytes(tmp_path):
    return b"".join(p.read_bytes() for p in _mail_dir(tmp_path).rglob("*")
                    if p.is_file())


def _mail_keys(tmp_path):
    return sorted(k for k, _ in boxfiles.read_boxes(_mail_dir(tmp_path)))


def _report_box(tmp_path):
    return dict(boxfiles.read_boxes(_mail_dir(tmp_path)))[REPORT_BOX]

REPO = pathlib.Path(__file__).resolve().parent.parent
HOUSE = REPO / "tests" / "fixtures" / "house"
PW = "correct horse battery"
BODY = "CANARYREPORTBODY shouted at me in the cellar"


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
        # A relog must be a LOGIN, not a grace reattach — the notices under test
        # live on the login path, and a reattach deliberately shows none of them.
        disconnect_grace_seconds=0,
    )
    base.update(over)
    return Config(**base)


async def _house(tmp_path, permissions: str = None, **over):
    if permissions:
        (tmp_path / "permissions.toml").write_text(permissions, encoding="utf-8")
    srv = TalkerServer(_cfg(tmp_path, **over))
    await srv.start()
    for name, rank in (("Generic", None), ("Ada", None),
                       ("Bob", None), ("Keeper1", "staff")):
        seed_account(srv.accounts, name, PW)
        if rank:
            srv.accounts.set_rank(name, rank)
    return srv, srv._servers[0].sockets[0].getsockname()[1]


async def _collect(r, t=0.4):
    buf = b""
    while True:
        try:
            chunk = await asyncio.wait_for(r.read(65536), t)
        except asyncio.TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode(errors="replace")


async def _login(port, name):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    await _collect(r, 0.4)
    w.write(name.encode() + b"\n"); await w.drain(); await _collect(r, 0.3)
    w.write(PW.encode() + b"\n"); await w.drain()
    return r, w, await _collect(r, 0.8)


async def _send(w, r, line, t=0.4):
    w.write(line.encode() + b"\n"); await w.drain()
    return await _collect(r, t)


# ---------------------------------------------------------------------------
# 1. Filing — both forms, into the shared box
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_inline_form_files_into_the_report_box(tmp_path):
    srv, port = await _house(tmp_path)
    try:
        r, w, out = await _login(port, "Ada")
        assert "atrium" in out.lower()
        said = await _send(w, r, f".report Bob {BODY}")
        assert "Report filed" in said
        msgs = srv.mail.read(REPORT_BOX)
        assert len(msgs) == 1
        assert msgs[0].sender == "Ada"
        assert BODY in msgs[0].body
        # The named person is the SUBJECT, carried in the body.
        assert "About Bob:" in msgs[0].body
        # ...and nothing landed in Bob's own mailbox: a report is not a letter
        # to the person it is about.
        assert srv.mail.count("Bob") == 0
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_bare_form_opens_THE_SAME_editor_mail_opens(tmp_path):
    """⚠ Reuse, asserted at the seam: `.report <name>` must start the ordinary
    multi-line composer, not a second one that merely looks like it."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, "Ada")
        opened = await _send(w, r, ".report Bob")
        assert "Writing a report about Bob" in opened
        assert "Composing" in opened          # the editor's own header
        await _send(w, r, "first line")
        await _send(w, r, "second line")
        done = await _send(w, r, ".end")
        assert "Report filed" in done
        body = srv.mail.read(REPORT_BOX)[0].body
        assert "first line" in body and "second line" in body
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_self_report_is_refused(tmp_path):
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, "Ada")
        out = await _send(w, r, ".report Ada something")
        assert "can't report yourself" in out
        assert srv.mail.count(REPORT_BOX) == 0
        # An unknown name is refused too — but as "no one called", not as a
        # permission or an action.
        assert "no one called" in await _send(w, r, ".report Nobody hi")
        w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 2. ⛔ The covenant carries over, because the box IS mail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_report_body_reaches_the_CAPTURE_FILE_and_nothing_else(tmp_path):
    """⛔ The whole reason the report box is a mailbox and not a new file — and,
    since v1.58.0, the ONE named exception to it.

    A canary is spoken through `.report` and hunted for in every byte the server
    writes. It may exist in exactly TWO places: inside a Fernet blob, and inside
    the operator's capture file — which is the record, is written once at
    filing, and is deliberately plaintext (see capture.py). ⚠ The sweep is
    UNCHANGED everywhere else, which is what keeps the exception narrow: if the
    body ever turned up in a third file this still fails.
    """
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, "Ada")
        await _send(w, r, f".report Bob {BODY}")
        await asyncio.sleep(0.3)
        # ⚠ rglob, not iterdir (v1.52.5): the mail store keeps one file per box
        # in a subdirectory now, so a flat listing would sweep past the files
        # the canary would actually be in.
        captures = sorted(srv.config.report_capture_path().glob("report-*.txt"))
        assert len(captures) == 1, "exactly one capture, written at filing"
        for path in tmp_path.rglob("*"):
            if not path.is_file() or path in captures:
                continue
            raw = path.read_bytes()
            assert BODY.encode() not in raw, f"report body reached {path.name}"
        # ...it really is in the capture, in the clear, which is the point...
        assert BODY in captures[0].read_text(encoding="utf-8")
        # ...and still in the box, decryptable by the store alone.
        assert BODY in srv.mail.read(REPORT_BOX)[0].body
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_SUBJECT_is_inside_the_ciphertext_not_beside_it(tmp_path):
    """⚠ The mail store keeps three plaintext fields — box key, timestamp,
    read flag. "Who has been reported" is not going to become the fourth: an
    unproven accusation in the clear is exactly the fact this house does not
    accumulate. So the reported name rides in the encrypted body.

    ⚠ Reads the per-box files (v1.52.5); the box key is the FILENAME now, and
    is still plaintext by design — decoded here rather than read out of JSON."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, "Ada")
        await _send(w, r, f".report Bob {BODY}")
        raw = _mail_bytes(tmp_path).decode("utf-8")
        assert "Bob" not in raw
        assert REPORT_BOX in _mail_keys(tmp_path)   # the box key IS plaintext
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_filing_a_report_records_A_BARE_FACT_AND_NOTHING_ELSE(tmp_path):
    """⛔ NOT A NAME, NOT A WORD — but the FACT, since v1.57.0.

    ⚠ This test USED to assert that filing recorded nothing at all, and the
    narrowing is worth stating rather than glossing. `staff_mail` records WHO
    reached for staff; a report has a reporter, an accused and some words, and
    the middle one is the thing a log must not casually collect — so NONE of the
    three is recorded, exactly as before. What is recorded is the one remaining
    fact, which is about nobody: *something is waiting in the box*. It exists so
    an external watcher can nudge the reader without the daemon reaching the
    network. What was DONE about a report is still a staff action and still
    shows in `.staff_actions`; being accused is still not a moderation record.
    """
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, "Ada")
        before = len(srv.events.read())
        await _send(w, r, f".report Bob {BODY}")
        await asyncio.sleep(0.3)
        after = srv.events.read()
        assert len(after) == before + 1, "the bare fact is recorded"
        (filed,) = [e for e in after if e["type"] == "report_filed"]
        # ⛔ THE WHOLE POINT: the row has a timestamp and a type, and there is no
        # third field for anything to hide in.
        assert set(filed) == {"ts", "type"}
        blob = str(after)
        assert "Bob" not in blob and BODY not in blob
        w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 3. Reading — an ordinary permission, not a lock
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_arch_reads_the_box_and_a_keeper_cannot(tmp_path):
    """The seed defaults. ⚠ The staff is refused as an UNKNOWN COMMAND — the
    ordinary invisible-below-rank rule, so the box's existence is not advertised
    to someone who cannot open it."""
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        kr, kw, _ = await _login(port, "Keeper1")
        refused = await _send(kw, kr, ".read_report")
        assert "Unknown command" in refused
        assert BODY not in refused

        gr, gw, _ = await _login(port, "Generic")
        got = await _send(gw, gr, ".read_report")
        assert BODY in got and "About Bob:" in got
        for x in (aw, kw, gw):
            x.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_operator_can_GRANT_the_read_to_staff(tmp_path):
    """⚠ The point of "permission, not lock". The default keeps reports off
    staff eyes because a report may be about a staff — but that is the
    operator's call, and moving one line in permissions.toml makes it so."""
    srv, port = await _house(
        tmp_path, permissions='[commands]\nread_report = "staff"\n')
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        kr, kw, _ = await _login(port, "Keeper1")
        got = await _send(kw, kr, ".read_report")
        assert BODY in got, got[-200:]
        aw.close(); kw.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_report_itself_honours_its_permission_rank(tmp_path):
    """`.report` seeds to the floor; raise it and residents lose it."""
    srv, port = await _house(
        tmp_path, permissions='[commands]\nreport = "owner"\n')
    try:
        r, w, _ = await _login(port, "Ada")
        assert "Unknown command" in await _send(w, r, ".report Bob hello")
        assert srv.mail.count(REPORT_BOX) == 0
        w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 4. Notices — live for the awake, unread-gated at the door
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_ONLINE_reader_is_told_at_once_and_gets_no_body(tmp_path):
    """⚠ The deliberate difference from mail. Somebody has just said something
    is wrong; a reader who is awake should hear so now, not at their next login.
    The body is NOT in the ping — an over-the-shoulder glance learns only that a
    report exists."""
    srv, port = await _house(tmp_path)
    try:
        gr, gw, _ = await _login(port, "Generic")      # arch: may read
        kr, kw, _ = await _login(port, "Keeper1")      # staff: may not
        ar, aw, _ = await _login(port, "Ada")
        await _collect(gr, 0.4); await _collect(kr, 0.4)

        await _send(aw, ar, f".report Bob {BODY}")
        arch_saw = await _collect(gr, 0.8)
        keeper_saw = await _collect(kr, 0.5)

        assert "A new report has been filed" in arch_saw
        assert ".read_report" in arch_saw
        assert BODY not in arch_saw               # ⛔ no body in the ping
        assert "Bob" not in arch_saw              # ...nor who it names
        assert "report" not in keeper_saw.lower()
        for x in (gw, kw, aw):
            x.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_login_notice_is_UNREAD_gated_and_clears_on_reading(tmp_path):
    """⛔ Unread, never merely non-empty. There is no delete verb, so a
    non-empty gate would nag until expiry — and would teach the reader to want
    an empty box, which is the one instinct a report box must not encourage."""
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        aw.close()
        await asyncio.sleep(0.3)

        gr, gw, first = await _login(port, "Generic")
        assert "You have 1 unread report." in first
        await _send(gw, gr, ".read_report")
        gw.close()
        await asyncio.sleep(0.3)

        gr2, gw2, second = await _login(port, "Generic")
        assert "unread report" not in second
        # ...and the report is still in the box. Read is not gone.
        assert srv.mail.count(REPORT_BOX) == 1
        gw2.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_login_notice_pluralises_and_only_reaches_a_reader(tmp_path):
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, ".report Bob one")
        await _send(aw, ar, ".report Bob two")
        aw.close()
        await asyncio.sleep(0.3)

        kr, kw, keeper_login = await _login(port, "Keeper1")
        assert "report" not in keeper_login.lower()
        kw.close()

        gr, gw, arch_login = await _login(port, "Generic")
        assert "You have 2 unread reports." in arch_login
        gw.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 5. ⛔ No delete. Expiry is the only way out.
# ---------------------------------------------------------------------------

def test_there_is_NO_report_delete_command_anywhere():
    """⛔ The deliberate difference from mail, asserted against the registry
    rather than against a list of guesses: no command may both name reports and
    delete. A box its reader can quietly empty is not accountability — and the
    reader is often the person whose handling a report might concern."""
    from heresay.commands.registry import COMMANDS
    names = {c.name for c in COMMANDS}
    for forbidden in ("dreport", "delreport", "report_delete", "dread_report",
                      "purge_report", "reportpurge"):
        assert forbidden not in names
    # And nothing in the report handlers calls the store's delete paths.
    import inspect
    from heresay.commands import staff as S
    for fn in (S.cmd_report, S.cmd_read_report, S._deliver_report):
        src = inspect.getsource(fn)
        assert ".delete(" not in src and "purge_mailbox" not in src


@pytest.mark.asyncio
async def test_mailpurge_cannot_reach_the_report_box(tmp_path):
    """⚠ The one existing command that destroys a whole mailbox. It resolves an
    ACCOUNT, and `#report` is not one — the leading '#' cannot start a name. So
    the box is out of its reach by construction, exactly as `#keeper` is."""
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        gr, gw, _ = await _login(port, "Generic")
        out = await _send(gw, gr, f".mailpurge {REPORT_BOX}")
        assert "no account called" in out
        assert srv.mail.count(REPORT_BOX) == 1
        aw.close(); gw.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 6. Retention — reports age on their own number
# ---------------------------------------------------------------------------

def test_reports_expire_on_report_retention_days_not_mail_s_180(tmp_path):
    """⚠ Two retentions, one store, one purge loop. Reports get their own value
    because expiry is their ONLY removal path — this is a report's whole
    lifetime, not a backstop under a reader who never tidied."""
    now = [1_000_000.0]
    store = MailStore(tmp_path / "m.json", tmp_path / "m.key",
                      purge_days=180, wall_clock=lambda: now[0],
                      box_purge_days={REPORT_BOX: 100})
    store.deliver("ada", "Bob", "a letter")
    store.deliver(REPORT_BOX, "Ada", "About Bob:\n\na report")
    assert store.purge_days_for("ada") == 180
    assert store.purge_days_for(REPORT_BOX) == 100

    day = 86400
    now[0] += 120 * day                     # past 100, short of 180
    assert store.purge() == 1               # the REPORT went
    assert store.count(REPORT_BOX) == 0
    assert store.count("ada") == 1         # the letter stayed

    now[0] += 100 * day                     # now past 180 as well
    assert store.purge() == 1
    assert store.count("ada") == 0


def test_the_default_report_retention_is_TEN_and_distinct_from_mail(tmp_path):
    """⛔ 100 → 10 in v1.58.0, and the capture file is the whole reason.

    ⚠ It was 100 when the box WAS the record. It is not any more: a report is
    written to the operator's disk in full at the instant of filing, so the copy
    in the box is the working copy `.read_report` opens. Ten matches
    `event_retention_days` — the honest window for anything the engine holds
    about people — and the house goes back to keeping nothing durable."""
    cfg = Config()
    assert cfg.report_retention_days == 10
    assert cfg.report_retention_days == cfg.event_retention_days
    assert cfg.mail_purge_days == 180


@pytest.mark.asyncio
async def test_the_server_wires_the_report_retention_into_the_store(tmp_path):
    """A config value nobody passes is a comment."""
    srv, port = await _house(tmp_path, report_retention_days=42)
    try:
        assert srv.mail.purge_days_for(REPORT_BOX) == 42
        assert srv.mail.purge_days_for("ada") == srv.config.mail_purge_days
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 7. `.source` — the AGPL §13 offer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_source_prints_the_url_and_the_running_version(tmp_path):
    """⚠ The version rides beside the URL because "the source" means the source
    of what is actually RUNNING — a bare URL points at whatever the branch is
    today, which may not be this house."""
    import heresay
    srv, port = await _house(tmp_path,
                             source_url="https://example.invalid/heresay.git")
    try:
        r, w, _ = await _login(port, "Ada")       # a RESIDENT: floor rank
        out = await _send(w, r, ".source")
        assert "https://example.invalid/heresay.git" in out
        assert heresay.__version__ in out
        w.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_source_says_so_plainly_when_it_is_not_configured(tmp_path):
    """⚠ It does NOT fall back to naming the upstream project: a modified
    instance pointing at unmodified upstream would be a wrong answer wearing the
    shape of a right one."""
    srv, port = await _house(tmp_path)
    try:
        r, w, _ = await _login(port, "Ada")
        out = await _send(w, r, ".source")
        assert "not configured" in out
        assert "http" not in out.lower()
        w.close()
    finally:
        await srv.aclose()


# ---------------------------------------------------------------------------
# 8. ⛔ ATTRIBUTION — a report the reader cannot attribute is an anonymous
#    accusation, which defeats the point (v1.50.1)
# ---------------------------------------------------------------------------
#
# ⚠ Nothing had to be ADDED for this: a report is mail, and mail has always
# carried its sender inside the encrypted blob. That is the reuse paying out —
# `deliver(REPORT_BOX, session.resident.name, …)` puts the filer in the same
# ciphertext as the subject, and `.read_report` renders it through `.mail`'s own
# header. But "true because of how it was built" is exactly the kind of property
# that a later refactor removes in silence, so it is pinned here.

@pytest.mark.asyncio
async def test_the_reader_sees_WHO_filed_ABOUT_WHOM_WHEN_and_WHAT(tmp_path):
    """All four, in one screen. The reader has to be able to weigh a report:
    who is making the claim, against whom, when, and in what words."""
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        gr, gw, _ = await _login(port, "Generic")
        view = await _send(gw, gr, ".read_report", 0.8)

        assert "from Ada" in view          # WHO filed
        assert "About Bob:" in view          # ABOUT WHOM
        assert re.search(r"\d{4}-\d{2}-\d{2}", view), view   # WHEN
        assert BODY in view                  # WHAT
        aw.close(); gw.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_reporter_is_the_FILING_ACCOUNT_not_a_placeholder(tmp_path):
    """⚠ The attribution is only worth anything if it names the person who
    actually typed it. Pinned at the store, so replacing the sender with a
    constant — "anonymous", the box name, the reader — would have to be a
    deliberate edit that fails a test rather than a quiet one."""
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        msg = srv.mail.read(REPORT_BOX)[0]
        assert msg.sender == "Ada"
        assert msg.sender != REPORT_BOX and msg.sender.lower() != "anonymous"
        aw.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_BOTH_names_live_ONLY_in_the_ciphertext(tmp_path):
    """⛔ The covenant half of attribution, and the reason it is safe to record.

    The reader learns who filed it and about whom — but only by DECRYPTING. On
    disk, `mailboxes.json` keeps exactly three plaintext fields, and neither
    name is among them. So an accusation and its author exist in the store, and
    nowhere that reading the file can reach.
    """
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        await asyncio.sleep(0.3)

        raw = _mail_bytes(tmp_path).decode("utf-8")
        for name in ("Ada", "Bob"):
            assert name not in raw, f"{name} reached the mail store in the clear"
        # ...and the record really is only those three fields, so nobody has
        # quietly added a fourth that a future name could sit in.
        rec = _report_box(tmp_path)["messages"][0]
        assert set(rec) == {"ts", "read", "blob"}
        assert isinstance(rec["ts"], float) and isinstance(rec["read"], bool)

        # Every OTHER file the server writes, too. ⚠ rglob, not iterdir
        # (v1.52.5): the stores keep one file per box in a SUBDIRECTORY now, and
        # a flat listing would have walked straight past the very files this
        # sweep exists to check.
        # ⚠ v1.58.0 exempts the CAPTURE FILE by path, not by name-matching: it
        # is the operator's record and holds both names and the body in the
        # clear, deliberately. Everything else is swept exactly as before.
        captures = set(srv.config.report_capture_path().glob("report-*.txt"))
        for path in tmp_path.rglob("*"):
            if not path.is_file() or path.name == "accounts.json":
                continue        # accounts.json legitimately holds every name
            if path in captures:
                continue        # THE record — see capture.py
            text = path.read_bytes()
            assert BODY.encode() not in text, path.name

        # And the pair is genuinely recoverable by the store alone.
        msg = srv.mail.read(REPORT_BOX)[0]
        assert msg.sender == "Ada" and "About Bob:" in msg.body
        aw.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_the_filed_row_names_NOBODY_and_the_reporter_is_there_only_as_a_LOGIN(
        tmp_path):
    """⚠ The honest form of "filing records no name".

    The reporter's name IS in `events.jsonl` — as her own `login`, the
    connection record clause 3 of the agreement discloses. That is not the
    report. What matters is that the row FILING adds names nobody, and that no
    row of any kind ties her to the person she named. ⚠ v1.57.0 added the row;
    it did not add a name to it.
    """
    srv, port = await _house(tmp_path)
    try:
        ar, aw, _ = await _login(port, "Ada")
        before = list(srv.events.read())
        await _send(aw, ar, f".report Bob {BODY}")
        await asyncio.sleep(0.4)
        after = list(srv.events.read())

        assert len(after) == len(before) + 1, "filing adds the bare fact"
        (filed,) = [e for e in after if e["type"] == "report_filed"]
        assert set(filed) == {"ts", "type"}, "no field to hold a name"
        # The accused never appears at all...
        blob = json.dumps(after)
        assert "Bob" not in blob
        assert BODY not in blob
        # ...and the reporter appears ONLY through the login type.
        for e in after:
            if "Ada" in json.dumps(e):
                assert e["type"] == "login", e
        aw.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_neither_the_ping_nor_the_login_notice_names_anybody(tmp_path):
    """⛔ Attribution is for the READER, at the moment they open it — not for
    anyone glancing at their screen. The two notices say a report exists and
    nothing else, so learning who accused whom requires the deliberate act of
    running `.read_report`."""
    srv, port = await _house(tmp_path)
    try:
        gr, gw, _ = await _login(port, "Generic")
        ar, aw, _ = await _login(port, "Ada")
        await _collect(gr, 0.4)

        await _send(aw, ar, f".report Bob {BODY}")
        ping = await _collect(gr, 0.8)
        assert "A new report has been filed" in ping
        for leak in ("Ada", "Bob", BODY):
            assert leak not in ping, f"the live ping leaked {leak!r}"
        gw.close()
        await asyncio.sleep(0.3)

        gr2, gw2, login_screen = await _login(port, "Generic")
        notice = "\n".join(l for l in login_screen.splitlines()
                           if "report" in l.lower())
        assert "You have 1 unread report." in notice
        for leak in ("Ada", "Bob", BODY):
            assert leak not in notice, f"the login notice leaked {leak!r}"
        aw.close(); gw2.close()
    finally:
        await srv.aclose()


@pytest.mark.asyncio
async def test_both_names_survive_the_40_column_view(tmp_path):
    """⚠ A phone must not lose the attribution. The narrow renderer splits the
    header (sender on one line, date indented beneath), so this checks the
    reader can still tell who filed and about whom — and that nothing overruns
    the frame while doing it."""
    import websockets
    srv, port = await _house(tmp_path)
    ws_port = next(s.getsockname()[1] for s in srv._ws_server.sockets)
    try:
        ar, aw, _ = await _login(port, "Ada")
        await _send(aw, ar, f".report Bob {BODY}")
        aw.close()
        await asyncio.sleep(0.3)

        ws = await websockets.connect(f"ws://127.0.0.1:{ws_port}")
        frames = []

        async def pump():
            try:
                async for f in ws:
                    frames.append(f)
            except Exception:
                pass
        t = asyncio.create_task(pump())
        await ws.send("\x01width 40"); await ws.send("\x01resume")
        await asyncio.sleep(0.4)
        await ws.send("Generic"); await asyncio.sleep(0.5)
        await ws.send(PW); await asyncio.sleep(1.2)
        frames.clear()
        await ws.send(".read_report"); await asyncio.sleep(1.0)

        view = "\n".join(frames)
        assert "Ada" in view, view
        assert "About Bob:" in view, view
        assert re.search(r"\d{4}-\d{2}-\d{2}", view), view
        assert max((len(f) for f in frames), default=0) <= 40, frames
        t.cancel()
        await ws.close()
    finally:
        await srv.aclose()


def test_the_help_page_does_not_promise_anonymity():
    """⛔ Found by this verification pass, and the one thing that had to change.

    The page said "Nobody is told you filed it." A resident could read that as
    "my report is anonymous" — and be wrong about the single fact most likely to
    decide whether they file at all. The reader sees their name. The page says
    so now, and says why.
    """
    page = (REPO / "tests" / "fixtures" / "house" / "help" / "report.txt").read_text(encoding="utf-8")
    assert "Nobody is told you filed it." not in page
    assert "signed" in page
    assert "sees your name" in page
    # ...while still being clear about who is NOT told.
    assert "Nobody ELSE is told" in page
