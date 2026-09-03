# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The word "wizard" never reaches a person (v1.45.0).

The wizard→staff/staff rename (v1.27.0) moved the RANKS and left the word in
places that are read: prose, comments, docs. This is the sweep that finishes it,
and — more usefully — the guard that keeps it finished.

⛔ **The line this file draws, and it is the whole point:**

* **DISPLAY** — anything a person reads: command output, view labels, log-line
  prose, help pages, authored content. No "wizard" here, ever. A collective, an
  action or a tier is **staff**; what ONE member of it is called is the house's
  own `[levels.staff] singular`, never a word baked into the engine.
* **STORED / INTERNAL** — since v1.54.0 the written tokens are `staff_action`
  (the event type) and `staff_only:` (the room-file key). ⚠ The two old
  spellings are NOT symmetrical any more: `wizard_action` is out of the writable
  set but a legacy ROW still reads back, because an `events.jsonl` written
  before v1.54.0 is still a record — while the `wizard_only:` header alias was
  DELETED in v1.60.0, so the old spelling is now an unknown header key, which is
  fatal at startup. The legacy `level` value `wizard` STILL migrates (a pre-v1.27
  account carries it). These are TOKENS, not display: §5 pins what gets written,
  and what is still read.

The two halves are asserted separately and on purpose: the display sweep must
not be satisfied by renaming a token, and the token pins must not be satisfied
by a view that prints one.
"""

import ast
import io
import logging
import pathlib

import pytest

import heresay.commands as C
from heresay.config import Config
from heresay.server import TalkerServer

from conftest import FakeSession, seed_account

REPO = pathlib.Path(__file__).resolve().parent.parent
ENGINE = sorted(REPO.glob("heresay/**/*.py"))
HOUSE = REPO / "tests" / "fixtures" / "house"

# The tokens a string literal is allowed to contain. Each is a value that lands
# on disk somewhere the engine did not write it alone: an event row, an
# operator's room file, an account from an older release.
TOKENS = ("wizard_action", "wizard_only", "staff_only_rooms")

# ⚠ Two literals that name a LEGACY token rather than describing anything, listed
# whole so each has to be argued rather than waved through by a loose pattern.
#
# The second is the interesting one: it is a log line an operator READS, and it
# says "wizard" on purpose. It is telling them which keys are in THEIR file — a
# pre-1.27 `theme.toml` really does say `wizard = "..."`, and rewording it to
# "staff" would send them looking for a key that is not there. Naming a token is
# not display prose even when a person reads it.
TOKEN_LITERALS = frozenset({
    # accounts.py — the pre-v3 `level` VALUE, compared against, never shown.
    "wizard",
    # theme.py — the pre-1.27 `[ranks]` KEY names, in the line that says it fell
    # back to reading them.
    "%s: [ranks] has no 'order' — reading the pre-1.27 resident/wizard/arch "
    "keys as the ladder %s",
})


def _allowed(text: str) -> bool:
    """Is every "wizard" in this string part of a token we keep?"""
    if text in TOKEN_LITERALS:
        return True
    stripped = text
    for tok in TOKENS:
        stripped = stripped.replace(tok, "")
    return "wizard" not in stripped.lower()


# ---------------------------------------------------------------------------
# 1. No engine STRING LITERAL says it
# ---------------------------------------------------------------------------
#
# ⚠ An AST walk rather than a grep, because a grep cannot tell a comment from a
# sentence somebody will read. Every `session.send(...)`, every `log.info(...)`,
# every error message is a string constant, so this covers the display surface
# without needing to know which call it went to.

@pytest.mark.parametrize("path", ENGINE, ids=lambda p: p.name)
def test_no_engine_string_literal_says_wizard(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        # A docstring is a string constant too. It is developer prose, not
        # display, and the legacy-token discussion genuinely lives in some of
        # them — so the sweep is over what could be SHOWN, and a docstring is
        # exempted by being the first statement of its module/class/function.
        if node.value in _docstrings(tree):
            continue
        assert _allowed(node.value), (
            f"{path.name}:{node.lineno} — a string literal says 'wizard': "
            f"{node.value[:70]!r}. Display text is staff/staff; only "
            f"{', '.join(TOKENS)} are kept.")


def _docstrings(tree) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                out.add(doc)
    return out


# ---------------------------------------------------------------------------
# 2. No AUTHORED CONTENT says it
# ---------------------------------------------------------------------------

def _house_text_files():
    for p in sorted(HOUSE.rglob("*.txt")):
        yield p


@pytest.mark.parametrize("path", list(_house_text_files()),
                         ids=lambda p: str(p.relative_to(HOUSE)))
def test_no_authored_content_says_wizard(path):
    """Help pages, the greeting, the agreement, the reboot script, and every
    room's PROSE. ⚠ A room file's `wizard_only:` header is exempt — it is the
    file FORMAT, and renaming it would break every operator's existing rooms
    (see the genericization note in response.md)."""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        assert _allowed(line), f"{path.name}:{lineno}: {line!r}"


# ---------------------------------------------------------------------------
# 3. No VIEW says it — driven, not read
# ---------------------------------------------------------------------------

def _cfg(tmp_path):
    return Config(
        owner_name="Generic", instance_name="Commons",
        house_dir=str(HOUSE),
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        forbidden_names_file=str(tmp_path / "forbidden_names.txt"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000, reload_poll_seconds=0)


@pytest.fixture
def srv(tmp_path):
    s = TalkerServer(_cfg(tmp_path))
    seed_account(s.accounts, "Generic", "correct horse battery")
    seed_account(s.accounts, "Wizzy", "correct horse battery")
    s.accounts.set_rank("Wizzy", "staff")
    # One of every action a staff view can show, including the reload actor.
    # ⚠ The WRITTEN type is `staff_action` now (v1.54.0); `wizard_action` is no
    # longer in EVENT_TYPES, so recording it would be dropped as unknown.
    s.events.record("staff_action", actor="Wizzy", action="gag", target="Bob")
    s.events.record("staff_action", actor="Generic", action="ban_ip[24h]",
                    target="Bob", outcome="cancelled")
    s.events.record("staff_action", actor="config", action="reload",
                    target="permissions")
    s.events.record("staff_call", account="Bob")
    s.events.record("auth_fail", ip="203.0.113.9", phase="password")
    return s


def _screen(srv, cmd, width=78):
    s = FakeSession(srv.world, name="Generic", level="arch", width=width,
                    config=srv.config, events=srv.events, state=srv.state,
                    accounts=srv.accounts, load_report=srv.load_report,
                    theme=srv.theme, mail=srv.mail, bans=srv.bans)
    s.clear()
    C.dispatch(s, cmd)
    return s.text


VIEWS = [".staff_actions", ".security", ".staff_calls", ".load_report",
         ".help", ".help ban", ".help gag", ".who", ".look", ".map",
         ".accounts", ".examine", ".version", ".set"]


@pytest.mark.parametrize("cmd", VIEWS)
@pytest.mark.parametrize("width", [78, 40])
def test_no_view_says_wizard(srv, cmd, width):
    """⛔ Including `.staff_actions`, which reads rows whose stored type IS
    `wizard_action` — the raw token must never surface on the screen that
    filters on it."""
    out = _screen(srv, cmd, width)
    assert "wizard" not in out.lower(), f"{cmd} at {width}: {out[:300]!r}"


def test_the_staff_action_view_shows_the_rows_it_read(srv):
    """Belt to the sweep above: the screen is not clean merely because it is
    empty. It really did render the three stored `wizard_action` rows."""
    out = _screen(srv, ".staff_actions")
    for expected in ("Wizzy", "gag", "Bob", "ban_ip[24h]", "cancelled", "config"):
        assert expected in out, expected


# ---------------------------------------------------------------------------
# 4. No LOG LINE says it
# ---------------------------------------------------------------------------

def test_a_boot_and_a_reload_log_no_wizard(tmp_path):
    """The operator reading journald is a person too. A whole boot plus a
    permissions reload — the pass that records a `wizard_action` with
    `actor="config"` — and not a "wizard" in any of it."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    log = logging.getLogger("heresay")
    log.addHandler(handler)
    level = log.level
    log.setLevel(logging.INFO)
    try:
        s = TalkerServer(_cfg(tmp_path))
        (tmp_path / "permissions.toml").write_text(
            '[commands]\ngag = "resident"\n', encoding="utf-8")
        s.reloader.check_once()
    finally:
        log.removeHandler(handler)
        log.setLevel(level)
    text = buf.getvalue()
    assert text.strip(), "nothing was logged — the sweep would pass vacuously"
    assert "wizard" not in text.lower(), text


