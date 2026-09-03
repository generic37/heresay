# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""`permissions.toml` and the live reload spine (v1.37.0).

The command table moved out of `state.json` — config the operator edits must not
share a file with state the daemon rewrites — and became a file that is watched
and reloaded while the house runs. What these tests protect:

  * ⛔ THE DAEMON NEVER WRITES THE FILE. v1.37.0 had it create and top up
    `permissions.toml` itself and CRASH-LOOPED IN PRODUCTION — `/etc` is mounted
    read-only by the hardened unit, so the first boot raised
    `OSError: [Errno 30]`. The tests that were missing are here: a boot with an
    unwritable directory, and a boot with no file at all;
  * the FILE: parsed, self-completed IN MEMORY, a bad value rejected, a stale
    line skipped;
  * the MIGRATION: an upgrading house's assignments survive, and the legacy
    `state.json` block is written back until `permissions.toml` exists and
    dropped once it does;
  * the CLOBBER, which is now impossible by construction: `state.json` no longer
    carries a `commands` key, so nothing the daemon writes can eat an edit;
  * the RELOAD: an edit reaches a session that is ALREADY CONNECTED, on its next
    dispatch, with no restart — and a broken file is rejected while the house
    stays up and keeps the table it had;
  * ⛔ NAMES ONLY (v1.43.0): a value in this file is a rank NAME from
    `[ranks] order` and nothing else. The role words the engine seeds with
    (`newb`/`floor`/`staff`/`arch`) are resolved when the file is WRITTEN and are
    not legal in it — a leftover one is refused exactly like a typo, and refusing
    means the house does not boot.
"""

import json
import time
from pathlib import Path

import pytest

import heresay.commands as C
from heresay import permissions, reload as reload_mod
from heresay.commands import dispatch
from heresay.commands.registry import OWNER, RESIDENT, STAFF, command_seed
from heresay.config import Config
from heresay.server import TalkerServer
from heresay.state import StateStore
from heresay.theme import DEFAULT_THEME, Theme
from heresay.world import load_world

from conftest import FakeSession, COMMONS_THEME

WORLD = Path(__file__).resolve().parent / "fixtures" / "house" / "world"

# ⚠ The servers built here load the SHIPPED house theme (house/theme.toml =
# Commons: resident/staff/owner), not the engine's GENERIC built-in
# DEFAULT_THEME (resident/staff/owner). `command_rank`/`resolve_anchor` resolve
# a stored value against the theme they are given, so every `T`-keyed assertion
# here must use the theme the server actually loaded — Commons — or a stored
# "staff" read against the generic ladder falls through to the arch fail-safe.
T = COMMONS_THEME


def _cfg(tmp_path, **over):
    base = dict(
        owner_name="Generic",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        events_file=str(tmp_path / "events.jsonl"),
        resume_file=str(tmp_path / "resume.json"),
        ban_history_file=str(tmp_path / "ban_history.json"),
        world_dir=str(WORLD), entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000, reload_poll_seconds=0,   # the loop is off; we tick by hand
    )
    base.update(over)
    return Config(**base)


def _theme_file(tmp_path, ladder):
    """Write a theme.toml describing `ladder`, so a server built here really
    loads it (rather than being handed a Theme after the fact — the ladder is an
    input to the permissions read and to the accounts, both at construction)."""
    f = tmp_path / "theme.toml"
    order = ", ".join(f'"{r}"' for r in ladder.order)
    f.write_text(f'[ranks]\norder = [{order}]\nnewb = "{ladder.newb_rank}"\n'
                 f'resident = "{ladder.resident_rank}"\nstaff = "{ladder.staff_rank}"\n',
                 encoding="utf-8")
    return f


def _server(tmp_path, _theme=None, **over):
    if _theme is not None:
        over.setdefault("theme_file", str(_theme_file(tmp_path, _theme)))
    return TalkerServer(_cfg(tmp_path, **over))


def seeded(ladder=T, **over):
    """The seed as it lands in a running house: RANK NAMES, not role words.

    ⚠ The seed itself stays generic (`floor`/`staff`/`arch`) — that is the
    portable default and a test pins it — but everything downstream of it, the
    in-memory table and the written file alike, speaks this house's names."""
    table = {k: ladder.resolve_anchor(v) for k, v in command_seed().items()}
    table.update(over)
    return table


def _perm_path(tmp_path):
    return tmp_path / "permissions.toml"


def _write(tmp_path, body):
    _perm_path(tmp_path).write_text(body, encoding="utf-8")


def _table(tmp_path):
    return {k: v for k, v in
            __import__("tomllib").loads(
                _perm_path(tmp_path).read_text(encoding="utf-8"))["commands"].items()}


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------

def test_a_fresh_house_writes_NOTHING_and_runs_on_the_defaults(tmp_path):
    """⛔ The v1.37.0 bug, inverted into a test. The daemon does not create its
    own config: it runs on the built-in table and says so."""
    srv = _server(tmp_path)
    assert not _perm_path(tmp_path).exists()      # nothing written, ever
    # ⚠ Complete anyway, in memory — and in RANK NAMES (v1.43.0): the seed's
    # role words are resolved on the way in, so the running table is identical
    # whether or not `--write-permissions` has ever been run.
    assert srv.state.commands == seeded()
    assert any("built-in defaults" in f for f in srv.load_report.facts)


def test_the_cli_writes_a_complete_commented_file(tmp_path):
    """`--write-permissions` is the half that writes — run as root at deploy."""
    from heresay.__main__ import write_permissions
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_base_config_toml(tmp_path), encoding="utf-8")
    assert write_permissions(str(cfg_path)) == 0
    text = _perm_path(tmp_path).read_text(encoding="utf-8")
    assert text.startswith("# permissions.toml")
    assert "[commands]" in text
    assert "# moderation" in text                 # grouped, like the help screen
    assert _table(tmp_path) == seeded()           # complete, and NAMES ONLY
    # ⛔ Not one role word reached the disk.
    # ⛔ Names only: every written value is a real RANK on the house ladder, so
    # no pure-role anchor (staff/owner/floor/arch/wizard/newb) leaked. ("resident"
    # is a legal Commons rank AND an anchor word, so an ANCHORS intersection
    # would trip on it — the honest check is "all values are ranks", v1.54.0.)
    assert set(_table(tmp_path).values()) <= set(T.order)


def test_a_read_only_directory_boots_fine(tmp_path):
    """⛔ THE PRODUCTION INCIDENT, as a test. v1.37.0 crash-looped here:

        OSError: [Errno 30] Read-only file system: '/etc/heresay/permissions.toml'

    A config file the daemon cannot write — or cannot even reach — must degrade
    to "runs on the in-memory table", never to a house that will not start."""
    import os
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)                           # read + execute, no write
    try:
        srv = _server(tmp_path, permissions_file=str(ro / "permissions.toml"))
        assert srv.state.commands == seeded()
        assert not (ro / "permissions.toml").exists()
        # ...and the gate works, which is the only thing that actually matters.
        assert srv.state.command_rank("gag", T) == T.staff_rank
    finally:
        os.chmod(ro, 0o755)


def test_the_file_is_beside_config_not_in_house(tmp_path):
    """⚠ deploy.sh rsyncs house/ with --delete --delete-excluded, so a file there
    is overwritten by every push (and an exclude would DELETE it on the server).
    /etc/heresay is outside the rsync entirely, which is why config.toml is there
    and why this is too."""
    cfg = Config(base_dir=tmp_path, house_dir="house")
    assert cfg.permissions_path() == tmp_path / "permissions.toml"
    assert cfg.house_path() not in cfg.permissions_path().parents


def test_an_operators_value_is_authoritative(tmp_path):
    _write(tmp_path, '[commands]\ngag = "resident"\n')
    srv = _server(tmp_path)
    assert srv.state.command_rank("gag", T) == T.resident_rank


def test_a_missing_command_takes_its_default_IN_MEMORY(tmp_path):
    """Complete in the table, untouched on disk. The file gains the line the
    next time root runs `--write-permissions` — never at boot."""
    body = '[commands]\ngag = "resident"\n'
    _write(tmp_path, body)
    srv = _server(tmp_path)
    assert srv.state.command_rank("boot", T) == T.staff_rank
    assert srv.state.command_rank("agreement", T) == T.resident_rank
    assert srv.state.commands == seeded(gag="resident")
    # ⛔ Byte for byte what it was. The daemon writes no config.
    assert _perm_path(tmp_path).read_text(encoding="utf-8") == body


