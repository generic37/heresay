# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The deploy kit, checked as text — because it is code that only ever runs once
a release, on the one machine that matters, in front of nobody.

⚠ **Why this file exists.** v1.37.1 shipped a `deploy.sh` step that ran
`python -m heresay` over SSH **without a working directory**, and the deploy
died on `No module named heresay`. The engine's own suite could not have caught
it: the failure was in a shell line, on a host, in a directory none of the tests
have ever been in. So the shell lines get read here instead.

Nothing runs the scripts — these are assertions about their TEXT. That is a
weaker guarantee than executing them, and it is the right weaker guarantee: what
went wrong was not logic, it was a missing `cd`.
"""

from pathlib import Path

import re

import pytest

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "deploy.sh"
DEPLOY_ALERTS = REPO / "deploy" / "deploy-alerts.sh"
CERT_COPY = REPO / "deploy" / "bin" / "heresay-cert-copy.sh"
UNIT = REPO / "deploy" / "install" / "heresay.service"
INSTALL_README = REPO / "deploy" / "install" / "README.md"
CONFIGURING = REPO / "docs" / "CONFIGURING.md"

# Where the deployed code lives, and therefore the only directory from which
# `-m heresay` resolves — the package is NOT installed into the venv (only its
# three dependencies are), deliberately: what is deployed is the code, not a
# copy of it.
OPT = "/opt/heresay"


def _lines(path):
    return path.read_text(encoding="utf-8").split("\n")


def _find_all(text, needle):
    at = text.find(needle)
    while at != -1:
        yield at
        at = text.find(needle, at + 1)


def test_the_unit_sets_the_working_directory():
    """The reason the DAEMON's `-m heresay` resolves. Everything else that runs
    the module on that host has to reproduce this."""
    text = UNIT.read_text(encoding="utf-8")
    assert f"WorkingDirectory={OPT}" in text
    assert f"ExecStart={OPT}/.venv/bin/python -m heresay" in text


def test_the_venv_installs_the_dependencies_and_not_the_package():
    """The fact the working-directory rule follows from. If this ever changes —
    `pip install -e .` in the deploy — the rule can relax, and this test is
    where somebody will find that out.

    ⚠ The install line itself moved in v1.48.0 (audit G2): it names a pinned,
    hashed requirements file rather than three bare package names. What this
    test is really about is unchanged — the deploy installs DEPENDENCIES, never
    the package — so the assertion moved with it rather than being dropped."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.txt" in text
    assert "pip install -e" not in text
    assert "pip install ." not in text


def _module_invocations(path):
    """Every line that runs `-m heresay`, excluding comments."""
    return [ln for ln in _lines(path)
            if "-m heresay" in ln and not ln.lstrip().startswith("#")]


def test_every_remote_module_run_has_a_working_directory():
    """⛔ THE v1.37.1 DEPLOY FAILURE, as a test.

    `ssh host '<cmd>'` starts in the login user's HOME, not in the deployed
    tree, so a bare `-m heresay` cannot find the package. Any line in deploy.sh
    that runs the module must `cd /opt/heresay` first (or set PYTHONPATH to it).
    """
    runs = _module_invocations(DEPLOY)
    assert runs, "deploy.sh no longer runs the module — delete this test if so"
    for line in runs:
        assert f"cd {OPT}" in line or f"PYTHONPATH={OPT}" in line, line


def test_the_permissions_step_matches_the_unit():
    """Same interpreter, same working directory, same config — the CLI and the
    daemon must not disagree about which house they are talking about."""
    line = next(l for l in _module_invocations(DEPLOY)
                if "--write-permissions" in l)
    assert f"cd {OPT}" in line
    assert f"{OPT}/.venv/bin/python -m heresay" in line
    assert "-c /etc/heresay/config.toml" in line


def test_the_permissions_step_runs_before_the_restart():
    """It exists to have the file ready for the boot that follows. It is also
    what kept the v1.37.1 failure harmless: the step aborted the deploy before
    anything touched the running service, so the house stayed up."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert text.index("--write-permissions") < text.index("systemctl restart")


def test_a_failed_permissions_step_ABORTS_the_deploy():
    """⛔ v1.43.0, and it is the guard rail on the whole names-only rule. A value
    that is not a rank refuses the BOOT, so the file has to be caught here — at
    deploy, with the old code still serving — rather than at the restart, as a
    house that will not open.

    Two mechanisms, and both are asserted because they do different jobs:
    `set -e` is what actually stops the script, and the explicit check is what
    tells the operator WHY. "deploy stopped" and "your permissions file is
    wrong" are not the same message to wake up to.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text

    step = text.index("--write-permissions\n") if "--write-permissions\n" in text \
        else text.index("--write-permissions")
    restart = text.index("systemctl restart")
    between = text[step:restart]
    assert "exit 1" in between, "a failed --write-permissions must stop the deploy"
    assert "Nothing has been restarted" in between
    # The guarded form, so the failure is reported rather than merely fatal.
    assert "if ! ssh" in text


@pytest.mark.parametrize("doc", [INSTALL_README, CONFIGURING, DEPLOY])
def test_the_by_hand_command_is_runnable_as_documented(doc):
    """An operator copying the command out of the docs must not hit the wall the
    deploy hit. The documented form wraps across a continuation line, so the
    block is checked rather than one line."""
    blocks = [b for b in doc.read_text(encoding="utf-8").split("\n\n")
              if "--write-permissions" in b and "-m heresay" in b]
    assert blocks, f"{doc.name} no longer shows the command"
    for b in blocks:
        assert f"cd {OPT} &&" in b, (doc.name, b)
        assert "-c /etc/heresay/config.toml" in b, (doc.name, b)


# ---------------------------------------------------------------------------
# The graceful countdown (v1.46.0)
# ---------------------------------------------------------------------------

UNIT = REPO / "deploy" / "install" / "heresay.service"


def test_the_deploy_order_is_rsync_permissions_countdown_restart():
    """⚠ THE ORDER IS THE POINT. Warning the house and THEN discovering the
    permissions file is broken would have announced a restart that never came —
    so the abort-on-failure step sits between the upload and the countdown."""
    text = DEPLOY.read_text(encoding="utf-8")
    order = [text.index(needle) for needle in (
        "rsync -az --delete",
        "--write-permissions",
        "--restart-countdown",
        "systemctl restart",
    )]
    assert order == sorted(order), order


def test_the_countdown_step_matches_the_unit_like_the_permissions_one():
    """Same interpreter, same working directory, same config — otherwise it is
    talking to a different house, or to nothing (the v1.37.2 failure)."""
    line = next(l for l in _module_invocations(DEPLOY)
                if "--restart-countdown" in l)
    assert f"cd {OPT}" in line
    assert f"{OPT}/.venv/bin/python -m heresay" in line
    assert "-c /etc/heresay/config.toml" in line


def test_a_missing_control_socket_does_NOT_abort_the_deploy():
    """⚠ The opposite call from --write-permissions, deliberately. A bad
    permissions file means the next boot fails, so it must stop the deploy; a
    daemon too old to have a control socket just means nobody gets a warning,
    and refusing to deploy over that would be worse than a quiet restart."""
    text = DEPLOY.read_text(encoding="utf-8")
    start = text.index("--restart-countdown")
    end = text.index("systemctl restart")
    between = text[start:end]
    assert "||" in between and "restarting anyway" in between
    assert "exit 1" not in between


# ---- cert-copy restarts gracefully (audit-3 F5) ---------------------------
# The daily cert-copy used to `systemctl restart heresay` the instant a renewed
# cert landed — kicking everyone with no warning. v1.53.3 makes it announce and
# count down first, reusing the SAME `--restart-countdown` mechanism deploy.sh
# uses (no new engine code). These pin the order and the tolerance.

def test_cert_copy_counts_down_before_it_restarts():
    """⛔ The graceful order: the v1.46 countdown must fire BEFORE the restart,
    or the countdown warns about a restart that already happened."""
    text = CERT_COPY.read_text(encoding="utf-8")
    assert "--restart-countdown" in text, "cert-copy no longer runs the countdown"
    assert "systemctl restart heresay" in text
    assert text.index("--restart-countdown") < text.index("systemctl restart heresay")


def test_cert_copy_countdown_matches_the_deploy_mechanism():
    """It reuses deploy.sh's exact invocation — same interpreter, working
    directory and config — so it talks to the running daemon, not to nothing
    (the v1.37.2 failure shape)."""
    text = CERT_COPY.read_text(encoding="utf-8")
    assert f"cd {OPT}" in text
    assert f"{OPT}/.venv/bin/python -m heresay" in text
    assert "-c /etc/heresay/config.toml" in text


def test_cert_copy_restarts_anyway_if_the_countdown_cannot_run():
    """⚠ Same tolerance as deploy.sh: a missing/old control socket must not skip
    the cert swap — the countdown falls through (`|| …`) and the restart still
    lands. A cert that fails to load is a worse outcome than a missed warning."""
    text = CERT_COPY.read_text(encoding="utf-8")
    # the countdown line ends in a `|| echo …` fallthrough, and the restart is a
    # separate, unconditional statement after it.
    cd = text.index("--restart-countdown")
    restart = text.index("systemctl restart heresay")
    between = text[cd:restart]
    assert "||" in between


# ---- make deploy-alerts (refresh the /usr/local/bin alert scripts) --------
# v1.53.2. The alert scripts live OUTSIDE the /opt/heresay rsync target, so
# `make deploy` never carries them; this target is the one that does. Same host
# machinery as deploy/deploy-content, and alerts-only — a wrong turn here (a
# daemon restart, a write under /opt/heresay) would defeat the decoupling.

def test_the_make_deploy_alerts_target_runs_the_script():
    """The Makefile target exists and invokes the script, like `deploy` and
    `deploy-content` invoke theirs — the host machinery lives in the script."""
    mk = MAKEFILE.read_text(encoding="utf-8")
    assert "deploy-alerts:" in mk
    assert "bash deploy/deploy-alerts.sh" in mk


def test_deploy_alerts_resolves_the_host_like_the_other_deploy_scripts():
    """DEPLOY_HOST from the env, else deploy/local.env, else refuse — the same
    resolution deploy.sh / deploy-content.sh use. No built-in default host."""
    text = DEPLOY_ALERTS.read_text(encoding="utf-8")
    assert "DEPLOY_HOST" in text
    assert "deploy/local.env" in text
    assert "Refusing to guess" in text or "is not set" in text


def test_deploy_alerts_installs_the_usr_local_bin_set_at_0755():
    """It installs exactly the five scripts that RUN FROM /usr/local/bin, at
    mode 0755, root-owned — the same set and mode the install README first
    installs there."""
    text = DEPLOY_ALERTS.read_text(encoding="utf-8")
    for f in ("heresay-notify.sh", "heresay-hcping.sh",
              "heresay-ceiling-watch.sh", "heresay-report-watch.sh",
              "heresay-healthcheck.sh"):
        assert f in text, f
    assert "install -m 0755 -o root -g root" in text
    assert "/usr/local/bin/" in text
    # The run-in-place scripts are NOT in this set — a normal deploy carries them.
    assert "heresay-backup.sh" not in text
    assert "heresay-depscan.sh" not in text


