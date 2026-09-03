# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The browser client's behavioural harness, run from the Python suite.

`scripts/tabtest.js` executes the REAL `web/index.html` `<script>` in Node
against a minimal DOM stub — the shipped code, not a copy — and drives the
actual `ws.onmessage` handler, the compose path, and the load-time branches. It
is the only automated coverage the web client has, and until now it lived on a
bridge directory outside version control, which meant it was only ever run when
someone remembered to.

**Node is optional.** The engine has exactly three runtime dependencies and Node
is not one of them; the suite must pass on a machine that has never installed
it. So every test here SKIPS cleanly when `node` is missing or too old, rather
than failing. A skip is honest — it says the client went unchecked — while a
failure would say the client is broken, which would be a lie.

Each of the harness's modes is one test, because a mode is one question asked
once per page load (a real page cannot be asked "what do you do with nothing
cached?" twice). Their expected check counts are pinned: a mode that silently
stopped running its checks would otherwise still exit 0 and look green.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "scripts" / "tabtest.js"

# Node 18 is the floor: the harness uses `String.replaceAll`, optional chaining
# and modern regex features, and 18 is the oldest release still remotely current.
NODE_MIN_MAJOR = 18

# One entry per harness mode: (id, env overrides, expected checks).
#
# Update a count here when you add checks to that mode — deliberately, because a
# mode whose checks quietly vanished would still exit 0. The ids are what pytest
# prints, so a failure names the mode without anyone reading this table.
MODES = [
    ("main", {}, 87),
    ("width", {"WIDTH": "1"}, 15),
    ("inworld", {"INWORLD": "1"}, 15),
    ("snap", {"SNAP": "1"}, 9),
    ("resume_none", {"RESUME": "1"}, 14),
    ("resume_stored", {"RESUME": "1", "SAVED_RESUME": "kept-token-abc"}, 13),
    ("master_off", {"MASTER_OFF": "1"}, 6),
    ("title_first_visit", {"TITLE_INIT": "1"}, 3),
    ("title_cached", {"TITLE_INIT": "1", "SAVED_TITLE": "Commons"}, 4),
    ("raise_first_visit", {"RAISE_INIT": "1", "EXPECT_RAISE": "1"}, 4),
    ("raise_saved_zero",
     {"RAISE_INIT": "1", "SAVED_RAISE": "0", "EXPECT_RAISE": "0"}, 4),
]

TOTAL_CHECKS = sum(n for _, _, n in MODES)


def _node() -> str:
    """The node executable, or a skip. Never a failure: see the module docstring."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("node is not installed — the web client harness cannot run "
                    "(the engine itself does not need Node)")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"could not run node --version ({type(exc).__name__})")
    m = re.match(r"v(\d+)\.", out)
    if not m:
        pytest.skip(f"could not read a version from node --version ({out!r})")
    if int(m.group(1)) < NODE_MIN_MAJOR:
        pytest.skip(f"node {out} is older than v{NODE_MIN_MAJOR} — "
                    "the harness needs a more current runtime")
    return exe


def _run(env_overrides: dict) -> subprocess.CompletedProcess:
    """One harness run. The mode rides in the environment, exactly as it does
    when a person runs it by hand."""
    env = dict(os.environ)
    env.update(env_overrides)
    # Run from the repo root, but the harness resolves the client from its own
    # location anyway — so this is for tidy relative paths in any output, not
    # for correctness.
    return subprocess.run([_node(), str(HARNESS)], capture_output=True, text=True,
                          cwd=REPO, env=env, timeout=300)


def _checks_reported(stdout: str) -> int:
    """The count from the harness's closing line, or -1 if it never printed one."""
    m = re.search(r"ALL (\d+) CHECKS PASSED", stdout)
    return int(m.group(1)) if m else -1


def test_the_harness_is_in_the_repo():
    """It used to live on a bridge directory outside version control, which meant
    a change to the client could only be checked by someone who happened to have
    that directory. This is the test that says: never again."""
    assert HARNESS.is_file(), f"{HARNESS} is missing"


def test_the_harness_parses():
    node = _node()
    r = subprocess.run([node, "--check", str(HARNESS)], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("mode,env,expected",
                         [pytest.param(m, e, n, id=m) for m, e, n in MODES])
def test_web_client_harness_mode(mode, env, expected):
    r = _run(env)
    # Surface the harness's own output on failure: it names the exact check that
    # broke, which is far more use than an exit code.
    assert r.returncode == 0, (
        f"web harness mode {mode!r} failed (exit {r.returncode})\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    )
    got = _checks_reported(r.stdout)
    assert got == expected, (
        f"web harness mode {mode!r} ran {got} checks, expected {expected}. "
        "If you added or removed checks, update MODES in this file — the count "
        "is pinned so a mode that silently stopped checking cannot pass.\n"
        f"--- stdout ---\n{r.stdout}"
    )


# ---------------------------------------------------------------------------
# The skip path itself. The promise is "the suite still passes on a machine
# without Node", and a promise nobody exercises is a hope — so these three
# assert it directly rather than relying on someone happening to have a
# Node-less machine to try it on.
# ---------------------------------------------------------------------------

def test_a_missing_node_skips_rather_than_fails(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(pytest.skip.Exception) as exc:
        _node()
    assert "not installed" in str(exc.value)


def test_an_ancient_node_skips_rather_than_fails(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v12.22.9\n",
                                                    stderr=""))
    with pytest.raises(pytest.skip.Exception) as exc:
        _node()
    assert "older than" in str(exc.value)


def test_an_unrunnable_node_skips_rather_than_fails(monkeypatch):
    """A `node` on PATH that cannot actually be executed (a broken symlink, a
    wrapper that dies) must not fail the Python suite either."""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/node")

    def _boom(*a, **k):
        raise OSError("exec format error")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(pytest.skip.Exception):
        _node()


def test_every_harness_check_runs_under_pytest():
    """The whole point of landing it here: the full 123, not just the default
    mode, go through the ordinary suite."""
    total = 0
    for mode, env, _expected in MODES:
        r = _run(env)
        assert r.returncode == 0, f"mode {mode!r} failed:\n{r.stdout}\n{r.stderr}"
        total += _checks_reported(r.stdout)
    assert total == TOTAL_CHECKS, (
        f"{total} harness checks ran across {len(MODES)} modes, expected "
        f"{TOTAL_CHECKS}"
    )


# ---------------------------------------------------------------------------
# The viewport meta tag (v1.44.1)
# ---------------------------------------------------------------------------
#
# ⚠ NOT a harness mode, and it cannot be: the harness runs the page's <script>,
# and this is a declaration in the <head> that only a real browser acts on. A
# string check is genuinely all that is unit-testable here — the confirmation
# that matters is the maintainer looking at Edge on the Pixel. What the check IS for is
# that the attribute is not lost in a later edit of the tag.

CLIENT = REPO / "web" / "index.html"


def _viewport_tag() -> str:
    for line in CLIENT.read_text(encoding="utf-8").splitlines():
        if 'name="viewport"' in line:
            return line
    raise AssertionError("web/index.html has no viewport meta tag")


def test_the_keyboard_shrinks_the_LAYOUT_viewport_on_android():
    """⛔ Without this, `100dvh` stays full-height while the on-screen keyboard
    covers the bottom of it — so the bottom-pinned input sits above the keyboard
    with a strip of dead page between them. Android's default is
    `resizes-visual`, which shrinks only the VISUAL viewport and leaves `dvh`
    alone; `resizes-content` shrinks the layout viewport, which is what `dvh`
    measures."""
    assert "interactive-widget=resizes-content" in _viewport_tag()


def test_the_rest_of_the_tag_is_untouched():
    """A one-attribute change: the width, the scale and the notch handling are
    all still there."""
    tag = _viewport_tag()
    for part in ("width=device-width", "initial-scale=1", "viewport-fit=cover"):
        assert part in tag, part


def test_the_visualViewport_fallback_STAYS_for_ios():
    """⚠ iOS/WebKit IGNORES `interactive-widget` and stays `resizes-visual`, so
    the meta tag does NOT replace the fallback — it fixes the Android path the
    fallback was awkwardly for. Removing the fallback because "the meta tag
    handles it now" would break exactly the browser the tag does not reach."""
    src = CLIENT.read_text(encoding="utf-8")
    assert "VIEWPORT" in src and "visualViewport" in src
    assert "enabled: false" in src          # ...and still off by default