def test_the_cli_appends_only_what_is_missing_and_keeps_the_operators_file(tmp_path):
    from heresay.__main__ import write_permissions
    body = ('# my own note\n[commands]\ngag = "resident"\nboot = "owner"\n')
    _write(tmp_path, body)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_base_config_toml(tmp_path), encoding="utf-8")
    assert write_permissions(str(cfg_path)) == 0

    text = _perm_path(tmp_path).read_text(encoding="utf-8")
    assert "# my own note" in text                # their comment
    assert 'gag  = "resident"' in text            # their VALUES, re-aligned
    assert 'boot = "owner"' in text
    # ⛔ v1.60.1: no banner, and nothing appended at the end. The new commands
    # went into their own sections — see the writer's own tests below.
    assert "# Added by Heresay" not in text
    assert _table(tmp_path) == seeded(gag="resident", boot="owner")

    # Idempotent: a complete file is left byte-identical.
    before = _perm_path(tmp_path).read_bytes()
    assert write_permissions(str(cfg_path)) == 0
    assert _perm_path(tmp_path).read_bytes() == before


def test_in_memory_completion_is_reported(tmp_path):
    """So an operator learns from `.load_report` that the file wants topping up,
    rather than from a command quietly not being there."""
    _write(tmp_path, '[commands]\ngag = "resident"\n')
    srv = _server(tmp_path)
    assert any("built-in defaults" in f and "--write-permissions" in f
               for f in srv.load_report.facts)


def test_a_bad_value_rejects_the_whole_file(tmp_path):
    """A permission table with an unreadable entry has no safe reading, and
    guessing which way to err on somebody's access is not the engine's call."""
    _write(tmp_path, '[commands]\ngag = "sideways"\ngo = 3\n')
    with pytest.raises(permissions.PermissionsError):
        permissions.parse(_perm_path(tmp_path).read_text(), command_seed())


def test_a_bad_value_is_fatal_at_boot(tmp_path):
    _write(tmp_path, '[commands]\ngag = 3\n')
    with pytest.raises(permissions.PermissionsError):
        _server(tmp_path)


def test_broken_toml_is_fatal_at_boot(tmp_path):
    _write(tmp_path, '[commands\ngag = "resident"\n')
    with pytest.raises(permissions.PermissionsError):
        _server(tmp_path)


def test_a_stale_line_is_skipped_not_fatal(tmp_path):
    """A name is far likelier to be a line from a past release than a mistake
    with consequences; rejecting the table for one dead line would punish the
    house for the engine's own history."""
    _write(tmp_path, '[commands]\nkeeperhelp = "staff"\ngag = "resident"\n')
    srv = _server(tmp_path)
    assert "keeperhelp" not in srv.state.commands
    assert srv.state.command_rank("gag", T) == T.resident_rank
    assert any("keeperhelp" in w for w in srv.load_report.warnings)


def test_a_value_naming_no_rank_is_now_FATAL_at_boot(tmp_path):
    """⚠ A DELIBERATE REVERSAL (v1.38.0). Until now an unresolvable value
    degraded that command to arch-only with a warning, on the "locking one thing
    down is safe" reasoning. That was right while the only legal values were
    three fixed words — a typo was nearly impossible. Now a value may be any RANK
    NAME, so a typo is easy, and its symptom is a command silently out of reach
    of the people it was given to. The gate is invisible by design: nobody
    notices. An error to fix, then, not a policy to guess."""
    _write(tmp_path, '[commands]\ngag = "warden"\n')
    with pytest.raises(permissions.PermissionsError) as exc:
        _server(tmp_path)
    assert "not a valid rank" in str(exc.value)
    assert "warden" in str(exc.value)


def test_a_rank_NAME_resolves_and_inherits(tmp_path):
    """The point of the feature: assign a command to any rung, and everyone
    above it keeps the command — the same threshold the anchors have."""
    ladder = Theme(order=("newcomer", "citizen", "warden", "High Warden",
                          "Steward"),
                   resident="citizen", staff="warden")
    _write(tmp_path, '[commands]\nban_ip = "High Warden"\n')
    srv = _server(tmp_path, _theme=ladder)
    assert srv.state.command_rank("ban_ip", ladder) == "High Warden"
    for rank, allowed in (("newcomer", False), ("citizen", False),
                          ("warden", False), ("High Warden", True),
                          ("Steward", True)):
        assert C._visible("ban_ip", rank, ladder, srv.state) is allowed, rank


@pytest.mark.parametrize("anchor", ["newb", "resident", "staff", "owner"])
def test_a_role_WORD_in_the_file_is_refused_like_any_other_non_rank(tmp_path, anchor):
    """⛔ THE v1.43.0 REVERSAL, and it is the whole release.

    v1.38.0 took anchors AND names, on the reading that it was additive. It was
    also unreadable: `gag = "staff"` and `gag = "warden"` meant the same thing,
    one of them was a word that appears nowhere in the operator's theme.toml,
    and "which ranks may I write here?" had two answers. The roles stay what the
    CODE seeds with and are resolved when the file is WRITTEN; in the file they
    are simply not ranks, and not-a-rank has exactly one consequence."""
    ladder = Theme(order=("newcomer", "citizen", "warden", "Steward"),
                   resident="citizen", staff="warden")
    _write(tmp_path, f'[commands]\ngag = "{anchor}"\n')
    with pytest.raises(permissions.PermissionsError) as exc:
        _server(tmp_path, _theme=ladder)
    assert "not a valid rank" in str(exc.value)
    # ...and it says the useful thing, because a word that was legal one release
    # ago deserves better than "no".
    assert "--write-permissions" in str(exc.value)


def test_a_rank_name_from_every_rung_is_legal(tmp_path):
    """The other half of the same rule: any rank on the ladder, precisely."""
    ladder = Theme(order=("newcomer", "citizen", "warden", "Steward"),
                   resident="citizen", staff="warden")
    _write(tmp_path, '[commands]\ngag = "warden"\nlook = "newcomer"\n'
                     'bans = "Steward"\n')
    srv = _server(tmp_path, _theme=ladder)
    assert srv.state.command_rank("gag", ladder) == "warden"
    assert srv.state.command_rank("look", ladder) == "newcomer"
    assert srv.state.command_rank("bans", ladder) == "Steward"


# ---------------------------------------------------------------------------
# The migration out of state.json
# ---------------------------------------------------------------------------

def _legacy_state(tmp_path, commands, **extra):
    payload = {"version": 1, "commands_version": 1, "commands": commands}
    payload.update(extra)
    (tmp_path / "state.json").write_text(json.dumps(payload), encoding="utf-8")


def test_an_upgrading_house_keeps_working_before_the_file_exists(tmp_path):
    """The window between deploying the code and running `--write-permissions`.
    The legacy block is still the table, and — crucially — is still WRITTEN BACK,
    because until the file exists it is the only copy of these assignments."""
    _legacy_state(tmp_path, {**command_seed(), "gag": "resident", "boot": "owner"},
                  allow_logins=False, jailed=["bob"])
    srv = _server(tmp_path)
    assert not _perm_path(tmp_path).exists()      # the daemon wrote nothing
    assert srv.state.command_rank("gag", T) == T.resident_rank
    assert srv.state.allow_logins is False and srv.state.jailed == {"bob"}

    srv.state.set_jailed("carol")                 # any state write at all
    on_disk = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert on_disk["commands"]["gag"] == "resident"  # ⚠ NOT dropped yet
    assert on_disk["commands"]["boot"] == "owner"


def test_the_cli_migrates_and_then_the_block_is_dropped(tmp_path):
    """The handover, in the order a deploy performs it: root writes the file
    (merging the legacy block), the daemon boots on it, and the next state write
    takes the stale block out for good — which is what ends the old clobber."""
    from heresay.__main__ import write_permissions
    _legacy_state(tmp_path, {**command_seed(), "gag": "resident", "boot": "owner"},
                  allow_logins=False, jailed=["bob"])
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_base_config_toml(tmp_path), encoding="utf-8")
    assert write_permissions(str(cfg_path)) == 0

    # The operator's two assignments came across — RESOLVED to rank names,
    # because the legacy block was written in role words and the file may not be.
    assert _table(tmp_path)["gag"] == "resident"
    assert _table(tmp_path)["boot"] == "owner"

    srv = _server(tmp_path)
    assert srv.state.command_rank("gag", T) == T.resident_rank
    assert srv.state.allow_logins is False and srv.state.jailed == {"bob"}
    srv.state.set_jailed("carol")
    assert "commands" not in json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8"))

    # And the boot itself still wrote nothing to /etc.
    before = _perm_path(tmp_path).read_bytes()
    _server(tmp_path)
    assert _perm_path(tmp_path).read_bytes() == before


