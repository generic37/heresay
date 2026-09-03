# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Every source file we author carries the SPDX licence line.

⛔ **DERIVED, NOT A LIST.** A hand-maintained roster of files ships whatever it
forgot — the same failure that put `docs/CODE_AUDIT.md` on the production host
until v1.55.1. So the scope here is a RULE over what git tracks, and every
exclusion is a property of the path rather than a name somebody remembered to
add.

The rule, in three parts:

  * a **suffix** we write code in (`.py`, `.sh`, `.js`, `.ps1`), under a
    **root** we author (`heresay/`, `tests/`, `deploy/`, `tools/`, `scripts/`);
  * plus the two authored files that live at a path of their own — `Makefile`
    and `web/index.html`;
  * and nothing else.

⚠ **What that rule excludes, and why it needs no exclusion list:**

  * ⛔ **`web/` third-party assets.** IBM Plex Mono and `web/OFL.txt` are **SIL
    OFL 1.1**, not ours. Only `web/index.html` is named, so the font and its
    licence are outside the rule by construction — and an AGPL header on
    somebody else's font would be a false licence claim, which is worse than no
    header at all.
  * **`house/` and `skel/house/`** — authored PROSE. Rooms, help pages, the
    agreement, the greeting. Operator content, not code, and not under an
    authored root.
  * **Data and config** — `.json`, `.toml`, `.txt`, `LICENSE`, `.gitignore`.
    Excluded by suffix, not by name.
  * **Anything generated or untracked** — the scan is `git ls-files`, so a build
    product cannot fail this test and a stray scratch file cannot either.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

SPDX = "SPDX-License-Identifier: AGPL-3.0-or-later"
COPYRIGHT = "Copyright (C) 2026 BigPoetry"

#: Languages this project is written in.
AUTHORED_SUFFIXES = {".py", ".sh", ".js", ".ps1"}
#: Directories whose source is ours.
AUTHORED_ROOTS = {"heresay", "tests", "deploy", "tools", "scripts"}
#: Authored files that are not under an authored root. ⚠ `web/` is deliberately
#: NOT a root: the font beside this file belongs to somebody else.
AUTHORED_FILES = {"Makefile", "web/index.html"}


def _tracked():
    """Every file in the tree — from git where there is one, by walking where
    there is not.

    ⛔ THE FALLBACK IS LOAD-BEARING. `git ls-files` answers nothing in a tree
    that is not a repository — an unpacked tarball, or the builder's output — and
    an empty answer made this file's own guard the only thing that noticed. A
    licence sweep that silently sweeps zero files is worse than no sweep."""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, check=True,
                             capture_output=True, text=True).stdout
        tracked = [line for line in out.splitlines() if line]
    except (subprocess.CalledProcessError, FileNotFoundError):
        tracked = []
    if tracked:
        return tracked
    skip = {"__pycache__", ".git", ".venv", ".venv-dev", ".audit-venv",
            "devstate", "dist-generic", "backups", "certs", ".pytest_cache"}
    found = []
    for p in REPO.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(REPO)
        if any(part in skip for part in rel.parts):
            continue
        found.append(str(rel))
    return sorted(found)


def in_scope(rel: str) -> bool:
    """The rule itself, exposed so a reader can check their own file."""
    if rel in AUTHORED_FILES:
        return True
    p = Path(rel)
    return p.suffix in AUTHORED_SUFFIXES and p.parts[0] in AUTHORED_ROOTS


def scope():
    return sorted(r for r in _tracked() if in_scope(r))


def test_the_scope_rule_finds_the_source_tree():
    """⚠ A guard on the guard: if the rule ever matches almost nothing, the test
    below would pass vacuously and prove nothing at all."""
    found = scope()
    assert len(found) > 100, f"the scope rule matched only {len(found)} files"
    # the shapes it must reach, each by a different clause of the rule
    assert "heresay/server.py" in found          # authored root + suffix
    assert "deploy/deploy.sh" in found           # nested authored root
    assert "scripts/tabtest.js" in found         # a suffix that is not .py/.sh
    assert "Makefile" in found                   # named file, no suffix
    assert "web/index.html" in found             # named file beside third-party


def test_every_authored_source_file_carries_the_SPDX_line():
    """⛔ The licence claim has to be ON the file. A repo-root `LICENSE` says what
    the project is under; a header says what THIS file is under, and a file that
    travels on its own — pasted, vendored, quoted in a bug report — takes its
    header with it and nothing else."""
    missing = [r for r in scope()
               if SPDX not in (REPO / r).read_text(encoding="utf-8")]
    assert not missing, (
        "source files with no SPDX licence header:\n  " + "\n  ".join(missing)
        + "\n\nAdd, after any shebang and before the module docstring:\n"
        f"  # {COPYRIGHT}\n  # {SPDX}\n"
        "(`//` for JavaScript, `<!-- -->` for HTML.)"
    )


def test_the_copyright_holder_is_the_same_everywhere():
    """⚠ One holder, or the licence grant is ambiguous about who is granting it.
    `LICENSE` and `README.md` name it too; this covers the headers."""
    wrong = []
    for r in scope():
        text = (REPO / r).read_text(encoding="utf-8")
        if SPDX in text and COPYRIGHT not in text:
            wrong.append(r)
    assert not wrong, f"SPDX line without the matching copyright holder: {wrong}"


def test_third_party_assets_carry_no_licence_claim_of_ours():
    """⛔ The mirror of the rule, and the one that would do real damage.

    `web/OFL.txt` and the font it covers are **SIL OFL 1.1**. Putting an AGPL
    header on either would be claiming somebody else's work — a false licence
    claim is worse than a missing header, so this asserts the absence."""
    for rel in ("web/OFL.txt",):
        text = (REPO / rel).read_text(encoding="utf-8", errors="replace")
        assert SPDX not in text, f"{rel} is not ours to license"
        assert not in_scope(rel), f"{rel} must not be in the header scope"