def test_deploy_alerts_is_ALERTS_ONLY():
    """⛔ The decoupling, as an assertion. It must not restart the daemon, run
    --write-permissions, or write anything under /opt/heresay or
    /var/lib/heresay. ⚠ Checked against the EXECUTABLE lines only — the comments
    legitimately NAME these to explain what the script does not do, exactly like
    the unit test strips `#` before reading directives. A `systemctl start
    heresay-healthcheck.service` HINT in a printed suggestion is fine: that
    starts the monitor oneshot, not the daemon."""
    code = "\n".join(l for l in DEPLOY_ALERTS.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    assert "systemctl restart heresay" not in code
    assert "--write-permissions" not in code
    assert "/opt/heresay" not in code
    assert "/var/lib/heresay" not in code
    assert "rsync -az --delete" not in code        # not an app/content rsync


def _alerts_unit_list():
    """The UNITS=( ... ) array from deploy-alerts.sh, as a list of filenames."""
    text = DEPLOY_ALERTS.read_text(encoding="utf-8")
    body = text.split("UNITS=(", 1)[1].split(")", 1)[0]
    return [l.strip() for l in body.splitlines() if l.strip()]


def test_deploy_alerts_never_ships_the_daemons_own_unit():
    """⛔ THE ONE MISTAKE THIS SCRIPT MUST NOT BE ABLE TO MAKE (v1.62.23).

    The alerts refresh ships systemd units now. `deploy/install/` also holds
    `heresay.service` — the daemon's own — and a silent overwrite of that from
    an alerts run is exactly the wrong path for it: a change there needs the
    graceful countdown `make deploy` performs, so that whoever is in the house
    is warned before the socket goes.

    ⚠ Three assertions, because the property has three ways to break: the unit
    named in the list; the list replaced by a glob that sweeps it up; the
    script's own refusal deleted.
    """
    units = _alerts_unit_list()
    assert units, "UNITS=( ) not found — did the array move?"
    assert "heresay.service" not in units, \
        "the daemon's unit must never ship from the alerts deploy — use make deploy"

    code = "\n".join(l for l in DEPLOY_ALERTS.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    # ⛔ A glob is how heresay.service gets in without anybody typing it.
    assert "install/*.service" not in code
    assert "install/*.timer" not in code
    # ...and the script refuses at runtime too, so a hand-edit fails loudly.
    assert "heresay.service)" in code


def test_deploy_alerts_restarts_only_the_two_long_running_watchers():
    """⚠ The restart set, asserted because "restart nothing that does not need
    it" is invisible once written.

    ceiling-watch and report-watch FOLLOW the event log and hold their script in
    memory, so a refreshed script does nothing for them until they restart.
    Everything else picks its new definition up on its own: timers from the
    daemon-reload, `notify@` on its next instantiation.

    ⛔ And it must never `enable` or `start`: a stopped watcher stays stopped,
    because enablement is an install-time decision (heresay-report-watch's
    manual enable is deliberate — docs/OPERATIONS.md).
    """
    code = "\n".join(l for l in DEPLOY_ALERTS.read_text(encoding="utf-8").splitlines()
                     if not l.lstrip().startswith("#"))
    assert "heresay-ceiling-watch:" in code and "heresay-report-watch:" in code
    assert "is-active --quiet" in code            # only a running watcher is restarted
    assert "daemon-reload" in code
    assert "systemctl enable" not in code
    assert "--now" not in code
    # ⛔ No timer is restarted: the reload is the whole mechanism for those.
    assert "restart heresay-healthcheck" not in code
    assert "restart heresay-depscan" not in code


def test_the_unit_will_not_revive_a_clean_exit():
    """⛔ `Restart=on-failure`, not `always` — the whole of "stays down". The
    timed shutdown ends with exit 0; under `always` systemd would bring the
    house straight back and the command could not do its one job."""
    # ⚠ Directives only — the comment above it EXPLAINS `Restart=always`, so a
    # whole-file grep would be reading the reasoning as if it were the setting.
    directives = [l for l in UNIT.read_text(encoding="utf-8").splitlines()
                  if l and not l.startswith("#")]
    assert "Restart=on-failure" in directives
    assert not [l for l in directives if l.startswith("Restart=always")]


def test_the_unit_waits_longer_than_the_longest_countdown():
    """A stop landing mid-countdown must be waited for, not SIGKILLed. The
    default 90s is under the shipped 120s deploy countdown."""
    from heresay.config import Config
    unit = UNIT.read_text(encoding="utf-8")
    line = next(l for l in unit.splitlines() if l.startswith("TimeoutStopSec="))
    assert int(line.split("=", 1)[1]) > max(Config().restart_checkpoints)


def test_the_unit_allows_the_UNIX_socket_family():
    """⚠ Without AF_UNIX the bind fails at startup — the daemon still runs, but
    the graceful countdown silently does not, which is the worst of both."""
    unit = UNIT.read_text(encoding="utf-8")
    line = next(l for l in unit.splitlines()
                if l.startswith("RestrictAddressFamilies="))
    assert "AF_UNIX" in line
    # ...and it is still restrictive: no raw packets, no netlink.
    assert set(line.split("=", 1)[1].split()) == {"AF_UNIX", "AF_INET", "AF_INET6"}


# ===========================================================================
# The backup, and what it must NOT carry (v1.48.0, audit E1/E2)
# ===========================================================================

BACKUP = REPO / "deploy" / "bin" / "heresay-backup.sh"
FETCH = REPO / "scripts" / "fetch-backup-cron.sh"


def test_the_nightly_tarball_EXCLUDES_the_mail_key():
    """⛔ The finding, as one assertion.

    Mail and `.revtell` are encrypted with `mail.key`. Tarring the whole state
    dir put the key in the same file as the ciphertext — and that file is the
    one that leaves the machine, weekly, to a second host. Encryption that
    travels with its key is a filename, not a protection.
    """
    text = BACKUP.read_text(encoding="utf-8")
    assert "--exclude='mail.key'" in text
    assert "--exclude='*/mail.key'" in text
    # The tar call and the excludes are one command, not two hopeful lines:
    # both excludes sit between `tar -czf` and the sources it is given.
    tar_at = text.index("tar -czf")
    srcs_at = text.index('"${SRCS[@]}"', tar_at)
    assert tar_at < text.index("--exclude='mail.key'") < srcs_at
    assert tar_at < text.index("--exclude='*/mail.key'") < srcs_at


def test_the_backup_COVERS_THE_WHOLE_INSTANCE_not_just_the_state_dir():
    """⛔ Git holds the code; the backup holds the instance; nothing is in both.

    So the tarball has to carry everything that makes this server THIS house
    rather than a fresh checkout — the state, the /etc configuration (including
    `notify.env`, whose two secrets exist nowhere else) and the authored house.
    Before v1.60.0 it took `/var/lib/heresay` alone, which meant a rebuild
    restored the accounts and lost the rooms, the config and the alerting.
    """
    text = BACKUP.read_text(encoding="utf-8")
    for src in ("/var/lib/heresay", "/etc/heresay", "/opt/heresay/house"):
        assert f'"{src}"' in text, src
    # ⚠ And NOT the rest of /opt — that is the code, and git has it.
    assert '"/opt/heresay"' not in text


def test_a_missing_SOURCE_refuses_rather_than_writing_a_partial_backup():
    """⚠ A tarball missing one of the three is the dangerous outcome, because it
    looks exactly like a good one: same name, same place, restores cleanly, and
    is short by whichever tree was not mounted. The script names what it could
    not find and exits non-zero — which, running under the timer, surfaces as a
    failed unit and (through `OnFailure=`) a push."""
    text = BACKUP.read_text(encoding="utf-8")
    assert "refusing a partial instance backup" in text
    assert "MISSING+=" in text
    # The check runs BEFORE tar, or it would be reporting on a file it wrote.
    assert text.index("MISSING+=") < text.index("tar -czf")


def test_the_backup_VERIFIES_the_key_is_absent_rather_than_trusting_the_exclude():
    """⚠ A belt, because being wrong here is silent and lasts as long as the
    backup does: a mistyped exclude would ship the key for a year before anyone
    looked. The script reads its own tarball back and refuses to keep one with a
    key in it."""
    text = BACKUP.read_text(encoding="utf-8")
    assert "tar -tzf" in text
    assert "refusing to keep it" in text
    assert "rm -f \"$OUT\"" in text


def test_the_operator_is_told_to_copy_the_key_ONCE_by_hand():
    """The key never rotates, so it wants one careful copy rather than a nightly
    one — and until that copy exists it lives in exactly one place. The step has
    to be written down where an operator setting up will meet it."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "one-time `mail.key` backup" in readme.lower() or \
           "The one-time `mail.key` backup" in readme
    assert "/var/lib/heresay/mail.key" in readme
    # And the consequence of not doing it.
    assert "exists in exactly one place" in readme


def test_the_restore_docs_say_it_now_takes_TWO_inputs():
    """⚠ The half that is easy to leave out. A restore from the tarball alone
    succeeds — and comes back with every account intact and not one readable
    message. A silent failure has to be documented loudly."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "TWO inputs" in readme
    assert "not one readable message" in readme
    # The scripts' own headers must not still claim the tarball holds the key.
    for path in (BACKUP, FETCH):
        text = path.read_text(encoding="utf-8")
        assert "the key that decrypts" not in text or "do NOT hold" in text or \
               "DOES NOT CONTAIN" in text, path


def test_the_pull_host_backup_directory_is_not_world_listable():
    """E2: the tarballs were 0600, but their directory was 0755 — and a listing
    of backup filenames is a map of what is worth taking."""
    text = FETCH.read_text(encoding="utf-8")
    assert "mkdir -m 700" in text
    assert 'chmod 700 "$DEST"' in text


# ===========================================================================
# Pinned, hashed dependencies (v1.48.0, audit G1/G2)
# ===========================================================================

REQS = REPO / "requirements.txt"
REQS_DEV = REPO / "requirements-dev.txt"
MAKEFILE = REPO / "Makefile"
RUNTIME_DEPS = ("argon2-cffi", "cryptography", "websockets")


def _pins(path):
    """name -> version, for every `name==version` line."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or "==" not in line:
            continue
        name, _, rest = line.partition("==")
        out[name.strip().lower()] = rest.split()[0].strip().rstrip("\\").strip()
    return out


def test_requirements_pins_every_runtime_dependency_with_a_version():
    pins = _pins(REQS)
    for dep in RUNTIME_DEPS:
        assert dep in pins, f"{dep} is not pinned in requirements.txt"
        assert pins[dep][0].isdigit(), f"{dep} pin is not a version: {pins[dep]!r}"


def test_requirements_carries_the_TRANSITIVE_closure_too():
    """⚠ Not tidiness: `--require-hashes` is all-or-nothing, so a single missing
    transitive hash fails the install outright. The closure is what makes the
    flag usable at all."""
    pins = _pins(REQS)
    for dep in ("argon2-cffi-bindings", "cffi", "pycparser"):
        assert dep in pins, f"{dep} (transitive) missing from requirements.txt"


def test_every_pin_carries_hashes():
    text = REQS.read_text(encoding="utf-8")
    assert text.count("--hash=sha256:") >= len(_pins(REQS)), \
        "requirements.txt has pins without hashes"


def test_cryptography_is_held_at_50_or_later():
    """⛔ 49.0.0 carried CVE-2026-69247 (a Bleichenbacher oracle in
    `pkcs7_decrypt_*`). Heresay imports only `Fernet`, so it was never reachable
    — but that is a fact about today's call graph, not a safety property."""
    major = int(_pins(REQS)["cryptography"].split(".")[0])
    assert major >= 50, "cryptography pinned below the CVE-2026-69247 fix"


def _commands(path):
    """The runnable lines only — comments explain what USED to be here, and a
    test that greps prose fails on its own explanation."""
    return [l for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def test_the_production_path_installs_the_pinned_file_with_hashes():
    for path in (DEPLOY, MAKEFILE):
        cmds = "\n".join(_commands(path))
        assert "--require-hashes -r requirements.txt" in cmds, path
        # ...and never the old bare resolve-from-PyPI form.
        assert "pip install argon2-cffi" not in cmds, path
        assert "pip install --quiet argon2-cffi" not in cmds, path


def test_the_PRODUCTION_venv_does_not_grow_a_test_framework():
    """⛔ A test runner on a live host is code running as the daemon's user for
    no reason anybody can state. `make venv` and the deploy install the runtime
    file and nothing else; pytest lives only in the dev file."""
    assert "pytest" not in REQS.read_text(encoding="utf-8")
    assert "pytest" in REQS_DEV.read_text(encoding="utf-8")
    deploy = DEPLOY.read_text(encoding="utf-8")
    assert "requirements-dev.txt" not in deploy.replace(
        "requirements-dev.txt is never installed here", "")


def test_the_pip_self_upgrade_stays_a_SEPARATE_install():
    """⚠ `--require-hashes` applies to the whole command, so sweeping the pip
    upgrade into it would fail the install."""
    for path in (DEPLOY, MAKEFILE):
        text = path.read_text(encoding="utf-8")
        line = next(l for l in text.splitlines()
                    if "--upgrade pip" in l and not l.strip().startswith("#"))
        assert "--require-hashes" not in line, path


def test_a_release_can_audit_the_pinned_dependencies():
    """The check that would have caught G1 without an audit."""
    text = MAKEFILE.read_text(encoding="utf-8")
    assert "pip_audit" in text or "pip-audit" in text
    assert "release-check" in text


# ===========================================================================
# B1 — the browser door's security headers (v1.49.0)
# ===========================================================================

CADDY = REPO / "deploy" / "install" / "Caddyfile.example"
CADDY_DEV = REPO / "deploy" / "Caddyfile.dev"


def test_the_caddyfile_carries_the_security_headers():
    """The page makes zero external requests — a promise currently kept by the
    code alone. These headers make the BROWSER enforce it."""
    text = CADDY.read_text(encoding="utf-8")
    assert "Content-Security-Policy" in text
    assert "Strict-Transport-Security" in text
    assert 'X-Content-Type-Options "nosniff"' in text
    assert 'Referrer-Policy "no-referrer"' in text
    assert "Permissions-Policy" in text
    assert "-Server" in text


def test_the_CSP_allows_exactly_what_the_page_needs_and_nothing_else():
    """⛔ Each source checked against web/index.html, not copied from a template.

    A CSP that is wrong in the permissive direction is decoration; one that is
    wrong in the strict direction white-screens the page. So the sources are
    asserted individually, against the reasons they exist.
    """
    csp = next(l for l in CADDY.read_text(encoding="utf-8").splitlines()
               if l.strip().startswith("Content-Security-Policy"))
    assert "default-src 'none'" in csp          # deny by default
    assert "script-src 'unsafe-inline'" in csp  # the one inline <script>
    assert "style-src 'unsafe-inline'" in csp   # the one inline <style>
    assert "font-src 'self'" in csp             # IBMPlexMono-Regular.woff2
    assert "img-src 'self' data:" in csp        # favicon: data: URLs + canvas
    assert "connect-src 'self'" in csp          # the same-origin WebSocket
    assert "frame-ancestors 'none'" in csp      # clickjacking
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    # Nothing may reach the network by another route.
    for banned in ("*", "http:", "https:", "'unsafe-eval'"):
        assert banned not in csp.replace("data:", ""), \
            f"{banned!r} would undo the zero-external-requests promise"


def test_the_connect_src_fallback_is_written_down():
    """⚠ CSP3 covers same-origin `wss://` under 'self', and every current
    browser implements it — but this is the line that would stop the page
    connecting, so the explicit form sits beside it, commented, with the
    operator's own domain placeholder."""
    text = CADDY.read_text(encoding="utf-8")
    assert "wss://your.domain.example" in text
    assert "# Content-Security-Policy" in text     # i.e. commented out


def test_the_script_src_hash_refinement_is_recorded_not_forgotten():
    """'unsafe-inline' is the honest-but-weak part. Hashing it is the stronger
    form and needs a regenerate-on-release step that does not exist — recorded
    so it stays a decision rather than an oversight."""
    text = CADDY.read_text(encoding="utf-8")
    assert "sha256-" in text and "later refinement" in text


def test_the_browser_door_caps_a_request_body():
    text = CADDY.read_text(encoding="utf-8")
    assert "request_body" in text and "max_size 64KB" in text


def test_the_DEV_caddyfile_matches_so_CSP_can_be_tested_before_production():
    """⛔ A CSP is the one part of this deployment that can white-screen the
    page, and the live site is the worst place to discover that. The dev config
    carries the same policy so it can be tried on localhost first.

    ⚠ Minus HSTS, which has no business being pinned into a browser for
    `localhost` — that is the one deliberate difference.
    """
    dev = CADDY_DEV.read_text(encoding="utf-8")
    prod = CADDY.read_text(encoding="utf-8")
    dev_csp = next(l.strip() for l in dev.splitlines()
                   if l.strip().startswith("Content-Security-Policy"))
    prod_csp = next(l.strip() for l in prod.splitlines()
                    if l.strip().startswith("Content-Security-Policy"))
    assert dev_csp == prod_csp, "the dev policy must be the one being tested"
    assert "Strict-Transport-Security" not in dev


# ===========================================================================
# F1 — systemd resource ceilings and hardening (v1.49.0)
# ===========================================================================

def _directives(text):
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#") and line[0].isalpha():
            k, _, v = line.partition("=")
            out.setdefault(k, []).append(v)
    return out


def test_the_unit_sets_resource_ceilings():
    d = _directives(UNIT.read_text(encoding="utf-8"))
    assert d.get("MemoryMax") == ["1G"]
    assert d.get("TasksMax") == ["64"]
    assert d.get("LimitNOFILE") == ["4096"]
    assert d.get("UMask") == ["0077"]


def test_systemd_owns_the_state_directory_mode():
    """It was a hand-made directory of unverified mode. The files inside were
    0600 either way; a world-listable state dir is a map of what to take."""
    d = _directives(UNIT.read_text(encoding="utf-8"))
    assert d.get("StateDirectory") == ["heresay"]
    assert d.get("StateDirectoryMode") == ["0700"]
    # ...and the absolute path the config names is still declared writable.
    assert "/var/lib/heresay" in d.get("ReadWritePaths", [])


def test_the_cheap_hardening_directives_are_all_present():
    d = _directives(UNIT.read_text(encoding="utf-8"))
    for key, want in (
        ("PrivateDevices", "true"), ("ProtectClock", "true"),
        ("ProtectHostname", "true"), ("ProtectProc", "invisible"),
        ("ProcSubset", "pid"), ("RestrictSUIDSGID", "true"),
        ("RemoveIPC", "true"), ("SystemCallArchitectures", "native"),
    ):
        assert d.get(key) == [want], f"{key} is not {want}"
    # No capabilities at all: every port it binds is above 1024.
    assert d.get("CapabilityBoundingSet") == [""]
    assert d.get("AmbientCapabilities") == [""]


def test_the_syscall_filter_ships_ACTIVE_and_flagged():
    """⚠⚠ The one directive here that can stop a working daemon starting.

    It ships active rather than commented, because a hardening line nobody ever
    turns on is not hardening — but the unit has to say, in the file, how to
    back it out. EPERM rather than SIGSYS so a blocked call is an error the
    process can report instead of an instant kill.
    """
    text = UNIT.read_text(encoding="utf-8")
    d = _directives(text)
    assert d.get("SystemCallFilter") == ["@system-service"]
    assert d.get("SystemCallErrorNumber") == ["EPERM"]
    assert "COMMENT OUT THE TWO LINES BELOW" in text
    assert "CONFIRM THE DAEMON STARTED" in text


def test_MemoryMax_is_flagged_as_a_tunable_that_can_bite():
    """Too low OOM-kills the daemon, which is worse than no cap. argon2 is
    m=64 MiB x p=4, transient, per hash in flight."""
    text = UNIT.read_text(encoding="utf-8")
    assert "SIZE IT TO YOUR BOX" in text
    assert "worse than no cap" in text


def test_EVERY_prior_hardening_directive_SURVIVED_the_edit():
    """⛔ The regression guard, and the reason this test is worth its length.

    F1 added eighteen directives to a file that already had ten. The way that
    goes wrong is not a bad new value — it is an old line quietly lost in the
    middle of a large edit, which nothing would report and no test previously
    covered.
    """
    d = _directives(UNIT.read_text(encoding="utf-8"))
    for key, want in (
        ("NoNewPrivileges", "true"), ("ProtectSystem", "strict"),
        ("ProtectHome", "true"), ("PrivateTmp", "true"),
        ("ProtectKernelTunables", "true"), ("ProtectKernelModules", "true"),
        ("ProtectControlGroups", "true"), ("RestrictNamespaces", "true"),
        ("LockPersonality", "true"), ("MemoryDenyWriteExecute", "true"),
        ("Restart", "on-failure"), ("TimeoutStopSec", "300"),
        ("User", "heresay"), ("WorkingDirectory", "/opt/heresay"),
    ):
        assert d.get(key) == [want], f"{key}={want} was lost or changed"
    # The v1.46.0 one, which has its own test above too — belt and braces,
    # because losing it breaks the countdown SILENTLY.
    assert "AF_UNIX" in d["RestrictAddressFamilies"][0]


# ===========================================================================
# The depscan timer and the fail2ban jail (v1.52.6)
#
# ⛔ THE GAP THIS CLOSES. Both of these existed ONLY on the live server, where a
# rebuild would have lost them — and the fail2ban half was worse than missing:
# §9 of the install README had documented a jail since v1.30.0 that the deploy
# never installed, and the live box turned out not to be running fail2ban at
# all. Documentation describing a thing nobody installed is exactly the failure
# mode this file exists to catch, so the files are in the repo now and these
# assertions are what keep the docs and the kit pointing at each other.
# ===========================================================================

INSTALL = REPO / "deploy" / "install"
DEPSCAN = REPO / "deploy" / "bin" / "heresay-depscan.sh"
DEPSCAN_SERVICE = INSTALL / "heresay-depscan.service"
DEPSCAN_TIMER = INSTALL / "heresay-depscan.timer"
HEALTHCHECK = INSTALL / "heresay-healthcheck.sh"
F2B_FILTER = INSTALL / "fail2ban-heresay.filter.conf"
F2B_JAIL = INSTALL / "fail2ban-heresay.jail.conf"

# The exact line the engine writes (events.py), and the one the filter must
# match. Kept here as a literal so a change to either side fails loudly.
AUTH_FAIL_LINE = '{"ts":1785531749.27,"type":"auth_fail","ip":"1.2.3.4"}'

HOST_PATTERN = r"(?P<host>[0-9a-fA-F:.]+)"


def _ini(path):
    """key -> value for a systemd/fail2ban style file, comments dropped."""
    out = {}
    for line in path.read_text(encoding="utf-8").split("\n"):
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


# ---- the dependency scan -------------------------------------------------

def test_the_depscan_files_all_exist():
    for path in (DEPSCAN, DEPSCAN_SERVICE, DEPSCAN_TIMER):
        assert path.exists(), f"{path.name} is missing from the repo"


def test_the_depscan_script_is_executable():
    """It runs straight out of the rsynced checkout, like the other two — so the
    bit has to be committed, not set by hand on the box."""
    import os
    import stat
    assert stat.S_IMODE(os.stat(DEPSCAN).st_mode) & 0o111


def test_the_depscan_unit_names_the_script_where_it_actually_LIVES():
    """⚠ The convention, and the trap: `heresay-backup.sh` and
    `heresay-cert-copy.sh` are NOT copied anywhere — their units point straight
    into `/opt/heresay/deploy/bin`, which is where the rsync puts them. A unit
    naming `/opt/heresay/bin/...` would parse, install, and fail every month
    with "No such file or directory"."""
    d = _ini(DEPSCAN_SERVICE)
    assert d["ExecStart"] == f"{OPT}/deploy/bin/heresay-depscan.sh"
    backup = _ini(INSTALL / "heresay-backup.service")
    assert backup["ExecStart"].rsplit("/", 1)[0] == \
        d["ExecStart"].rsplit("/", 1)[0], "the live scripts must share a home"


def test_the_depscan_unit_is_a_oneshot_that_waits_for_the_network():
    d = _ini(DEPSCAN_SERVICE)
    assert d["Type"] == "oneshot"          # so a finding = a FAILED unit
    assert d["After"] == "network-online.target"
    assert d["Wants"] == "network-online.target"


def test_the_depscan_timer_is_monthly_persistent_and_jittered():
    d = _ini(DEPSCAN_TIMER)
    assert d["OnCalendar"] == "monthly"
    assert d["Persistent"] == "true"       # a box that was off still runs it
    assert d["RandomizedDelaySec"] == "1h"
    assert d["WantedBy"] == "timers.target"


# ---- the state-size check folded into healthcheck (closes audit-3 F3) ----
# v1.53.1 folded the standalone state monitor into heresay-healthcheck, which
# already ran a daily free-disk check: F3 (accounts.json / ban_history.json
# unbounded in principle) is a WATCH, not a store cap. These pin the folded
# behaviour — the state-dir SIZE trigger, the two files named by size, and the
# no-secret reuse of the existing notify path.

def test_the_healthcheck_reports_state_size_and_names_the_two_files():
    """⛔ The F3 view: the disk alert must report the state-dir size and name
    accounts.json + ban_history.json, so a signup flood reads as "the store grew"
    and not a bare "disk filling"."""
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert "accounts.json" in text and "ban_history.json" in text
    assert 'STATE_DIR' in text
    # human sizes in the message, numeric MB only for the threshold compare
    assert "du -sh" in text and "du -sm" in text


def test_the_healthcheck_has_a_state_size_trigger():
    """The size trigger alongside the existing free-space one — catches growth on
    a big disk before free space moves."""
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert "STATE_WARN_MB" in text
    assert 'STATE_WARN_MB:-500' in text          # the documented default
    assert "DISK_WARN" in text                   # the free-space trigger is kept


def test_the_healthcheck_embeds_no_secret_and_reuses_the_notify_path():
    """⛔ No ntfy topic / Healthchecks UUID — it calls the shared
    /usr/local/bin/heresay-notify.sh, which reads the secret out-of-band from
    /etc/heresay/notify.env."""
    text = HEALTHCHECK.read_text(encoding="utf-8")
    assert "/usr/local/bin/heresay-notify.sh" in text
    assert "ntfy.sh/" not in text and "hc-ping.com/" not in text


def test_no_trace_of_the_standalone_state_monitor_remains():
    """⛔ v1.53.1 removed the standalone tool entirely. Nothing in deploy/ or the
    docs may still name it — a stale unit or install line would point at a script
    the rsync deleted. The needle is built from pieces so this guard does not
    itself leave the literal token in the tree for the repo-wide grep to find."""
    import subprocess
    needle = "disk" + "check"
    out = subprocess.run(["git", "grep", "-il", needle, "--", "deploy/", "docs/"],
                         cwd=REPO, capture_output=True, text=True).stdout.strip()
    assert not out, f"standalone monitor still referenced in: {out}"


def test_the_depscan_audits_the_INSTALLED_venv_not_the_pinned_file():
    """The whole point of having it on the box at all: `make audit` checks what
    we intend to install, this checks what is running."""
    text = DEPSCAN.read_text(encoding="utf-8")
    assert '"$DAEMON_VENV/bin/pip" freeze' in text
    assert "DAEMON_VENV=/opt/heresay/.venv" in text
    assert "pip-audit" in text


def test_the_audit_tooling_stays_OUT_of_the_daemon_venv():
    """⛔ The production venv holds the three runtime dependencies and nothing
    else — requirements-dev.txt is never installed on a server, and pip-audit's
    dependency tree has no business next to the daemon's imports."""
    text = DEPSCAN.read_text(encoding="utf-8")
    assert "AUDIT_VENV=/opt/heresay/.audit-venv" in text
    for line in text.split("\n"):
        if "pip-audit" in line and "install" in line:
            assert "$AUDIT_VENV" in line, line


def test_the_deploy_does_not_delete_the_audit_venv_on_every_push():
    """⚠ `--delete-excluded` removes anything in /opt/heresay that is not in the
    repo, and `.venv/` does not match `.audit-venv`. Without its own exclude the
    scan's venv is rebuilt every deploy, for nothing."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--exclude '.audit-venv/'" in text


def test_the_deploy_does_not_ship_local_dev_tool_state():
    """⛔ `.claude/` is machine-local scratch — dev-tool state and any git
    worktrees under it. It is gitignored, but the rsync copies the WORKING TREE,
    so being untracked never kept it off the box; only an exclude does. Anchored
    and directory-suffixed, like the other repo-root dev excludes."""
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--exclude '/.claude/'" in text
    assert ".claude/" in (REPO / ".gitignore").read_text(encoding="utf-8")


# ---- fail2ban ------------------------------------------------------------

def test_the_fail2ban_files_are_IN_THE_REPO():
    """⛔ The gap, as an assertion. This was prose in a README and nothing
    else."""
    for path in (F2B_FILTER, F2B_JAIL):
        assert path.exists(), f"{path.name} is missing from the repo"


def test_the_filter_matches_a_REAL_auth_fail_line():
    """The regex, checked against the exact bytes the engine writes.

    `<HOST>` is fail2ban's own token; it is substituted here with the address
    pattern fail2ban expands it to, which is the closest a unit test gets to
    `fail2ban-regex` without the package installed.
    """
    import re
    failregex = _ini(F2B_FILTER)["failregex"]
    assert "<HOST>" in failregex
    m = re.search(failregex.replace("<HOST>", HOST_PATTERN), AUTH_FAIL_LINE)
    assert m is not None, f"{failregex!r} did not match {AUTH_FAIL_LINE!r}"
    assert m.group("host") == "1.2.3.4"


def test_the_filter_matches_an_IPv6_address_too():
    import re
    pattern = _ini(F2B_FILTER)["failregex"].replace("<HOST>", HOST_PATTERN)
    line = '{"ts":1785531749.27,"type":"auth_fail","ip":"2001:db8::1"}'
    m = re.search(pattern, line)
    assert m is not None and m.group("host") == "2001:db8::1"


def test_the_filter_does_NOT_match_another_event_type_carrying_an_ip():
    """⚠ The fields are matched ADJACENT, so an `ip` belonging to a connection
    or a refusal on the same line cannot satisfy the pattern. A looser
    `.*"ip":"<HOST>"` would ban people for connecting."""
    import re
    pattern = _ini(F2B_FILTER)["failregex"].replace("<HOST>", HOST_PATTERN)
    for line in (
        '{"ts":1785531749.27,"type":"connection","dir":"open","ip":"1.2.3.4"}',
        '{"ts":1785531749.27,"type":"refused","ip":"1.2.3.4","reason":"conn_cap"}',
        '{"ts":1785531749.27,"type":"login","account":"Wren","ip":"1.2.3.4"}',
    ):
        assert re.search(pattern, line) is None, line


def test_the_filter_can_read_the_EPOCH_timestamp():
    """⚠ The half that fails silently: without a datepattern fail2ban treats
    every matched line as "now", and `findtime` stops meaning anything. The
    timestamp is a bare epoch float in a JSON field, not a syslog date."""
    d = _ini(F2B_FILTER)
    assert d["datepattern"] == '"ts":{EPOCH}'


def test_the_jail_polls_the_FILE_and_does_not_read_the_journal():
    """⛔ The gotcha that made the live jail work. Ubuntu defaults to
    `backend = systemd`, which reads the journal — and since v1.30.0 the domain
    facts are NOT in the journal. The default jail runs happily and matches
    nothing, with no error anywhere."""
    d = _ini(F2B_JAIL)
    assert d["backend"] == "polling"
    assert d["logpath"] == "/var/lib/heresay/events.jsonl"
    assert d["filter"] == "heresay"
    assert d["enabled"] == "true"


def test_the_jail_can_never_ban_SSH():
    """⛔ The other gotcha, and the one that would hurt: the ban is scoped to the
    doors. 22 is deliberately absent, so no jail action can take away the way
    you administer the box."""
    ports = [p.strip() for p in _ini(F2B_JAIL)["port"].split(",")]
    assert "2323" in ports
    assert "22" not in ports and "ssh" not in ports


def test_the_jail_ships_with_LOOPBACK_ONLY_and_no_real_address():
    """⛔ No operator's real IP is baked into a repo file. Loopback is the
    template; the admin address is the one edit the README asks for, because a
    FIREWALL ban is the one that can shut you out of your own box."""
    ignore = _ini(F2B_JAIL)["ignoreip"].split()
    assert ignore == ["127.0.0.1/8", "::1"], ignore
    assert "add YOUR OWN admin address" in F2B_JAIL.read_text(encoding="utf-8")


def test_the_jail_dials_are_the_ones_that_were_validated():
    d = _ini(F2B_JAIL)
    assert (d["maxretry"], d["findtime"], d["bantime"]) == ("6", "10m", "1h")


# ---- the install path actually references all of it ----------------------

def test_the_install_README_installs_fail2ban_the_PACKAGE():
    """The docs-vs-reality gap, closed at its root: the checklist installed
    every other thing it documented, and not this one."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "apt install -y python3 python3-venv rsync git fail2ban" in readme
    assert "systemctl enable --now fail2ban" in readme


def test_the_install_README_copies_every_new_file_to_its_destination():
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "$SRC/heresay-depscan.service      /etc/systemd/system/" in readme
    assert "$SRC/heresay-depscan.timer        /etc/systemd/system/" in readme
    assert "$SRC/fail2ban-heresay.filter.conf /etc/fail2ban/filter.d/heresay.conf" \
        in readme
    assert "$SRC/fail2ban-heresay.jail.conf   /etc/fail2ban/jail.d/heresay.conf" \
        in readme


def test_the_install_README_enables_the_new_timer_with_the_others():
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "heresay-depscan.timer" in readme.split("## 7. Turn it on")[1]
    # ⚠ FIVE since the alerting layer's healthcheck timer joined them; the verify step counts
    # them, so this moves with the kit rather than rotting quietly.
    assert "five timers scheduled" in readme


def test_the_README_tells_the_operator_how_to_VERIFY_the_filter():
    """The step that would have caught both fail2ban gotchas on the live box,
    written where somebody will meet it."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "fail2ban-regex /var/lib/heresay/events.jsonl" in readme
    assert "Date template hits" in readme          # the part people skip
    assert "fail2ban-client status heresay" in readme


def test_the_README_records_the_TWO_gotchas_for_the_next_box():
    # ⚠ Whitespace-collapsed: prose wraps, and where it wraps is not the point.
    readme = INSTALL_README.read_text(encoding="utf-8")
    flat = " ".join(readme.replace("**", "").split())
    assert "backend = polling" in readme
    assert "backend = systemd" in readme           # ...and what it would break
    assert "22 is deliberately absent" in flat


def test_the_README_no_longer_documents_a_jail_nobody_installed():
    """⚠ The old section gave inline `heresay-auth` blocks to hand-type. They are
    gone as INSTRUCTIONS — the shipped files are the instructions now. The old
    NAMES still have to survive SOMEWHERE, because an upgrading box has those
    files and a stale jail matches nothing beside the working one — so the
    removal step moved to the upgrade notes with the rest of the migration, and
    this asserts it is still there rather than merely gone from here."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "[heresay-auth]" not in readme          # no longer a thing to write
    upgrading = (REPO / "docs" / "UPGRADING.md").read_text(encoding="utf-8")
    assert "rm -f /etc/fail2ban/filter.d/heresay-auth.conf" in upgrading


# ===========================================================================
# The alerting layer
#
# ⛔ SAME GAP AS DEPSCAN AND FAIL2BAN: a full phone/email alerting layer built
# and validated on the live box, existing nowhere else, one rebuild away from
# being lost. It is repo files now.
#
# ⛔ AND ONE HARD GUARDRAIL THAT THE OTHERS DID NOT HAVE: this layer has a
# SECRET FILE. The ntfy topic is the only auth on public ntfy.sh and the
# Healthchecks URL is the ability to silence the alarm — so the repo ships
# placeholders only, and the install refuses to overwrite a filled-in file.
# ===========================================================================

NOTIFY_SCRIPTS = ("heresay-notify.sh", "heresay-hcping.sh",
                  "heresay-ceiling-watch.sh", "heresay-report-watch.sh",
                  "heresay-healthcheck.sh", "heresay-boot-notify.sh")
NOTIFY_UNITS = ("heresay-notify@.service", "heresay-ceiling-watch.service",
                "heresay-report-watch.service",
                "heresay-healthcheck.service", "heresay-healthcheck.timer",
                "heresay-boot-notify.service")
NOTIFY_DROPINS = ("onfailure.conf", "hcping.conf")
NOTIFY_ENV_EXAMPLE = INSTALL / "notify.env.example"

# The five units worth being woken up for.
ONFAILURE_UNITS = ("heresay", "heresay-depscan", "heresay-backup", "caddy",
                   "heresay-cert-copy")

# Where the host-ops scripts run from on the box. ⚠ NOT /opt/heresay — see the
# test below.
LOCAL_BIN = "/usr/local/bin"


def test_every_alerting_file_is_in_the_repo():
    for name in NOTIFY_SCRIPTS + NOTIFY_UNITS + NOTIFY_DROPINS:
        assert (INSTALL / name).exists(), f"{name} is missing from the repo"
    assert NOTIFY_ENV_EXAMPLE.exists()


def test_the_alerting_scripts_are_executable():
    import os
    import stat
    for name in NOTIFY_SCRIPTS:
        mode = stat.S_IMODE(os.stat(INSTALL / name).st_mode)
        assert mode & 0o111, f"{name} is not executable"


# ---- the runtime path, and why it is not /opt/heresay --------------------

def test_every_alerting_unit_runs_from_usr_local_bin():
    """⛔ THE PATH DECISION, as an assertion. `make deploy` rsyncs /opt/heresay
    with --delete --delete-excluded, so anything living there that is not in the
    repo is removed on the next push. These are host-ops scripts: they belong
    outside the rsync target, where a code deploy cannot disturb them and they
    keep working while the daemon is the thing that is broken."""
    for name in NOTIFY_UNITS:
        text = (INSTALL / name).read_text(encoding="utf-8")
        for line in text.split("\n"):
            if line.startswith("ExecStart"):
                assert LOCAL_BIN in line, (name, line)
                assert "/opt/heresay" not in line, (name, line)


def test_the_backup_dropin_also_runs_from_outside_the_rsync_target():
    text = (INSTALL / "hcping.conf").read_text(encoding="utf-8")
    assert f"ExecStartPost={LOCAL_BIN}/heresay-hcping.sh success" in text
    assert "/opt/heresay" not in text


# ---- ⛔ the secret guardrail ----------------------------------------------

def test_the_example_env_holds_PLACEHOLDERS_ONLY():
    """⛔ The whole reason this file is `.example`. Both values are secrets in
    the only sense that matters — knowing one IS the permission."""
    text = NOTIFY_ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "NTFY_URL=https://ntfy.sh/CHANGE-ME" in text
    assert "HC_BACKUP_URL=https://hc-ping.com/CHANGE-ME" in text


def test_NO_REAL_ntfy_topic_or_healthchecks_uuid_IS_IN_THE_REPO():
    """⛔ THE GUARDRAIL, swept over every tracked file rather than trusted.

    A real ntfy topic in a public repo is a stranger reading your alerts and
    posting fake ones; a real Healthchecks URL is a stranger silencing the alarm
    that says your backups stopped. Every occurrence of either host must be a
    CHANGE-ME placeholder or prose about the placeholder.
    """
    import re
    pattern = re.compile(r"(?:ntfy\.sh|hc-ping\.com)/([A-Za-z0-9_-]+)")
    offenders = []
    for path in sorted(REPO.rglob("*")):
        if not path.is_file() or ".git/" in str(path):
            continue
        if path.suffix in (".woff2", ".png", ".ico", ".gz"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for token in pattern.findall(text):
            if not token.startswith("CHANGE-ME"):
                offenders.append(f"{path.relative_to(REPO)}: {token}")
    assert not offenders, ("a real ntfy topic / healthchecks uuid is in the "
                           "repo:\n" + "\n".join(offenders))


def test_a_stray_notify_env_in_the_checkout_is_GITIGNORED():
    """A belt on the guardrail: the real file belongs in /etc, but an operator
    who drops a filled-in copy in the checkout should not be one `git add -A`
    away from publishing their own alert topic."""
    ignore = (REPO / ".gitignore").read_text(encoding="utf-8").split("\n")
    assert "notify.env" in [l.strip() for l in ignore]
    # ...and the EXAMPLE is still tracked, or the kit would ship without it.
    assert "notify.env.example" not in [l.strip() for l in ignore]


def test_the_install_REFUSES_to_overwrite_a_filled_in_notify_env():
    """⚠ The install seeds the file only when it is ABSENT. Copying the example
    over a filled-in notify.env would silently disable every alert on the box —
    and the failure would be invisible, because the symptom is silence, which is
    also what "everything is fine" looks like."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "if [ ! -e /etc/heresay/notify.env ]; then" in readme
    # The example itself is always copied; only the real file is guarded.
    assert "notify.env.example /etc/heresay/notify.env.example" in readme


def test_the_secret_file_is_installed_0600():
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "install -m 0600" in readme
    assert "chmod 600 /etc/heresay/notify.env" in readme


# ---- the notifier must never fail its caller -----------------------------

def test_the_notifier_never_fails_its_caller():
    """⛔ It is called from `OnFailure=` and from `ExecStartPost=`. A notifier
    that returned non-zero would mark the very unit it was reporting on as
    failed — turning one problem into two, and the second into a loop."""
    for name in ("heresay-notify.sh", "heresay-hcping.sh"):
        text = (INSTALL / name).read_text(encoding="utf-8")
        assert text.rstrip().endswith("exit 0"), name
        # Every early return is an exit 0, and the curl cannot propagate.
        assert "|| echo" in text
        assert "set -uo pipefail" in text        # ⚠ NOT -e: see above
        assert "set -euo pipefail" not in text



# ---- the deploy blocklist covers EVERY private doc -------------------------

# ⛔ Every file in docs/ is one or the other, and the sides are DERIVED FROM THE
# TREE rather than typed out here. `deploy.sh`'s excludes are a BLOCKLIST, and a
# blocklist ships whatever it forgot — `CODE_AUDIT.md` was copied to the
# production host on every push until v1.55.1 because nobody had added a line.
#
# ⚠ DEPLOY-EXCLUDED IS NOT THE SAME AS PRIVATE, which is why the set is named for
# what it is. `UPGRADING.md` has been excluded since the list existed and ships in
# the public repo: the runtime host has no use for a version-migration note.


def _docs_excluded_by_the_deploy():
    """The `docs/*.md` basenames `deploy.sh` refuses to send, read out of it.

    ⚠ The script is the authority on what is private, because it is the thing
    that acts on the answer. A second list beside it can only ever disagree."""
    text = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    return frozenset(re.findall(r"--exclude '/docs/([A-Za-z0-9_.-]+\.md)'", text))


def _docs_present():
    return frozenset(p.name for p in (REPO / "docs").glob("*.md"))


# ⚠ Intersected with what is PRESENT, deliberately. `deploy.sh` ships verbatim
# into the public tree, excludes and all, so downstream it names files that were
# never there — which is correct for an operator's own private docs and must not
# read as a stale line here.
DEPLOY_EXCLUDED_DOCS = frozenset(_docs_excluded_by_the_deploy() & _docs_present())
PUBLIC_DOCS = frozenset(_docs_present() - _docs_excluded_by_the_deploy())


MAINTAINER_MAILBOX = "heresay@bigpoetry.net"


def test_SECURITY_md_names_the_maintainer_mailbox():
    """⛔ The published security contact is PINNED here, for two reasons.

    One: it is a promise to a stranger. A researcher who writes to the address in
    `SECURITY.md` and hears nothing concludes the project does not answer and
    discloses publicly — so the address the policy prints has to be the address
    the maintainer actually watches, and a silent edit to it is a silent breach
    of that promise.

    Two: it is the ENGINE's address, not any one house's. `SECURITY.md` ships
    with the code, so a downstream operator's copy names the maintainer — which
    is why the old `.mail admin` / instance-address route was wrong and was
    removed in v1.61.4. ⚠ A house's OWN contact is a different address and does
    not belong in this file.

    ⚠ **What this test CANNOT do is prove the mailbox is live** — an offline
    suite has no way to. Standing it up and sending to it from outside is
    the "The two mailboxes" gate in `docs/ROADMAP.md`, and only the maintainer can clear it.
    ⚠ Cited by NAME, never by number: that file has no numbered lists, so a
    number here could only ever be one somebody invented (this line said
    "gate 5" for ten releases after gate 5 stopped existing).
    """
    text = (REPO / "SECURITY.md").read_text(encoding="utf-8")
    assert MAINTAINER_MAILBOX in text, (
        f"SECURITY.md no longer names {MAINTAINER_MAILBOX}. If the contact "
        "address changed, change it HERE and in docs/ROADMAP.md's "
        "\"The two mailboxes\" gate "
        "together — they are a promise and its owner."
    )
    # ⛔ The retired routes: an in-world command on somebody else's instance, and
    # a config key deleted in v1.60.0. Both were in this file until v1.61.4.
    assert ".mail admin" not in text, "the in-world route was removed in v1.61.4"
    assert "[moderation] arch" not in text, (
        "`arch` was deleted in v1.60.0 — the key is `[moderation] owner`"
    )


def test_every_deploy_excluded_doc_has_its_exclude_line():
    """Every doc on the excluded side is excluded by a line `deploy.sh` still
    carries, and every one of them is a file that exists."""
    text = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    for name in sorted(DEPLOY_EXCLUDED_DOCS):
        assert f"--exclude '/docs/{name}'" in text, name
        assert (REPO / "docs" / name).is_file(), name


def test_the_public_docs_are_NOT_excluded():
    """⚠ The mirror of the rule: a doc that SHIPS must not pick up an exclude
    line by accident. OPERATIONS.md in particular is what an operator reaches for
    while something is on fire — excluding it would take the operations doc off
    the box that needs it.

    ⛔ The assertion that carries weight is the SECOND one: the public side is
    not empty. A `deploy.sh` that excluded every doc would satisfy a partition
    and ship an operator a box with no documentation on it."""
    text = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    for name in sorted(PUBLIC_DOCS):
        assert f"--exclude '/docs/{name}'" not in text, name
    assert PUBLIC_DOCS, "deploy.sh excludes every doc there is"


def test_every_doc_is_classified_public_or_private():
    """⛔ Every `docs/*.md` lands on exactly one side, and the two sides are
    disjoint and together are the whole directory.

    ⚠ By construction now rather than by anybody's memory — which is the point
    of deriving them. What this still catches is the derivation itself breaking:
    a reworded `--exclude` line that the parser stops matching silently moves a
    private doc to the public side, and the counts below stop adding up."""
    present = _docs_present()
    assert DEPLOY_EXCLUDED_DOCS | PUBLIC_DOCS == present, (
        "the two sides do not cover docs/: "
        f"missing {sorted(present - (DEPLOY_EXCLUDED_DOCS | PUBLIC_DOCS))}")
    assert not (DEPLOY_EXCLUDED_DOCS & PUBLIC_DOCS), (
        f"a doc is on both sides: {sorted(DEPLOY_EXCLUDED_DOCS & PUBLIC_DOCS)}")
    assert present, "docs/ holds no markdown at all — the path moved"


def test_the_deploy_deletes_what_it_excludes():
    """⚠ Why adding an exclude is enough to CLEAN UP as well as prevent. Without
    --delete-excluded, a file already copied by an earlier push stays on the box
    for ever and the new exclude only stops the next copy."""
    text = (REPO / "deploy" / "deploy.sh").read_text(encoding="utf-8")
    assert "--delete-excluded" in text

# ---- the ceiling watcher is tied to what the ENGINE actually writes -------

def test_the_ceiling_watcher_matches_the_REAL_serialized_event(tmp_path):
    """⛔ THE TEST THIS LAYER MOST NEEDED. The watcher greps `events.jsonl` for a
    fixed substring; if the engine ever serialised that event differently, the
    alert would stop firing SILENTLY — which is the worst failure an alerting
    layer has, because silence is also what "nothing is wrong" looks like.

    So the pattern is not asserted against a literal typed twice: a real
    `auth_ceiling` event is written by the real EventStore and the watcher's own
    pattern is matched against those bytes.
    """
    from heresay.events import EventStore

    text = (INSTALL / "heresay-ceiling-watch.sh").read_text(encoding="utf-8")
    # Pull the pattern out of the script rather than restating it here.
    marker = [l for l in text.split("\n") if l.strip().startswith("*'\"type\"")]
    assert len(marker) == 1, "the watcher's match line moved or multiplied"
    pattern = marker[0].strip().strip("*)").strip("*").strip("'")

    store = EventStore(tmp_path / "events.jsonl")
    store.record("auth_ceiling", phase="tripped", threshold=50, window=60.0,
                 cooldown=120.0)
    store.record("auth_ceiling", phase="cleared")
    lines = [l for l in (tmp_path / "events.jsonl").read_text(
        encoding="utf-8").split("\n") if l]

    assert any(pattern in l for l in lines), (
        f"the watcher's pattern {pattern!r} matches nothing the engine writes")
    # ...and it must NOT fire on the clear, or every incident buzzes twice.
    assert sum(1 for l in lines if pattern in l) == 1


def test_the_watcher_follows_by_NAME_because_the_engine_trims_in_place():
    """⚠ `tail -F`, not `-f`. The engine rewrites events.jsonl to honour
    `event_retention_days`, which replaces the inode — `-f` would go on watching
    a file nobody writes to any more and report nothing, forever."""
    text = (INSTALL / "heresay-ceiling-watch.sh").read_text(encoding="utf-8")
    assert "tail -n0 -F" in text


def test_the_watcher_debounces_a_sustained_run():
    text = (INSTALL / "heresay-ceiling-watch.sh").read_text(encoding="utf-8")
    assert "COOLDOWN=1800" in text


def test_the_watcher_gates_alerts_on_the_event_timestamp():
    """⛔ THE INVARIANT, pinned as text: a given event alerts AT MOST ONCE, EVER.

    TWO persisted values, and both halves of the gate. The `ts >= START` half is
    the v1.53.4 freshness rule (a restart must not re-report history); the
    `ts > high` half is v1.55.1's, and it is the one that survives a retention
    trim replaying the file. ⚠ `>=` on START and `>` on the high-water mark is
    deliberate — a trip in the same second the watcher started is real."""
    text = (INSTALL / "heresay-ceiling-watch.sh").read_text(encoding="utf-8")
    assert "START=" in text
    assert '"$ts" -ge "$START"' in text
    assert "HIGHWATER=" in text
    assert '"$ts" -gt "$high"' in text


def test_the_high_water_mark_advances_BEFORE_the_debounce():
    """⛔ THE HOLE THE ONE-VALUE DESIGN HAD, pinned by source order. An event the
    debounce swallows has still been PROCESSED; if that is not recorded it stays
    eligible and re-alerts when a trim replays it hours later. The write must
    therefore come BEFORE the cooldown check, not after the notify."""
    text = (INSTALL / "heresay-ceiling-watch.sh").read_text(encoding="utf-8")
    advance = text.index('echo "$ts" > "$HIGHWATER"')
    debounce = text.index('-lt "$COOLDOWN"')
    notify = text.index('"$NOTIFY" high')
    assert advance < debounce < notify


def _run_ceiling_watch(tmp_path, log_lines, start_epoch, state=None):
    """Run the real watcher against a FINITE canned stream (CEILING_TAIL=cat) with
    a stub notifier, and return the CUMULATIVE number of alerts pushed into
    `state`. The CEILING_* seams default to production values, so injecting them
    here does not change what the box runs.

    ⚠ `state` is the persistence directory (stamp + high-water + the stub's call
    log). Passing the SAME one to two runs is how a file replacement is modelled:
    `tail -F` reopening a trimmed log and re-reading it from the top is, from the
    script's point of view, the same lines arriving again against the same
    persisted state.
    """
    import os
    import subprocess

    state = state or tmp_path
    state.mkdir(parents=True, exist_ok=True)
    calls = state / "calls"
    stub = state / "notify.sh"
    stub.write_text(f'#!/usr/bin/env bash\necho "$2" >> {calls}\n', encoding="utf-8")
    stub.chmod(0o755)
    log = state / "events.jsonl"
    log.write_text("".join(l + "\n" for l in log_lines), encoding="utf-8")

    env = dict(os.environ,
               NOTIFY=str(stub), CEILING_TAIL="cat", CEILING_LOG=str(log),
               CEILING_STAMP=str(state / "stamp"),
               CEILING_HIGHWATER=str(state / "high"),
               CEILING_START=str(start_epoch))
    subprocess.run(["bash", str(INSTALL / "heresay-ceiling-watch.sh")],
                   env=env, check=True, timeout=15)
    return calls.read_text().count("\n") if calls.exists() else 0


def test_a_stale_trip_on_startup_does_NOT_alert(tmp_path):
    """A trip whose ts predates the watcher's start — from a prior run, or
    re-surfaced by a trim — must be ignored."""
    n = _run_ceiling_watch(
        tmp_path,
        ['{"ts":1000000000.0,"type":"auth_ceiling","phase":"tripped","threshold":50}'],
        start_epoch=1_500_000_000)
    assert n == 0


def test_a_fresh_trip_after_startup_DOES_alert(tmp_path):
    """A trip stamped at or after the watcher's start alerts at once — including a
    trip moments after a restart. And a `cleared` line never alerts."""
    n = _run_ceiling_watch(
        tmp_path,
        ['{"ts":1000000000.0,"type":"auth_ceiling","phase":"tripped"}',   # stale: ignored
         '{"ts":2000000000.0,"type":"auth_ceiling","phase":"tripped"}',   # fresh: alerts
         '{"ts":2000000001.0,"type":"auth_ceiling","phase":"cleared"}'],  # never alerts
        start_epoch=1_500_000_000)
    assert n == 1


TRIPPED = '{{"ts":{ts}.0,"type":"auth_ceiling","phase":"tripped"}}'


def test_an_ALREADY_ALERTED_event_replayed_after_a_file_replacement_is_SILENT(tmp_path):
    """⚡ THE BUG, SEVEN TIMES OVER. The engine trims events.jsonl to honour
    `event_retention_days`; the trim replaces the inode and `tail -F` reopens the
    replacement AT ITS TOP, so every surviving line arrives again. Under the old
    one-value gate an event NEWER than the watcher's start passed a second time
    and re-alerted — observed live. The high-water mark makes the replay silent."""
    state = tmp_path / "run"
    first = _run_ceiling_watch(tmp_path, [TRIPPED.format(ts=2_000_000_000)],
                               start_epoch=1_500_000_000, state=state)
    assert first == 1, "the genuine trip must alert once"

    # ⚠ AGE THE DEBOUNCE OUT FIRST. Without this the test passes against the OLD
    # one-value script — not because the gate works but because the 30-minute
    # cooldown happens to still be running a second later. A trim replay in the
    # real world arrives hours after the alert, and the test has to be the same
    # shape or it proves nothing. (Checked: with the stamp aged, this fails on
    # the pre-v1.55.1 script and passes on this one.)
    (state / "stamp").write_text("0\n", encoding="utf-8")

    # The same line again, same watcher, same persisted state — a trim replay.
    again = _run_ceiling_watch(tmp_path, [TRIPPED.format(ts=2_000_000_000)],
                               start_epoch=1_500_000_000, state=state)
    assert again == 1, "the replay must add NO further alert"


def test_a_DEBOUNCED_event_replayed_later_is_SILENT(tmp_path):
    """⚡ THE HOLE IN THE ONE-VALUE DESIGN, which is why the mark advances whether
    or not a notification was sent.

    The second trip here is swallowed by the 30-minute debounce, so nothing was
    ever sent for it. If that fact is not recorded it stays eligible for ever —
    and when a trim replays it later, by which time the debounce has long since
    expired, it alerts as if it were new. ⚠ The stamp is aged between the runs so
    the debounce CANNOT be what saves us: the high-water mark has to."""
    state = tmp_path / "run"
    first = _run_ceiling_watch(
        tmp_path,
        [TRIPPED.format(ts=2_000_000_000),   # alerts, sets stamp + high-water
         TRIPPED.format(ts=2_000_000_060)],  # inside the cooldown: NOT sent
        start_epoch=1_500_000_000, state=state)
    assert first == 1, "the burst must produce exactly one alert"
    assert (state / "high").read_text().strip() == "2000000060", (
        "the swallowed event must still advance the high-water mark")

    # Age the debounce out — a real replay happens long after the cooldown.
    (state / "stamp").write_text("0\n", encoding="utf-8")
    again = _run_ceiling_watch(tmp_path, [TRIPPED.format(ts=2_000_000_060)],
                               start_epoch=1_500_000_000, state=state)
    assert again == 1, "the never-sent event must NOT alert on replay"


def test_the_ceiling_watcher_restarts_if_it_dies():
    """A watcher that dies quietly is worse than no watcher, because you believe
    you are covered."""
    d = _ini(INSTALL / "heresay-ceiling-watch.service")
    assert d["Restart"] == "always"
    assert d["WantedBy"] == "multi-user.target"


# ---- nothing in the kit names one operator's box -------------------------

def _without_licence_header(path):
    """The file's text with its SPDX licence header dropped.

    ⚠ The copyright holder's name is not configuration. The header carries
    `Copyright (C) 2026 BigPoetry` on every source file we author, and the
    hostname sweep below reads the maintainer's handle as a leaked host — which
    it would be anywhere else in these files, and is not here. ⛔ Expressed as a
    rule over the header's shape rather than an exception for one name, so a
    future holder or a re-worded notice needs no edit."""
    keep = [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if not re.match(r"\s*#\s*(Copyright \(C\)|SPDX-License-Identifier:)", ln)]
    return "\n".join(keep)


def test_no_alerting_file_hardcodes_a_HOSTNAME_or_DOMAIN():
    """⚠ The live box's copies named that box and probed its own domain. The
    install kit is house-neutral everywhere else
    (`commons.example.net`, `your.domain.example`), and a hardcoded SNI would make
    the cert check silently never fire for anybody else — the same silent-failure
    shape as the fail2ban backend. The hostname comes from systemd's `%H` or
    `hostname -s`; the domain comes from the file cert-copy already reads."""
    for name in NOTIFY_SCRIPTS + NOTIFY_UNITS + NOTIFY_DROPINS:
        text = _without_licence_header(INSTALL / name).lower()
        assert "commons" not in text, name
        assert "bigpoetry" not in text, name
    hc = (INSTALL / "heresay-healthcheck.sh").read_text(encoding="utf-8")
    assert "/etc/heresay/cert-copy.env" in hc     # one place names the domain
    assert 'DOMAIN="your.domain.example"' in hc   # ...and the neutral default


# ---- the install path references every piece -----------------------------

def test_the_boot_notice_reports_STATES_and_hands_over_no_errand():
    """⛔ AN ALERT THAT CAN ANSWER ITS OWN QUESTION MUST (v1.62.26).

    The boot notice used to end `verify: systemctl status heresay caddy` — a
    buzz whose entire content was a command the sender could have run itself.
    The states ARE the message now, and no `systemctl`/`journalctl` errand may
    reappear in what reaches the phone.

    ⚠ Asserted against the notifier CALL, not the file: the script runs
    `systemctl is-active` legitimately, and a whole-file grep would fail on the
    line that does the work.

    ⚠ SCOPED TO THIS NOTICE ON PURPOSE. `heresay-notify@.service` DOES hand over
    `systemctl status` / `journalctl`, and that is correct rather than the same
    fault: it already reports the fact (`<unit> FAILED`), and the question it
    leaves open — *why* — has no state to sample and no answer that fits on a
    lock screen. The rule this test carries is **if the state is samplable,
    sample it**; that one is not.
    """
    unit = (INSTALL / "heresay-boot-notify.service").read_text(encoding="utf-8")
    sh = (INSTALL / "heresay-boot-notify.sh").read_text(encoding="utf-8")
    for name, text in (("unit", unit), ("script", sh)):
        for raw in text.splitlines():
            line = raw.split("#", 1)[0]
            if "heresay-notify.sh" not in line and "$NOTIFY" not in line:
                continue
            if line.strip().startswith("NOTIFY="):
                continue                       # the definition, not a message
            assert "systemctl" not in line, (name, raw)
            assert "journalctl" not in line, (name, raw)
    # ⛔ And the unit no longer carries the message at all — a unit file cannot
    # sample a state, so a canned string there is the shape of the old bug.
    assert "verify:" not in unit
    assert "/usr/local/bin/heresay-boot-notify.sh" in unit


def test_the_boot_notice_waits_for_a_BOUNDED_time_before_reporting():
    """⛔ `After=` orders a START, not a healthy state — and heresay is
    `Type=simple`, so it counts as started the instant it execs. A notice that
    sampled at the ordering point would call a perfectly healthy service
    `activating`, and ⚠ a false alarm is worse than the nag it replaced.

    So: ordering to narrow the window, a bounded poll to close it, and a hard
    ceiling above the poll so a wedged notice can never hold multi-user.target.
    """
    unit = (INSTALL / "heresay-boot-notify.service").read_text(encoding="utf-8")
    directives = [l for l in unit.splitlines() if l and not l.startswith("#")]
    assert "After=heresay.service caddy.service" in directives
    # ⛔ Ordering only: Wants=/Requires= on those two would pull a service into
    # the boot transaction, or fail the notice in the case it exists for.
    assert not [l for l in directives if l.startswith(("Requires=", "Wants=heresay",
                                                      "Wants=caddy"))]
    assert [l for l in directives if l.startswith("TimeoutStartSec=")]

    sh = (INSTALL / "heresay-boot-notify.sh").read_text(encoding="utf-8")
    assert "BOOT_WAIT" in sh                      # the bound exists
    assert "SECONDS" in sh and "deadline" in sh   # ...and the loop honours it
    # ⛔ It always fires: no silent-when-healthy branch to add later by accident.
    assert sh.count("$NOTIFY") == 2               # exactly the two shapes
    assert "exit 0" in sh


def test_the_README_installs_every_script_and_unit():
    readme = INSTALL_README.read_text(encoding="utf-8")
    for name in NOTIFY_UNITS:
        assert f"$SRC/{name}" in readme, name
    # The scripts go through a loop, so assert the loop names each of them.
    for name in NOTIFY_SCRIPTS:
        assert name[: -len(".sh")] in readme, name
    assert f"/usr/local/bin/$f.sh" in readme


def test_the_README_installs_the_FIVE_onfailure_dropins():
    readme = INSTALL_README.read_text(encoding="utf-8")
    line = [l for l in readme.split("\n") if l.startswith("for u in heresay ")]
    assert len(line) == 1
    named = set(line[0].replace(";", " ").split())
    for unit in ONFAILURE_UNITS:
        assert unit in named, (unit, line[0])
    assert "$u.service.d/onfailure.conf" in readme
    assert "heresay-backup.service.d/hcping.conf" in readme


def test_the_README_enables_the_watcher_and_the_timer_but_NOT_boot_notify():
    """⚠ `enable`, not `enable --now`, for boot-notify: starting it by hand would
    announce a boot that did not happen."""
    readme = INSTALL_README.read_text(encoding="utf-8")
    enable = " ".join(readme.split())
    assert "enable --now heresay-ceiling-watch.service \\ " \
           "heresay-report-watch.service heresay-healthcheck.timer" in enable
    boot = [l for l in readme.split("\n")
            if "heresay-boot-notify.service" in l and "systemctl" in l]
    assert boot, "boot-notify is never enabled"
    for line in boot:
        # The COMMAND, not the comment beside it — which says "NOT --now" and
        # would otherwise trip this on the very line that gets it right.
        command = line.split("#", 1)[0]
        assert "--now" not in command, line


def test_the_README_documents_the_alerting_layer():
    readme = INSTALL_README.read_text(encoding="utf-8")
    flat = " ".join(readme.replace("**", "").split())
    assert "## 12. Alerting" in readme
    # The model, the mandatory step, and the path reasoning.
    assert "ntfy" in readme and "Healthchecks" in readme
    assert "dead-man's-switch" in readme
    assert "head -c 9 /dev/urandom" in readme          # the topic one-liner
    assert "outside the rsync target" in flat
    # Every alert has a row saying what fires it.
    for trigger in ("auth spray", "disk", "cert expires", "rebooted",
                    "backup silence", "a report was filed"):
        assert trigger in flat, trigger


def test_the_five_timers_are_counted_in_the_verify_step():
    readme = INSTALL_README.read_text(encoding="utf-8")
    assert "five timers scheduled" in readme
    assert "systemctl is-active heresay-ceiling-watch" in readme
    assert "systemctl is-active heresay-report-watch" in readme


# ===========================================================================
# ⛔ THE REPORT WATCHER — a nudge, never the content (v1.57.0)
#
# It is `heresay-ceiling-watch.sh`'s gate, deliberately copied rather than
# shared (the script argues why: a shared shell library would be a fifth file
# in /usr/local/bin whose absence kills a watcher, which is the exact class of
# silent failure this layer exists to notice). What buys the guarantee instead
# is `test_BOTH_watchers_advance_the_mark_before_the_debounce` below, which
# reads both scripts and would also catch a third watcher written years from
# now — something a shared library could not do.
# ===========================================================================

REPORT_WATCH = INSTALL / "heresay-report-watch.sh"


def test_the_report_watcher_matches_the_REAL_serialized_event(tmp_path):
    """The watcher greps for a substring of what the ENGINE writes, so the two
    are pinned to each other rather than to somebody's memory of the format."""
    import sys
    sys.path.insert(0, str(REPO))
    from heresay.events import EventStore

    text = REPORT_WATCH.read_text(encoding="utf-8")
    pattern = None
    for raw in text.split("\n"):
        if raw.strip().startswith("*'\"type\":\"report_filed\"'*)"):
            pattern = raw.strip()[2:-3]        # strip  *'  and  '*)
    assert pattern, "the match line is not where the test thinks it is"

    store = EventStore(tmp_path / "events.jsonl", retention_days=0)
    store.record("report_filed")
    store.record("staff_mail", account="Corvid")
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert pattern in lines[0], (pattern, lines[0])
    # ⚠ And NOT the neighbours: a looser pattern would buzz for staff mail.
    assert pattern not in lines[1]


def test_the_report_event_the_watcher_reads_carries_NOTHING(tmp_path):
    """⛔ THE COVENANT LINE, at the source. The nudge cannot leak content because
    the event has no field that could hold any: `{ts, type}`, full stop."""
    import sys
    sys.path.insert(0, str(REPO))
    from heresay.events import EventStore

    store = EventStore(tmp_path / "events.jsonl", retention_days=0)
    store.record("report_filed")
    (entry,) = store.read()
    assert set(entry) == {"ts", "type"}


def test_the_report_watcher_body_names_the_command_and_carries_no_fields():
    """The message is a doorbell and a pointer. ⛔ Nothing in it is interpolated
    from the event — and a count is deliberately absent, because a count is the
    first step toward the alert describing the box rather than pointing at it."""
    text = REPORT_WATCH.read_text(encoding="utf-8")
    body = [l for l in text.split("\n") if "log in and .read_report" in l]
    assert body, "the nudge body is not where the test thinks it is"
    line = body[0]
    # Only ONE shell expansion, and it is the hostname.
    assert line.count("${") == 1 and "${HOST}" in line
    assert "$line" not in line and "$ts" not in line
    for word in ("count", "from", "about", "subject", "reporter"):
        assert word not in line.lower(), word


def test_BOTH_watchers_advance_the_mark_before_the_debounce():
    """⚡ THE INVARIANT, swept across every watcher rather than shared as code.

    ⛔ `advance < debounce < notify` in each: an event the debounce swallows must
    still be RECORDED as processed, or it stays eligible and re-alerts when a
    trim replays it hours later. That is the bug that took seven attempts on the
    ceiling watcher; this asserts the shape in both files, so a third watcher
    written later is covered by the same line."""
    for script, high in ((INSTALL / "heresay-ceiling-watch.sh", '"$HIGHWATER"'),
                         (REPORT_WATCH, '"$HIGHWATER"')):
        text = script.read_text(encoding="utf-8")
        advance = text.index(f'echo "$ts" > {high}')
        debounce = text.index('-lt "$COOLDOWN"')
        notify = text.index('"$NOTIFY" ')
        assert advance < debounce < notify, script.name


def test_the_two_watchers_do_NOT_share_state_files():
    """⚠ One high-water mark between them would have each silencing the other's
    events — the marks are timestamps in one shared log, so a ceiling trip at
    T would suppress a report at T-1."""
    ceiling = (INSTALL / "heresay-ceiling-watch.sh").read_text(encoding="utf-8")
    report = REPORT_WATCH.read_text(encoding="utf-8")
    assert "/run/heresay-ceiling.high" in ceiling
    assert "/run/heresay-report.high" in report
    assert "heresay-ceiling." not in report
    assert "heresay-report." not in ceiling


def _run_report_watch(tmp_path, log_lines, start_epoch, state=None):
    """Run the REAL watcher against a finite canned stream (REPORT_TAIL=cat)
    with a stub notifier, and return the CUMULATIVE nudges pushed into `state`.

    ⚠ The stub records the notifier's whole argv, so the no-content test can
    grep every byte the notifier was handed — title and body both.
    """
    import os
    import subprocess

    state = state or tmp_path
    state.mkdir(parents=True, exist_ok=True)
    calls = state / "calls"
    stub = state / "notify.sh"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {calls}\n',
                    encoding="utf-8")
    stub.chmod(0o755)
    log = state / "events.jsonl"
    log.write_text("".join(l + "\n" for l in log_lines), encoding="utf-8")

    env = dict(os.environ,
               NOTIFY=str(stub), REPORT_TAIL="cat", REPORT_LOG=str(log),
               REPORT_STAMP=str(state / "stamp"),
               REPORT_HIGHWATER=str(state / "high"),
               REPORT_START=str(start_epoch))
    subprocess.run(["bash", str(REPORT_WATCH)], env=env, check=True, timeout=15)
    return calls.read_text().count("\n") if calls.exists() else 0


FILED = '{{"ts":{ts}.0,"type":"report_filed"}}'


def test_a_report_after_startup_nudges_EXACTLY_ONCE(tmp_path):
    """The ordinary case — and a report older than the watcher's start is
    history (a prior run, or a trim re-surfacing it) and stays silent."""
    n = _run_report_watch(
        tmp_path,
        [FILED.format(ts=1_000_000_000),                        # stale: ignored
         FILED.format(ts=2_000_000_000),                        # fresh: nudges
         '{"ts":2000000001.0,"type":"staff_mail","account":"Corvid"}'],
        start_epoch=1_500_000_000)
    assert n == 1


def test_an_ALREADY_NUDGED_report_replayed_after_a_file_replacement_is_SILENT(tmp_path):
    """⚡ THE TRIM REPLAY. The engine trims events.jsonl to honour
    `event_retention_days`; the trim replaces the inode and `tail -F` reopens
    the replacement AT ITS TOP, so every surviving line arrives again."""
    state = tmp_path / "run"
    first = _run_report_watch(tmp_path, [FILED.format(ts=2_000_000_000)],
                              start_epoch=1_500_000_000, state=state)
    assert first == 1, "the genuine report must nudge once"

    # ⚠ AGE THE DEBOUNCE OUT FIRST, or this passes on a one-value gate — not
    # because the gate works but because the cooldown is still running a second
    # later. A real replay arrives hours after the nudge.
    (state / "stamp").write_text("0\n", encoding="utf-8")

    again = _run_report_watch(tmp_path, [FILED.format(ts=2_000_000_000)],
                              start_epoch=1_500_000_000, state=state)
    assert again == 1, "the replay must add NO further nudge"


def test_a_DEBOUNCED_report_replayed_later_is_SILENT(tmp_path):
    """⚡ THE HOLE IN A ONE-VALUE DESIGN — which is why the mark advances whether
    or not a notification was sent. The second report here is swallowed by the
    cooldown, so nothing was sent for it; if that is not recorded it stays
    eligible for ever and a later trim replay nudges as if it were new."""
    state = tmp_path / "run"
    first = _run_report_watch(
        tmp_path,
        [FILED.format(ts=2_000_000_000),   # nudges, sets stamp + high-water
         FILED.format(ts=2_000_000_060)],  # inside the cooldown: NOT sent
        start_epoch=1_500_000_000, state=state)
    assert first == 1, "the burst must produce exactly one nudge"
    assert (state / "high").read_text().strip() == "2000000060", (
        "the swallowed report must still advance the high-water mark")

    (state / "stamp").write_text("0\n", encoding="utf-8")
    again = _run_report_watch(tmp_path, [FILED.format(ts=2_000_000_060)],
                              start_epoch=1_500_000_000, state=state)
    assert again == 1, "the never-sent report must NOT nudge on replay"


def test_the_NUDGE_CARRIES_NOTHING_a_real_report_said(tmp_path):
    """⛔⛔ THE ONE THAT MATTERS. A real report is filed through the real command,
    with a distinctive sentinel as its text and another as the accused's name —
    then the watcher runs over the REAL events file the server wrote, and every
    byte the notifier was handed is grepped for both.

    ⚠ The report is filed for real rather than hand-written into the log,
    because the question is not "does this script quote the event" but "can
    anything a resident typed reach a phone" — and only the real path can
    answer that."""
    import sys
    sys.path.insert(0, str(REPO))
    from heresay.config import Config
    from heresay.server import TalkerServer
    # ⚠ `conftest`, not `tests.conftest`: pytest already has this file's
    # directory on the path, and importing it under a second name runs the
    # module a SECOND time. The generic tree's conftest repoints a dataclass
    # default there, which is not idempotent — it failed on exactly this line.
    from conftest import FakeSession, seed_account               # noqa: F401
    from heresay.commands import dispatch

    said = "quintuple-marmalade-sentinel"
    accused = "Zarquon"

    cfg = Config(
        owner_name="Generic",
        accounts_file=str(tmp_path / "accounts.json"),
        mail_file=str(tmp_path / "mailboxes.json"),
        mail_key_file=str(tmp_path / "mail.key"),
        revtell_file=str(tmp_path / "revtells.json"),
        bans_file=str(tmp_path / "bans.json"),
        state_file=str(tmp_path / "state.json"),
        permissions_file=str(tmp_path / "permissions.toml"),
        events_file=str(tmp_path / "events.jsonl"),
        world_dir=str(REPO / "tests" / "fixtures" / "house" / "world"),
        entry_room="atrium", jail_room="holding_cell",
        wrap_columns=1000,
    )
    srv = TalkerServer(cfg)
    seed_account(srv.accounts, "Corvid", "password-1234")
    seed_account(srv.accounts, accused, "password-1234")
    s = FakeSession(srv.world, name="Corvid", events=srv.events, mail=srv.mail,
                    state=srv.state, accounts=srv.accounts, config=cfg)
    dispatch(s, f".report {accused} {said}")

    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert '"type":"report_filed"' in raw, "the report must have been filed"
    assert said not in raw and accused not in raw, "the EVENT already leaks"

    # Now the watcher, over the file the server actually wrote.
    state = tmp_path / "watch"
    state.mkdir()
    calls = state / "calls"
    stub = state / "notify.sh"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {calls}\n',
                    encoding="utf-8")
    stub.chmod(0o755)
    import os
    import subprocess
    subprocess.run(
        ["bash", str(REPORT_WATCH)],
        env=dict(os.environ, NOTIFY=str(stub), REPORT_TAIL="cat",
                 REPORT_LOG=str(tmp_path / "events.jsonl"),
                 REPORT_STAMP=str(state / "stamp"),
                 REPORT_HIGHWATER=str(state / "high"),
                 REPORT_START="0"),
        check=True, timeout=15)

    pushed = calls.read_text(encoding="utf-8")
    assert pushed.strip(), "the real report must have nudged"
    assert said not in pushed
    assert accused not in pushed
    assert "Corvid" not in pushed              # nor the REPORTER
    assert ".read_report" in pushed            # it does point at the reader


# ===========================================================================
# ⛔ THE HOUSE IS CONTENT — code deploys must not carry it (v1.59.0, the split)
# ===========================================================================

def test_deploy_EXCLUDES_the_house_AND_PROTECTS_it_from_delete_excluded():
    """⛔⛔ BOTH LINES, AND THE SECOND IS THE ONE THAT MATTERS.

    The engine tree ships a SKELETON house (`world/rooms/void.txt` and the
    templates), so once the engine and the instance live in separate trees a
    code deploy without this would replace a live house with the skeleton.

    ⚠ And the obvious fix is the destructive one: `--delete-excluded` deletes
    excluded paths AT THE DESTINATION, so `--exclude '/house/'` alone does not
    spare the live house — it DELETES it. Measured both ways with real rsync
    before the line was written. The `protect` filter is what keeps the server's
    house; the exclude is what stops the skeleton being sent. Neither is
    sufficient alone, so neither may be removed alone.
    """
    text = DEPLOY.read_text(encoding="utf-8")
    assert "--exclude '/house/'" in text, "the skeleton must not be SENT"
    assert "--filter='protect /house/'" in text, (
        "without this, --delete-excluded DELETES the live house")
    # The pair must sit inside the EXCLUDES array that the rsync actually uses.
    # ⚠ The array's terminator is a `)` ALONE ON A LINE — the first `)` after
    # `EXCLUDES=(` is inside the comment above the entries, and slicing to that
    # cut the block off before the lines this is checking for.
    after = text[text.index("EXCLUDES=("):]
    block = after[:after.index("\n)")]
    assert "--exclude '/house/'" in block and "--filter='protect /house/'" in block
    # ⚠ And --delete-excluded is still there: if it ever goes, the protect
    # filter stops being load-bearing and this test's reasoning changes.
    assert "--delete-excluded" in text


def test_deploy_content_is_the_ONLY_route_house_content_takes():
    """Content reaches the server by `deploy-content.sh` and nothing else — it
    rsyncs the script's OWN repo's `house/`, which is what lets the private
    instance tree push its house with no engine involvement at all."""
    content = (REPO / "deploy" / "deploy-content.sh").read_text(encoding="utf-8")
    assert '"$REPO/house/" "$HOST:$DEST/"' in content
    assert 'DEST="/opt/heresay/house"' in content