def test_a_v128_era_table_is_migrated_and_completed(tmp_path):
    """The blast radius, closed by the move: a table seeded when the table was
    invented is missing everything added since, and each lands at the rank its
    release declared."""
    old = {k: v for k, v in command_seed().items()
           if k not in ("staff_actions", "ban_ip", "unban_ip", "bans",
                        "staff_calls", "security", "load_report", "agreement")}
    _legacy_state(tmp_path, old)
    srv = _server(tmp_path)
    assert srv.state.command_rank("agreement", T) == T.resident_rank
    for verb in ("staff_calls", "load_report", "ban_ip", "unban_ip", "bans"):
        assert srv.state.command_rank(verb, T) == T.staff_rank, verb
    for verb in ("staff_actions", "security"):
        assert srv.state.command_rank(verb, T) == T.owner_rank, verb


def test_state_json_never_writes_a_command_table_again(tmp_path):
    st = StateStore(tmp_path / "state.json", command_seed=command_seed(), ladder=T)
    st.set_command_table(command_seed())
    st.set_logins(False)
    assert "commands" not in json.loads(
        (tmp_path / "state.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# The reload — the point of the whole thing
# ---------------------------------------------------------------------------

def _tick(srv):
    return srv.reloader.check_once()


def _connected_resident(srv, name="Corvid"):
    return FakeSession(srv.world, name=name, state=srv.state, config=srv.config)


def test_an_edit_reaches_an_already_connected_session(tmp_path):
    """⚡ The whole feature in one test: no restart, no reconnect, no refresh —
    the gate asks the shared table on every dispatch, so replacing its contents
    IS the reload."""
    srv = _server(tmp_path)
    res = _connected_resident(srv)
    dispatch(res, ".gag Bob")
    assert "Unknown command" in res.text                  # a resident, before

    _write(tmp_path, '[commands]\ngag = "resident"\n')
    results = _tick(srv)
    assert results and results[0].ok

    res.clear()
    dispatch(res, ".gag Bob")
    assert "Unknown command" not in res.text              # ...and after


def test_the_reload_reports_the_diff_not_that_it_happened(tmp_path):
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\ngag = "resident"\n')
    r = _tick(srv)[0]
    assert r.changes == [".gag staff → resident"]
    assert "gag" in r.summary()


def test_a_broken_file_is_rejected_and_the_house_keeps_running(tmp_path):
    """⛔ The rule the whole spine exists for: a live house must never be closed
    by a typo. At boot the same file is fatal; at runtime it is refused."""
    srv = _server(tmp_path)
    before = dict(srv.state.commands)
    _write(tmp_path, '[commands]\ngag = 3\n')
    r = _tick(srv)[0]
    assert not r.ok and r.errors
    assert srv.state.commands == before                   # untouched
    res = _connected_resident(srv)
    dispatch(res, ".gag Bob")
    assert "Unknown command" in res.text                  # still gated as before


def test_a_half_written_file_is_simply_the_next_tick(tmp_path):
    """Validate-before-swap is what makes a mid-save read safe: it fails to
    parse, is rejected, and the finished file is picked up on the next pass."""
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\ngag = "flo')                 # caught mid-write
    assert not _tick(srv)[0].ok
    _write(tmp_path, '[commands]\ngag = "resident"\n')            # the save finished
    r = _tick(srv)[0]
    assert r.ok and r.changes


def test_an_unchanged_file_is_not_reloaded(tmp_path):
    _write(tmp_path, '[commands]\ngag = "staff"\n')
    srv = _server(tmp_path)
    assert _tick(srv) == []                                # nothing changed
    _write(tmp_path, _perm_path(tmp_path).read_text() + '\n# a comment\n')
    assert _tick(srv)                                      # touched -> a pass


def test_the_watcher_does_not_watch_state_json(tmp_path):
    """⚠ The daemon rewrites state.json; watching it would be a feedback loop."""
    srv = _server(tmp_path)
    watched = [str(p) for src in srv.reloader.sources for p in src.paths(srv)]
    assert not any(p.endswith("state.json") for p in watched)


def test_the_reload_is_recorded_as_a_fact(tmp_path):
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\ngag = "resident"\n')
    _tick(srv)
    acts = srv.events.read(types=("staff_action",))
    assert [(e["actor"], e["action"], e["target"]) for e in acts] == \
        [("config", "reload", "permissions")]


def test_a_rejected_reload_is_recorded_too(tmp_path):
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\ngag = 3\n')
    _tick(srv)
    acts = srv.events.read(types=("staff_action",))
    assert acts and acts[-1]["action"] == "reload_rejected"


def test_privileged_commands_still_reads_the_code_seed(tmp_path):
    """⛔ The covenant tests read this. "Nothing above the floor reads another's
    mail" is a statement about the HANDLERS, and an operator editing a file must
    not be able to switch the check off."""
    from heresay.commands.registry import privileged_commands
    _write(tmp_path, '[commands]\nmailpurge = "resident"\nstaffmail = "resident"\n')
    _server(tmp_path)
    assert "mailpurge" in privileged_commands()
    assert "staffmail" in privileged_commands()


def test_no_new_commands_were_invented(tmp_path):
    """The watcher is the trigger and `.load_report` is the readout — there is
    nothing to type."""
    from heresay.commands.registry import BY_NAME
    for verb in ("reload", "permissions", "setperm", "reloadperms"):
        assert verb not in BY_NAME


# ---------------------------------------------------------------------------
# Source 2 — config values
# ---------------------------------------------------------------------------
#
# The four classes, one test each, because the whole risk here is a value being
# in the wrong class: a "live now" that turns out to be boot-copied looks like it
# worked and did nothing.

def _config_server(tmp_path, body="", world="", storage="", poll=0,
                   moderation='owner = "Generic"'):
    """A server whose config.toml is a real file it can re-read."""
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        _base_config_toml(tmp_path, world, storage, poll, moderation) + body,
        encoding="utf-8")
    from heresay.config import load_config
    cfg = load_config(cfg_path)
    return TalkerServer(cfg, config_path=cfg_path)


def _base_config_toml(tmp_path, world="", storage="", poll=0,
                      moderation='owner = "Generic"'):
    # ⚠ owner_name lives in the [moderation] table (v1.54.0). ⛔ The pre-v1.54
    # `arch` key was deleted in v1.60.0. The house refuses to boot without it, so the
    # base always names one; tests that need to change a [moderation] value pass
    # it through `moderation=` rather than appending a second [moderation] header
    # (a TOML duplicate-table error).
    w = str(WORLD).replace("\\", "/")
    t = str(tmp_path).replace("\\", "/")
    h = str(WORLD.parent).replace("\\", "/")     # the shipped house/ (agreement, help, theme)
    return f"""
[storage]
house_dir = "{h}"
accounts_file = "{t}/accounts.json"
mail_file = "{t}/mailboxes.json"
mail_key_file = "{t}/mail.key"
revtell_file = "{t}/revtells.json"
bans_file = "{t}/bans.json"
state_file = "{t}/state.json"
permissions_file = "{t}/permissions.toml"
events_file = "{t}/events.jsonl"
resume_file = "{t}/resume.json"
ban_history_file = "{t}/ban_history.json"
world_dir = "{w}"
{storage}

[world]
entry_room = "atrium"
jail_room = "holding_cell"
reload_poll_seconds = {poll}
{world}

[moderation]
{moderation}
"""


def _rewrite(tmp_path, body="", world="", storage="", poll=0,
             moderation='owner = "Generic"'):
    """Rewrite the whole config. ⚠ `world=`/`storage=`/`moderation=` add lines
    INSIDE the existing tables: appending a second `[world]` header is a TOML
    duplicate-table error, which the reload would (correctly) reject."""
    p = tmp_path / "config.toml"
    p.write_text(
        _base_config_toml(tmp_path, world, storage, poll, moderation) + body,
        encoding="utf-8")


def test_a_live_now_value_takes_effect_immediately(tmp_path):
    """Read at USE time off the shared Config, so moving the field is the whole
    of it — `.ban <name>` picks the new default up on its next run."""
    srv = _config_server(tmp_path)
    assert srv.config.ban_default_hours == 24.0
    _rewrite(tmp_path, moderation='owner = "Generic"\nban_default_hours = 6')
    r = _tick(srv)[0]
    assert r.ok
    assert srv.config.ban_default_hours == 6.0
    assert any("ban_default_hours" in c for c in r.changes)


def test_a_boot_copied_value_is_re_applied(tmp_path):
    """⚠ The class that would silently do nothing: `mailbox_cap` is COPIED into
    the MailStore at boot, so moving the Config field alone changes nothing."""
    srv = _config_server(tmp_path)
    assert srv.mail.cap == 50
    _rewrite(tmp_path, '\n[mail]\nmailbox_cap = 7\n')
    r = _tick(srv)[0]
    assert r.ok
    assert srv.config.mailbox_cap == 7
    assert srv.mail.cap == 7                      # the copy, re-applied


def test_every_boot_copied_field_re_applies(tmp_path):
    """The list is explicit so it cannot rot — so it is swept, not sampled."""
    srv = _config_server(tmp_path)
    for field, apply in reload_mod.CONFIG_REAPPLY:
        apply(srv, getattr(srv.config, field))    # every lambda reaches its object


def test_the_house_NAME_is_CONFIG_and_reloads_from_there(tmp_path):
    """⚠ `instance_name` moved out of `house/theme.toml` in v1.60.0, and it is
    boot-COPIED: the server fills it into the frozen `Theme` in `_build`, so
    moving the Config field alone would rename the house in the file and
    nowhere else. The hook re-applies the copy — and pushes it, because the
    tab's name is the browser's to hold and it cannot discover a change."""
    srv = _config_server(tmp_path, world='instance_name = "Commons"')
    assert srv.theme.instance_name == "Commons"
    _rewrite(tmp_path, world='instance_name = "Elsewhere"')
    r = _tick(srv)[0]
    assert r.ok
    assert srv.config.instance_name == "Elsewhere"
    assert srv.theme.instance_name == "Elsewhere"     # the copy, re-applied


def test_the_echo_window_reaches_the_world(tmp_path):
    """A boot-copied value that is NOT quoted in the agreement, so it is live.
    (`rev_window_minutes` used to be this test; it is reboot-gated now — see
    below.)"""
    srv = _config_server(tmp_path)
    assert srv.world.echo_min == 10 * 60.0
    _rewrite(tmp_path, world="echo_min_minutes = 2")
    _tick(srv)
    assert srv.world.echo_min == 2 * 60.0


def test_a_per_session_value_applies_to_new_connections_only(tmp_path):
    srv = _config_server(tmp_path)
    _rewrite(tmp_path, '\n[ratelimit]\nspeech_burst = 99\n')
    r = _tick(srv)[0]
    assert srv.config.speech_burst == 99          # the next Session reads this
    assert not any("speech_burst" in c for c in r.changes)
    assert any("new connections only" in n and "speech_burst" in n
               for n in r.notes)


def test_an_infra_value_is_seen_but_not_applied(tmp_path):
    """Rebinding a socket under a running house is a different feature; a change
    is reported so nobody wonders why nothing happened."""
    srv = _config_server(tmp_path)
    was = srv.config.ws_port
    _rewrite(tmp_path, '\n[listeners]\nws_port = 9999\n')
    r = _tick(srv)[0]
    assert srv.config.ws_port == was              # deliberately NOT applied
    assert any("NOT applied" in n and "ws_port" in n for n in r.notes)


# ---------------------------------------------------------------------------
# ⛔ The classification is the reload's whole promise (v1.52.4)
#
# The spine either APPLIES a change or says RESTART TO APPLY. A field that falls
# through to "live now" while the thing that actually reads it kept the boot
# value is the one state the report may never be in: it says "applied" about
# something inert. These are the four that were.
# ---------------------------------------------------------------------------

def test_arch_name_is_restart_gated(tmp_path):
    """⛔ Who owns the house is not a live edit (the maintainer's call). It is copied into
    the AccountStore at boot AND read at use time all over the engine, so a live
    change re-pointed the top rank within one poll — unvalidated, with the store
    still holding the old name. Restart-gated, so the boot check (does the arch
    account exist?) runs again on the way back up."""
    from heresay.commands import staff as staff_mod

    srv = _config_server(tmp_path)
    assert srv.config.owner_name == "Generic"
    # A config that renames the owner (via the legacy `arch` key here) — the
    # reload must SEE it and NOT apply it (restart-gated infra).
    _rewrite(tmp_path, moderation='arch = "Mallory"')
    r = _tick(srv)[0]

    assert r.ok
    assert srv.config.owner_name == "Generic"          # deliberately NOT applied
    assert srv.accounts.owner_name == "Generic"        # nor the boot-time copy
    sess = FakeSession(srv.world, name="Mallory", state=srv.state,
                       config=srv.config)
    assert not staff_mod._is_arch(sess, "Mallory")    # and nobody was crowned
    assert any("NOT applied" in n and "owner_name" in n for n in r.notes)


def test_reload_poll_seconds_is_restart_gated(tmp_path):
    """⛔ Self-referential: the watch loop CLOSES OVER the interval and the
    `if reload_poll_seconds > 0` on/off gate is read once, at boot. A live edit
    could neither retune the loop nor stop it, so reporting it as applied was
    the reload lying about itself."""
    srv = _config_server(tmp_path)
    assert srv.config.reload_poll_seconds == 0
    _rewrite(tmp_path, poll=30)
    r = _tick(srv)[0]

    assert r.ok
    assert srv.config.reload_poll_seconds == 0        # the loop's own value
    assert any("NOT applied" in n and "reload_poll_seconds" in n for n in r.notes)


def test_the_control_socket_path_is_restart_gated(tmp_path):
    """⛔ Bound at boot like every other path (W1). `--shutdown` loads the config
    FRESH, so a live change would have the CLI knocking at a new path while the
    daemon still listens on the old one — a countdown that silently reaches
    nobody."""
    srv = _config_server(tmp_path)
    was = srv.config.control_socket_path()
    _rewrite(tmp_path, storage='control_socket_file = "elsewhere.sock"')
    r = _tick(srv)[0]

    assert r.ok
    assert srv.config.control_socket_path() == was    # where it really listens
    assert any("NOT applied" in n and "control_socket_file" in n for n in r.notes)


def test_the_forbidden_names_path_is_restart_gated(tmp_path):
    """The blocklist's CONTENTS are live (its own source); which FILE is the
    blocklist is infra, exactly like `permissions_file` beside it. The two are
    the same shape of setting and were classified differently."""
    srv = _config_server(tmp_path)
    was = srv.config.forbidden_names_path()
    _rewrite(tmp_path, storage='forbidden_names_file = "other_names.txt"')
    r = _tick(srv)[0]

    assert r.ok
    assert srv.config.forbidden_names_path() == was
    assert any("NOT applied" in n and "forbidden_names_file" in n for n in r.notes)


def test_report_retention_reaches_the_live_mail_store(tmp_path):
    """The other half of the promise: a value that CAN apply live must actually
    do so. `report_retention_days` is copied into the MailStore's per-box
    retention at boot, so moving the Config field alone left reports ageing on
    the old number while the report said it had changed."""
    from heresay.commands import REPORT_BOX

    srv = _config_server(tmp_path)
    # ⚠ 10 since v1.58.0 — the box copy stopped being the record when the
    # capture file started being written at filing.
    assert srv.mail.purge_days_for(REPORT_BOX) == 10
    _rewrite(tmp_path, '\n[mail]\nreport_retention_days = 7\n')
    r = _tick(srv)[0]

    assert r.ok
    assert srv.config.report_retention_days == 7
    assert srv.mail.purge_days_for(REPORT_BOX) == 7       # the copy, re-applied
    assert srv.mail.purge_days == 180                     # ordinary mail untouched
    assert any("report_retention_days" in c for c in r.changes)


def test_the_keepalive_knobs_are_new_connections_only(tmp_path):
    """Read when a socket is ACCEPTED, so a change reaches the next connection
    and never re-tunes a socket already open — which is what PER-SESSION means.
    Reported as such rather than as a change that landed everywhere."""
    srv = _config_server(tmp_path)
    _rewrite(tmp_path, '\n[keepalive]\ntcp_keepidle = 42\n')
    r = _tick(srv)[0]

    assert r.ok
    assert srv.config.tcp_keepidle == 42              # the next accepted socket
    assert not any("tcp_keepidle" in c for c in r.changes)
    assert any("new connections only" in n and "tcp_keepidle" in n for n in r.notes)


def test_every_config_field_is_classified(tmp_path):
    """⛔ THE GUARD. The classification lists are documented as exhaustive, and
    "exhaustive" is worth nothing unless something checks. Every `Config` field
    must be in exactly one of the four classes or in the explicit
    `CONFIG_USE_TIME` allowlist — so a field added next year cannot quietly join
    the fall-through and be reported as applied when it is not.
    """
    from heresay.config import Config

    fields = set(Config.__dataclass_fields__)
    sets = {
        "CONFIG_INFRA": set(reload_mod.CONFIG_INFRA),
        "CONFIG_PER_SESSION": set(reload_mod.CONFIG_PER_SESSION),
        "CONFIG_REAPPLY": set(reload_mod.CONFIG_REAPPLY_FIELDS),
        "CONFIG_USE_TIME": set(reload_mod.CONFIG_USE_TIME),
    }

    # No name in any list may be a field the dataclass does not have — a typo
    # there is a classification that silently applies to nothing.
    for name, members in sets.items():
        assert members <= fields, f"{name} names non-fields: {members - fields}"
    assert set(reload_mod.AGREEMENT_QUOTED) <= fields

    covered = set().union(*sets.values())
    assert fields - covered == set(), (
        "unclassified Config field(s) — add each to a class in reload.py or, if "
        "it is genuinely read at use time off the shared Config, to "
        f"CONFIG_USE_TIME with a comment: {sorted(fields - covered)}")

    # Exactly one, so a reader can answer "what happens when this moves?" with
    # one lookup.
    for a, b in [(x, y) for x in sets for y in sets if x < y]:
        overlap = sets[a] & sets[b]
        assert not overlap, f"{a} and {b} both claim {sorted(overlap)}"

    # ⚠ The ONE documented overlap: a quoted value is boot-copied too, and keeps
    # its re-apply hook against the day it stops being quoted. AGREEMENT_QUOTED
    # is checked FIRST in `reload_config`, so it is the effective class.
    quoted = set(reload_mod.AGREEMENT_QUOTED)
    assert quoted <= sets["CONFIG_REAPPLY"]
    assert not quoted & (sets["CONFIG_INFRA"] | sets["CONFIG_PER_SESSION"]
                         | sets["CONFIG_USE_TIME"])


def test_a_broken_config_is_rejected_and_the_old_values_kept(tmp_path):
    srv = _config_server(tmp_path)
    (tmp_path / "config.toml").write_text("[world\nentry_room = ", encoding="utf-8")
    r = _tick(srv)[0]
    assert not r.ok and r.errors
    assert srv.config.entry_room == "atrium"


@pytest.mark.parametrize("body,field,check", [
    ("rev_window_minutes = 45", "rev_window_minutes",
     lambda s: s.world.review_window == 30 * 60.0),
    ("revtell_count = 5", "revtell_count",
     lambda s: s.revtells.cap == 20),
    ("event_retention_days = 3", "event_retention_days",
     lambda s: s.events.retention_days == 10.0),
])
def test_a_quoted_value_is_REBOOT_GATED(tmp_path, body, field, check):
    """⛔ A DELIBERATE REVERSAL (v1.40.0). v1.37.0 applied these live and
    recomputed the agreement on the spot — which meant that from that instant new
    logins consented to the NEW version while everyone already in-world stayed on
    the OLD one. **You cannot have different people under different agreements.**

    So neither the value NOR the agreement moves live. Both move together on the
    next boot, which is a clean cutover because it drops everyone."""
    srv = _config_server(tmp_path)
    before_hash = srv.agreement_hash
    before_value = getattr(srv.config, field)

    _rewrite(tmp_path, world=body)
    r = _tick(srv)[0]

    assert r.ok
    assert srv.agreement_hash == before_hash            # the terms did not move
    assert getattr(srv.config, field) == before_value   # nor did the value
    assert check(srv)                                   # nor its functional effect
    loud = " ".join(r.notes)
    assert "RESTART TO APPLY" in loud and field in loud
    assert "quoted in the agreement" in loud


@pytest.mark.parametrize("body,field", [
    ("rev_window_minutes = 45", "rev_window_minutes"),
    ("revtell_count = 5", "revtell_count"),
    ("event_retention_days = 3", "event_retention_days"),
])
def test_a_reboot_applies_the_value_AND_the_agreement_together(tmp_path, body, field):
    """The other half: on the next boot they move together, so the document can
    never say "30 minutes" while the buffer really keeps 45."""
    srv = _config_server(tmp_path)
    before = srv.agreement_hash
    _rewrite(tmp_path, world=body)

    from heresay.config import load_config
    rebooted = TalkerServer(load_config(tmp_path / "config.toml"),
                            config_path=tmp_path / "config.toml")
    assert getattr(rebooted.config, field) != getattr(srv.config, field)
    assert rebooted.agreement_hash != before


def test_an_unquoted_value_leaves_the_agreement_alone(tmp_path):
    srv = _config_server(tmp_path)
    before = srv.agreement_hash
    _rewrite(tmp_path, '\n[mail]\nmailbox_cap = 9\n')
    r = _tick(srv)[0]
    assert srv.agreement_hash == before
    assert not any("re-consent" in n.lower() for n in r.notes)


def test_a_connected_session_keeps_the_BOOT_agreement_text(tmp_path):
    """⛔ `.agreement` shows what people actually consented to — the version
    frozen at boot. Nothing recomputes it while the process runs (v1.40.0)."""
    srv = _config_server(tmp_path)
    res = FakeSession(srv.world, name="Corvid", state=srv.state, config=srv.config)
    res.agreement_text = srv.agreement_text
    srv.world.residents[res.resident.name.casefold()] = res.resident
    res.resident.session = res
    was = srv.agreement_text
    _rewrite(tmp_path, world="rev_window_minutes = 45")
    _tick(srv)

    # Unmoved — on the server AND in the hand of the person standing in a room.
    assert srv.agreement_text == was
    assert res.agreement_text == was
    flat = " ".join(srv.agreement_text.split())
    assert "last 30 minutes" in flat          # still the version they agreed to
    assert "last 45 minutes" not in flat


def test_the_config_source_is_skipped_when_there_is_no_path(tmp_path):
    """A test (or an embedding) that builds a Config by hand has no file to
    re-read, and the engine never guesses one: a stray config.toml in the CWD
    must not become authoritative over a running house."""
    srv = _server(tmp_path)
    assert srv.config_path is None
    watched = [str(p) for src in srv.reloader.sources for p in src.paths(srv)]
    assert all(not p.endswith("config.toml") for p in watched)


# ---------------------------------------------------------------------------
# `.load_report` shows the reload
# ---------------------------------------------------------------------------

def _report(srv, width=78):
    s = FakeSession(srv.world, name="Wizzy", level="wizard", width=width,
                    config=srv.config, load_report=srv.load_report)
    s.clear()
    dispatch(s, ".load_report")
    return s.text


def test_load_report_shows_the_last_reload(tmp_path):
    srv = _server(tmp_path)
    assert "reloaded" not in _report(srv)          # nothing has happened yet
    _write(tmp_path, '[commands]\ngag = "resident"\n')
    _tick(srv)
    out = _report(srv)
    assert "reloaded" in out
    assert "permissions: .gag staff → resident" in out


def test_load_report_shows_a_rejection_and_why(tmp_path):
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\ngag = 3\n')
    _tick(srv)
    out = _report(srv)
    assert "REJECTED" in out and "kept the old" in out


@pytest.mark.parametrize("width", [40, 78])
def test_the_reload_block_fits_both_widths(tmp_path, width):
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\ngag = "resident"\nban_ip = "resident"\n')
    _tick(srv)
    for line in _report(srv, width=width).split("\n"):
        assert len(line) <= width, repr(line)


# ---------------------------------------------------------------------------
# ⛔ The daemon writes no config, swept
# ---------------------------------------------------------------------------

def test_the_engine_has_no_write_of_the_permissions_file(tmp_path):
    """A source sweep, so the v1.37.0 mistake cannot be made a second time by
    somebody adding a "helpful" top-up back into the boot path.

    `permissions.write_file` is the CLI's, and `__main__` is the only caller."""
    import inspect
    import heresay.server as S
    import heresay.reload as RL
    import heresay.permissions as P
    for mod in (S, RL):
        src = inspect.getsource(mod)
        assert "write_file" not in src, mod.__name__
        assert "permissions_path()" not in src or "write" not in src.split(
            "permissions_path()")[1].split("\n")[0]
    # And in permissions.py itself, EVERY write lives inside `write_file`.
    # Parsed rather than grepped: the module docstring quotes `p.write_text(...)`
    # while describing the incident, and a string count would trip over its own
    # explanation.
    import ast
    tree = ast.parse(inspect.getsource(P))
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        writes = [c for c in ast.walk(fn)
                  if isinstance(c, ast.Call)
                  and ((isinstance(c.func, ast.Attribute)
                        and c.func.attr in ("write_text", "write_bytes", "mkdir"))
                       or (isinstance(c.func, ast.Attribute) and c.func.attr == "open"
                           and any(isinstance(a, ast.Constant) and a.value == "a"
                                   for a in c.args)))]
        assert not writes or fn.name == "write_file", fn.name


def test_a_boot_writes_nothing_at_all_beside_the_config(tmp_path):
    """Belt to the sweep's braces: run a real boot with an existing file and
    assert the bytes are identical afterwards."""
    body = '[commands]\ngag = "resident"\n'
    _write(tmp_path, body)
    before = _perm_path(tmp_path).read_bytes()
    _server(tmp_path)
    assert _perm_path(tmp_path).read_bytes() == before


def test_an_unreadable_file_degrades_rather_than_crashing(tmp_path, caplog):
    """Mode 000 — the other way a config file can be out of reach. Same rule as
    the read-only directory: it must never be the reason a house will not open."""
    import os
    _write(tmp_path, '[commands]\ngag = "resident"\n')
    os.chmod(_perm_path(tmp_path), 0o000)
    try:
        with caplog.at_level("WARNING"):
            srv = _server(tmp_path)
        assert srv.state.commands == seeded()          # the built-in table
        assert "could not read" in caplog.text
    finally:
        os.chmod(_perm_path(tmp_path), 0o644)


# ---------------------------------------------------------------------------
# ⚠ Boot-fatal, runtime-discard — the two halves of the strict rule (v1.38.0)
# ---------------------------------------------------------------------------

def test_a_live_edit_to_an_invalid_rank_is_discarded_and_says_why(tmp_path):
    """⛔ The runtime-never-closes rule, meeting the new strictness. At boot the
    same file stops the daemon; at runtime it must not — the old table stays,
    the house stays up, and the reason is reported."""
    _write(tmp_path, '[commands]\ngag = "staff"\n')
    srv = _server(tmp_path)
    before = dict(srv.state.commands)

    _write(tmp_path, '[commands]\ngag = "wardn"\n')      # a typo, live
    r = _tick(srv)[0]
    assert not r.ok
    assert "not a valid rank" in " ".join(r.errors)
    assert srv.state.commands == before                  # untouched

    res = _connected_resident(srv)
    dispatch(res, ".gag Bob")
    assert "Unknown command" in res.text                 # still gated as before
    assert "REJECTED" in _report(srv)


def test_a_live_edit_to_a_valid_rank_name_applies(tmp_path):
    ladder = Theme(order=("newcomer", "citizen", "warden", "Steward"),
                   resident="citizen", staff="warden")
    _write(tmp_path, '[commands]\ngag = "warden"\n')
    srv = _server(tmp_path, _theme=ladder)
    assert srv.state.command_rank("gag", ladder) == "warden"
    _write(tmp_path, '[commands]\ngag = "citizen"\n')
    r = _tick(srv)[0]
    assert r.ok and r.changes == [".gag warden → citizen"]
    assert srv.state.command_rank("gag", ladder) == "citizen"


# ---------------------------------------------------------------------------
# ⛔ Names only (v1.43.0) — the migration off the role words
# ---------------------------------------------------------------------------
#
# The rule: a value in `permissions.toml` is a rank NAME from `[ranks] order`,
# full stop. The CODE keeps seeding in role anchors — that is the portable,
# house-independent default and must not become Commons-specific — and
# `--write-permissions` resolves them on the way to disk. So the two vocabularies
# meet in exactly one place, at write time, run by a person.

def _cli(tmp_path):
    from heresay.__main__ import write_permissions
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_base_config_toml(tmp_path), encoding="utf-8")
    return write_permissions(str(cfg_path))