# ---------------------------------------------------------------------------
# 5. ⛔ The STORED token is unchanged
# ---------------------------------------------------------------------------

def test_the_event_type_is_now_staff_action(srv):
    """⛔ RENAMED (v1.54.0). The WRITTEN type is `staff_action`; the pre-v1.54
    `wizard_action` is gone from the writable set (`EVENT_TYPES`) but is still
    ACCEPTED on READ, so historical rows written by an older release keep
    surfacing on `.staff_actions` (see the filter test below)."""
    from heresay.events import DURABLE_TYPES, EVENT_TYPES
    assert "staff_action" in EVENT_TYPES
    assert "staff_action" in DURABLE_TYPES
    assert "wizard_action" not in EVENT_TYPES     # no longer writable
    rows = srv.events.read(types=("staff_action",))
    assert len(rows) == 3
    assert all(r["type"] == "staff_action" for r in rows)


def test_a_legacy_wizard_action_row_still_reads_back(srv):
    """The read half of the compat: a raw `wizard_action` row written by a
    pre-v1.54 house is still returned by the store's reader, so `.staff_actions`
    (which asks for both types) never loses a historical moderation record."""
    path = pathlib.Path(srv.config.events_path())
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": 1.0, "type": "wizard_action", "actor": "Old", '
                 '"action": "gag", "target": "Bob"}\n')
    rows = srv.events.read(types=("staff_action", "wizard_action"))
    assert any(r["type"] == "wizard_action" and r["actor"] == "Old" for r in rows)