def test_the_code_seed_stays_GENERIC(tmp_path):
    """⚠ The seed is the engine's default for ANY house, so it speaks in roles.
    Baking Commons's words into it would make the defaults untrue everywhere
    else — a house whose ranks are citizen/warden/Steward would ship with a
    table naming ranks it does not have."""
    values = set(command_seed().values())
    assert values <= {RESIDENT, STAFF, OWNER}
    # ⚠ v1.54.0: `resident` is BOTH a role anchor and Commons's floor RANK
    # name, so it legitimately appears in the seed. The words that must never
    # be baked in are the house-specific display words the seed does NOT use.
    assert values <= {RESIDENT, STAFF, OWNER}


def test_write_permissions_CONVERTS_an_anchor_file_to_names(tmp_path):
    """⛔ THE MIGRATION. Commons's live file was written in role words; this
    is the one pass that rewrites them, run as root at deploy with the old code
    still serving."""
    body = ('# my own note, which must survive\n'
            '[commands]\n'
            'gag   = "staff"     # and this trailing comment\n'
            'boot  = "owner"\n'
            'look  = "resident"\n')     # already a name: must not move
    _write(tmp_path, body)
    assert _cli(tmp_path) == 0

    text = _perm_path(tmp_path).read_text(encoding="utf-8")
    # ⚠ The trailing comment and the ORDER are theirs; the SPACING is the
    # writer's since v1.60.1 (one `=` column per section, set by the longest
    # key in it — here `look`, so three-character `gag` pads out to match).
    assert 'gag  = "staff"     # and this trailing comment' in text
    assert 'boot = "owner"' in text
    assert 'look = "resident"' in text           # value untouched
    assert "# my own note, which must survive" in text
    # A canonicalization, not a reset: their comments and ordering are theirs.
    assert text.index("gag") < text.index("boot") < text.index("look")
    # ⛔ Not one role word left anywhere in the file.
    # ⛔ Names only: every written value is a real RANK on the house ladder, so
    # no pure-role anchor (staff/owner/floor/arch/wizard/newb) leaked. ("resident"
    # is a legal Commons rank AND an anchor word, so an ANCHORS intersection
    # would trip on it — the honest check is "all values are ranks", v1.54.0.)
    assert set(_table(tmp_path).values()) <= set(T.order)


def test_the_conversion_is_idempotent(tmp_path):
    """Running it twice is a no-op — after the first pass there is no anchor to
    match, so the second finds a complete names-only file and writes nothing."""
    _write(tmp_path, '[commands]\ngag = "staff"\n')
    assert _cli(tmp_path) == 0
    once = _perm_path(tmp_path).read_bytes()
    assert _cli(tmp_path) == 0
    assert _perm_path(tmp_path).read_bytes() == once


def test_the_converted_file_boots_and_gates_exactly_as_before(tmp_path):
    """The point of the whole exercise: the same permissions, said in the
    operator's own words."""
    _write(tmp_path, '[commands]\ngag = "staff"\nlook = "resident"\n'
                     'mailpurge = "owner"\n')
    assert _cli(tmp_path) == 0
    srv = _server(tmp_path)
    assert srv.state.command_rank("gag", T) == T.staff_rank
    assert srv.state.command_rank("look", T) == T.resident_rank
    assert srv.state.command_rank("mailpurge", T) == T.owner_rank


def test_conversion_speaks_the_HOUSE_S_words_not_commons_s(tmp_path):
    """The resolution is against THIS house's ladder — which is the reason the
    seed may not carry names of its own."""
    ladder = Theme(order=("newcomer", "citizen", "warden", "Steward"),
                   resident="citizen", staff="warden")
    theme_file = str(_theme_file(tmp_path, ladder)).replace("\\", "/")
    _write(tmp_path, '[commands]\ngag = "staff"\nlook = "resident"\nbans = "owner"\n')
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        _base_config_toml(tmp_path).replace(
            "[storage]", f'[storage]\ntheme_file = "{theme_file}"', 1),
        encoding="utf-8")
    from heresay.__main__ import write_permissions
    assert write_permissions(str(cfg_path)) == 0
    t = _table(tmp_path)
    assert (t["gag"], t["look"], t["bans"]) == ("warden", "citizen", "Steward")