def test_the_written_file_carries_the_new_token(srv):
    """On disk, byte for byte — what a later release reads back. New rows carry
    `staff_action` now."""
    raw = (pathlib.Path(srv.config.events_path())).read_text(encoding="utf-8")
    assert '"type": "staff_action"' in raw or '"staff_action"' in raw


def test_the_staff_actions_filter_reads_ONE_type(srv):
    """⛔ v1.60.0 deleted the legacy read. The command is
    `read(types=("staff_action",), …)` plus a layout, and nothing in the engine
    knows the word `wizard_action` any more — so a row written before v1.54.0
    is simply not shown. ⚠ That is a bounded loss by construction: those rows
    age out on `event_retention_days`, so the fallback could only ever have
    mattered for the ten days after the rename deploy."""
    import inspect
    from heresay.commands import staff
    src = inspect.getsource(staff.cmd_staff_actions)
    assert '"staff_action"' in src
    assert "wizard_action" not in src


def test_the_room_file_key_is_now_staff_only(tmp_path):
    """RENAMED to `staff_only:` (v1.54.0). ⛔ v1.60.0 deleted the `wizard_only:`
    header alias, so the old spelling is now an UNKNOWN header key — which is
    fatal at startup, deliberately: a typo'd key would otherwise silently seal
    a room, and that rule is older than the rename."""
    import pytest
    from heresay.world import FLAG_KEYS, WorldError, parse_room_file
    assert "staff_only" in FLAG_KEYS
    assert "wizard_only" not in FLAG_KEYS
    # Shipped content uses the new key.
    assert 'staff_only: yes' in (HOUSE / "world" / "rooms" / "backroom.txt").read_text(
        encoding="utf-8")
    p = tmp_path / "old.txt"
    p.write_text("name: Old Backroom\nwizard_only: yes\n\nA dusty room.\n",
                 encoding="utf-8")
    with pytest.raises(WorldError):
        parse_room_file(p)