def test_a_new_command_is_appended_as_a_NAME_too(tmp_path):
    """The top-up resolves like everything else, so a file cannot drift back
    into role words one release at a time."""
    _write(tmp_path, '[commands]\ngag = "staff"\n')
    assert _cli(tmp_path) == 0
    added = _table(tmp_path)
    assert added["gag"] == "staff"
    assert added["boot"] == "staff" and added["mailpurge"] == "owner"
    assert set(added.values()) <= set(T.order)      # all real ranks, no anchors


def test_the_in_memory_completion_MATCHES_the_written_file(tmp_path):
    """⚠ The daemon must behave identically whether or not the file has been
    re-written yet — otherwise the deploy window is a window of different
    permissions."""
    in_memory = dict(_server(tmp_path).state.commands)      # no file at all
    assert _cli(tmp_path) == 0
    assert _table(tmp_path) == in_memory


def test_write_permissions_REFUSES_a_file_it_cannot_make_sense_of(tmp_path):
    """⚠ This is the deploy's guard rail. A value that is not a rank makes the
    CLI exit non-zero, which aborts deploy.sh BEFORE the restart — so a bad file
    is met with the old code still serving, not as a house that will not open."""
    _write(tmp_path, '[commands]\ngag = "wardn"\n')
    before = _perm_path(tmp_path).read_bytes()
    assert _cli(tmp_path) == 1
    assert _perm_path(tmp_path).read_bytes() == before       # nothing written


def test_an_anchor_that_survived_the_deploy_is_boot_fatal(tmp_path):
    """⛔ NO BELT. If `--write-permissions` somehow did not run, the daemon does
    not quietly resolve the leftover — it refuses, and the operator fixes the
    file. That is the whole posture: no resolve-with-warning, no degrade path."""
    # ⚠ `newb`, not `staff`: the anchor word that can NEVER also be a rank. It
    # is derived as `order[0]` and is not configurable, where `staff` is a legal
    # rung on some ladders — including the generic one — so a leftover `staff`
    # resolves there and this test would pass for the wrong reason.
    _write(tmp_path, '[commands]\ngag = "newb"\n')
    with pytest.raises(permissions.PermissionsError):
        _server(tmp_path)


def test_commons_after_conversion_is_resident_keeper_caretaker(tmp_path):
    """The shipped ladder, end to end: what the operator will actually read."""
    assert _cli(tmp_path) == 0
    values = set(_table(tmp_path).values())
    assert values == {"resident", "staff", "owner"}


# ---------------------------------------------------------------------------
# ⛔ `disabled` — a command that is not a command (v1.56.0)
#
# The semantics, and every test below is one clause of it: a disabled command
# DOES NOT EXIST in that house. Not hidden, not refused, not permission-denied.
# Typing it is a typo; it is absent from `.help`; no permission check runs; and
# it binds EVERYONE, the owner included.
# ---------------------------------------------------------------------------

def _owner(srv, name="Generic"):
    """A session at the OWNER level — the one a rank could never stop."""
    return FakeSession(srv.world, name=name, state=srv.state,
                       config=srv.config, level="arch")


def test_a_disabled_command_answers_EXACTLY_as_gibberish_does(tmp_path):
    """⛔ Byte-identical, and asserted against the REAL gibberish path rather
    than a hand-copied string — a wording that drifted would otherwise pass here
    and advertise, in production, that the house has something it has hidden."""
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\nreboot = "disabled"\n')
    assert _tick(srv)[0].ok

    res = _connected_resident(srv)
    dispatch(res, ".reboot")
    disabled_reply = res.text

    res.clear()
    dispatch(res, ".reboot")            # same verb, for the message text
    got = res.text
    res.clear()
    dispatch(res, ".qwertyuiop")        # a verb that has never existed
    gibberish = res.text

    # The only difference between the two lines is the verb quoted back, which
    # is exactly what a typo gets.
    assert got == C.unknown_command("reboot")
    assert gibberish == C.unknown_command("qwertyuiop")
    assert got.replace("reboot", "X") == gibberish.replace("qwertyuiop", "X")
    assert disabled_reply == got


def test_a_disabled_command_binds_THE_OWNER_TOO(tmp_path):
    """⛔ The line between a disable and a rank. A value that let the owner
    through would be a rank — "reserved to me" — and this is not that."""
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\nreboot = "disabled"\n')
    assert _tick(srv)[0].ok

    boss = _owner(srv)
    assert boss.resident.rank == T.owner_rank        # really the top of the ladder
    dispatch(boss, ".reboot")
    assert boss.text == C.unknown_command("reboot")

    # ⚡ THE CONTRAST, which is what makes this a distinction rather than a
    # restatement: the nearest thing the old model could express — pin it to the
    # TOP rank — does not stop the owner, and never could. That is exactly why
    # `disabled` had to be a value outside the ladder rather than a rank on it.
    _write(tmp_path, f'[commands]\nreboot = "{T.owner_rank}"\n')
    assert _tick(srv)[0].ok
    boss.clear()
    dispatch(boss, ".reboot")
    assert "Unknown command" not in boss.text


def test_a_disabled_command_is_ABSENT_from_the_help_screen(tmp_path):
    """Absent, not greyed and not footnoted: the house does not advertise what
    it does not have. Checked at the OWNER's screen, which shows everything."""
    srv = _server(tmp_path)
    before = C.help_text(T, "long", T.owner_rank, 78, srv.state)
    assert ".reboot" in before

    _write(tmp_path, '[commands]\nreboot = "disabled"\n')
    assert _tick(srv)[0].ok
    after = C.help_text(T, "long", T.owner_rank, 78, srv.state)
    assert ".reboot" not in after
    # ...and it is gone from the SHORT index too — both styles read one plan.
    assert ".reboot" not in C.help_text(T, "short", T.owner_rank, 78, srv.state)
    # Nothing else moved.
    assert ".who" in after and ".help" in after


def test_the_help_PAGE_is_still_shipped_so_re_enabling_is_a_config_change(tmp_path):
    """⚠ The page stays on disk; it is simply not listed or reachable. Turning
    the command back on is one line in permissions.toml — never a content
    deploy, which on a real house is a different action with a different risk."""
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\nreboot = "disabled"\n')
    assert _tick(srv)[0].ok
    page = Path(__file__).resolve().parent / "fixtures" / "house" / "help" / "reboot.txt"
    assert page.exists(), "the authored page must survive the command"
    # ...and it is not REACHABLE while disabled: `.help reboot` gates on the
    # same `_visible` the dispatcher does, so it answers as a typo.
    res = _connected_resident(srv)
    dispatch(res, ".help reboot")
    assert res.text == C.unknown_command("reboot")


def test_an_ALIAS_of_a_disabled_command_is_dead_too(tmp_path):
    """The gate keys on the CANONICAL name, so an alias dies with its command
    exactly as it inherits its rank. `.ex` is `.examine`'s."""
    srv = _server(tmp_path)
    assert "ex" in C.CANONICAL and C.CANONICAL["ex"] == "examine"
    _write(tmp_path, '[commands]\nexamine = "disabled"\n')
    assert _tick(srv)[0].ok

    res = _connected_resident(srv)
    dispatch(res, ".ex")
    assert res.text == C.unknown_command("ex")


def test_disabling_a_LOAD_BEARING_command_simply_works(tmp_path):
    """⛔ THE ABSENCE OF A PROTECTED LIST, pinned so nobody adds one back later
    believing it was an oversight. The maintainer's explicit call: *"no matter how
    stupid means just that."* `.passwd` is the sharpest case — a house that
    disables it has no way to change a password — and it is the operator's house.

    The escape hatch is structural, not a guard list: this file is root-owned on
    the server and the daemon never writes it, so nothing done in-world can lock
    anybody out of undoing it."""
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\npasswd = "disabled"\nquit = "disabled"\n')
    result = _tick(srv)[0]
    assert result.ok, "no refusal, no warning, no protected set"

    res = _connected_resident(srv)
    dispatch(res, ".passwd")
    assert res.text == C.unknown_command("passwd")
    res.clear()
    dispatch(res, ".quit")
    assert res.text == C.unknown_command("quit")


def test_disabling_and_RE_ENABLING_both_take_effect_on_a_live_reload(tmp_path):
    """Both directions, on an already-connected session, with no restart."""
    srv = _server(tmp_path)
    res = _connected_resident(srv)

    dispatch(res, ".reboot")                       # enabled at the default
    assert "Unknown command" not in res.text

    _write(tmp_path, '[commands]\nreboot = "disabled"\n')
    assert _tick(srv)[0].ok
    res.clear(); dispatch(res, ".reboot")
    assert res.text == C.unknown_command("reboot")   # off, live

    _write(tmp_path, '[commands]\nreboot = "resident"\n')
    assert _tick(srv)[0].ok
    res.clear(); dispatch(res, ".reboot")
    assert "Unknown command" not in res.text         # ...and back, live


def test_a_rank_NAMED_disabled_refuses_the_boot(tmp_path):
    """⛔ Boot-fatal, and not a fallback. A rank of that name would make every
    permissions line ambiguous between "this rank and above" and "no such
    command" — opposite answers, and a wrong guess is a SILENT permission change
    on a gate that is invisible by design."""
    from heresay.theme import ThemeError
    bad = tmp_path / "theme.toml"
    bad.write_text('[ranks]\norder = ["resident", "disabled", "owner"]\n',
                   encoding="utf-8")
    with pytest.raises(ThemeError) as exc:
        _server(tmp_path, theme_file=str(bad))
    msg = str(exc.value)
    assert "disabled" in msg and "rank" in msg.lower()
    assert "Rename the rank" in msg


def test_the_value_survives_the_deploy_conversion(tmp_path):
    """⚠ `--write-permissions` rewrites role words as rank names — it converted
    39 values on the last deploy — and `disabled` must come through untouched.
    It is not an anchor, so the canonicalizer does not match it; this pins that
    rather than trusting it."""
    _write(tmp_path, '[commands]\nreboot = "disabled"\ngag = "staff"\n')
    assert _cli(tmp_path) == 0
    table = _table(tmp_path)
    assert table["reboot"] == "disabled"      # untouched
    assert table["gag"] == "staff"           # the anchor WAS converted
    # Idempotent, exactly as before: a second pass changes nothing.
    before = _perm_path(tmp_path).read_bytes()
    assert _cli(tmp_path) == 0
    assert _perm_path(tmp_path).read_bytes() == before


def test_a_disabled_value_is_accepted_by_the_strict_parse():
    """The parse is the strict one — a value that is not a rank refuses the
    boot — so `disabled` has to be legal there explicitly."""
    p = permissions.parse('[commands]\nreboot = "disabled"\n',
                          command_seed(), ladder=T)
    assert p.table["reboot"] == "disabled"
    # ...and a near-miss is still refused, so this is a sentinel and not a hole.
    with pytest.raises(permissions.PermissionsError):
        permissions.parse('[commands]\nreboot = "disable"\n',
                          command_seed(), ladder=T)


def test_the_refusal_message_names_disabled_as_the_other_legal_value(tmp_path):
    """An operator who mistypes a rank should be told the whole truth about what
    this field takes, not half of it."""
    with pytest.raises(permissions.PermissionsError) as exc:
        permissions.parse('[commands]\ngag = "wardn"\n', command_seed(), ladder=T)
    assert "disabled" in str(exc.value)


def test_may_run_agrees_with_the_dispatcher_about_a_disabled_command(tmp_path):
    """⚠ `may_run` is the dispatcher's gate under a public name — the report
    box asks it in two places outside dispatch. If it disagreed, the house would
    tell somebody about a command it does not have."""
    srv = _server(tmp_path)
    _write(tmp_path, '[commands]\nread_report = "disabled"\n')
    assert _tick(srv)[0].ok
    boss = _owner(srv)
    assert C.may_run("read_report", boss) is False


def test_the_seed_side_fallback_honours_disabled_too(monkeypatch):
    """⚠ The branch `_visible` takes when there is no table at all — a pure
    unit test, a session before state is wired, a store built without a seed.

    It is a FAIL-SAFE rather than a shipped configuration: nothing in the code
    seed is disabled today (the generic distribution ships the value in
    `skel/permissions.toml`, not in the registry). Without this branch a seeded
    `disabled` would fall through `resolve_anchor` and read as OWNER-ONLY, which
    is the one answer a disable must never give."""
    monkeypatch.setattr(C, "command_seed",
                        lambda: {**command_seed(), "reboot": permissions.DISABLED})
    assert C._visible("reboot", T.owner_rank, T, None) is False
    assert C._visible("who", T.owner_rank, T, None) is True


# ---------------------------------------------------------------------------
# The writer inserts INTO the file (v1.60.1)
# ---------------------------------------------------------------------------
#
# ⛔ What replaced `append_lines`, and why it is worth this many tests: the old
# writer appended new commands at the END of the file under a banner. That
# accreted one banner per release, and when the operator later wrote the same
# key in its proper place the file held it TWICE — a TOML duplicate-key error,
# which refuses the parse, which aborts the deploy. It did exactly that on
# v1.60.0. A writer that can produce a file its own reader rejects is not one
# you can run unattended, which is what `deploy.sh` does with it.

_CATS = {"ownerset": "administration", "load_report": "administration",
         "logins": "administration", "staff_actions": "administration",
         "gag": "moderation", "boot": "moderation", "report": "mail"}


def test_a_new_command_lands_in_its_own_section_with_no_banner():
    text = ('[commands]\n\n# administration\n'
            'ownerset    = "owner"\n'
            'load_report = "owner"\n')
    out = permissions.top_up(text, ["staff_actions"],
                             {"staff_actions": "owner"}, _CATS)
    assert "# Added" not in out and "Heresay" not in out
    lines = out.splitlines()
    at = lines.index("# administration")
    # In the section, after the ones already there.
    assert lines[at + 1:at + 4] == [
        'ownerset      = "owner"',
        'load_report   = "owner"',
        'staff_actions = "owner"',
    ]


def test_the_WHOLE_section_re_aligns_to_its_longest_key():
    """⚡ The column belongs to the SECTION, not to the line that happened to
    arrive with it. Adding a longer key therefore moves every `=` in that
    section — which is right: a column only some lines respect reads worse than
    no column, and the file is meant to look hand-written."""
    text = '[commands]\n\n# moderation\ngag  = "staff"\nboot = "staff"\n'
    out = permissions.top_up(text, ["staff_actions"],
                             {"staff_actions": "owner"},
                             {**_CATS, "staff_actions": "moderation"})
    for line in out.splitlines():
        if "=" in line and not line.startswith("["):
            assert line.index("=") == len("staff_actions") + 1, line


def test_old_banner_blocks_are_stripped_and_their_commands_re_homed():
    """⚠ Stripping the label and leaving the lines stranded under no heading
    would tidy the symptom and keep the mess, so the commands the old writer
    appended go where they belong — values untouched."""
    text = ('[commands]\n\n# administration\nownerset = "owner"\n\n'
            '# Added by Heresay 1.58.0 — new command(s), at their built-in defaults.\n'
            '# Change the value to move one; the line stays either way.\n'
            'report = "resident"\n')
    out = permissions.top_up(text, (), {}, _CATS)
    assert "Added by Heresay" not in out
    assert "# mail" in out
    assert out.splitlines().index('report = "resident"') > out.splitlines().index("# mail")


def test_a_DUPLICATED_KEY_is_de_duplicated_rather_than_left_to_fail_the_parse():
    """⛔ The v1.60.0 deploy failure, as one assertion. Two `ownerset` lines is
    `Cannot overwrite a value` from tomllib — the file cannot be READ, so the
    fix has to be a line rewrite that runs BEFORE anything parses it."""
    text = ('[commands]\n\n# administration\nownerset = "owner"\n\n'
            '# Added by Heresay 1.54.0 — new command(s), at their built-in defaults.\n'
            '# Change the value to move one; the line stays either way.\n'
            'ownerset = "owner"\n')
    with pytest.raises(Exception):               # tomllib refuses it as it is
        permissions.parse(text, command_seed(), ladder=DEFAULT_THEME)
    out = permissions.top_up(text, (), {}, _CATS)
    assert out.count("ownerset") == 1
    permissions.parse(out, command_seed(), ladder=COMMONS_THEME)   # parses now


def test_an_operators_own_VALUES_survive_the_rewrite_untouched():
    """⛔ The writer moves lines and sets spacing. It has no opinion about what
    a line SAYS — this is somebody's configuration, and the restraint is the
    point."""
    text = ('[commands]\n\n# moderation\n'
            'gag = "High Staff"        # our own rung\n'
            'boot = "disabled"\n')
    out = permissions.top_up(text, ["staff_actions"],
                             {"staff_actions": "owner"}, _CATS)
    assert 'gag  = "High Staff"        # our own rung' in out
    assert 'boot = "disabled"' in out


def test_the_top_up_is_idempotent(tmp_path):
    """Run twice, byte for byte — or `deploy.sh` re-flows the file every push
    and the diff is noise nobody reads."""
    from heresay.commands.registry import BY_NAME
    from heresay.__main__ import write_permissions          # noqa: F401 (import guard)
    cats = {n: c.category for n, c in BY_NAME.items()}
    p = tmp_path / "permissions.toml"
    p.write_text('[commands]\n\n# system\nreboot = "disabled"\n', encoding="utf-8")
    ladder = Theme(order=("resident", "staff", "owner"), staff="staff")
    action, added, _ = permissions.write_file(p, command_seed(), categories=cats,
                                              version="1.60.1", ladder=ladder)
    assert action == "appended" and added
    once = p.read_text(encoding="utf-8")
    assert "Added by Heresay" not in once
    action2, added2, _ = permissions.write_file(p, command_seed(), categories=cats,
                                                version="1.60.1", ladder=ladder)
    assert (action2, added2) == ("unchanged", [])
    assert p.read_text(encoding="utf-8") == once


def test_the_shipped_skeleton_is_already_in_the_writers_own_format():
    """⚡ What a fresh install finds on disk and what the writer produces must
    be the same shape, or the first deploy churns a file nobody edited."""
    # ⚠ `skel/permissions.toml` here; `permissions.toml` at the root in the built
    # tree — the skeleton IS the install there, and `skel/` is a build source
    # that does not ship. The test asks the tree which path it has.
    root = Path(__file__).resolve().parent.parent
    skel = root / "skel" / "permissions.toml"
    if not skel.exists():
        skel = root / "permissions.toml"
    text = skel.read_text(encoding="utf-8")
    assert "Added by Heresay" not in text
    assert "\n# system\nreboot = \"disabled\"" in text
    assert permissions.top_up(text, (), {}, {"reboot": "system"}) == text
