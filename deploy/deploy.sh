#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Deploy the heresay to the production server.
#
# One idempotent command: rsync the repo to /opt/heresay (never touching runtime
# state), sync the deps, and restart the service. Safe to re-run any time.
#
# The target host is REQUIRED via DEPLOY_HOST (an SSH host alias or user@host):
#
#   DEPLOY_HOST=myhost ./deploy/deploy.sh
#
# ...or, so a regular operator need not type it each time, create a git-ignored
# deploy/local.env containing:  DEPLOY_HOST=<ssh-host>
# The script sources it only when DEPLOY_HOST is not already set, so an explicit
# environment value always wins. There is NO built-in default host: an unset
# DEPLOY_HOST is a hard error, before anything is executed.
#
# NEVER uploads runtime state — accounts/mail/key/bans/state live only on the
# server, under /var/lib/heresay, and are excluded below. Production config lives
# at /etc/heresay/config.toml on the server and is likewise never overwritten.
# Dev-only files (CLAUDE.md, .git, tests, config.toml, config.dev.toml) are also
# excluded, and --delete-excluded removes any copies a past deploy left on the
# server. All rsync deletion is confined to the destination (/opt/heresay); it can
# never reach /var/lib/heresay or /etc/heresay, which are separate directories and
# not part of the transfer.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve the deploy host. An explicit DEPLOY_HOST in the environment wins;
# otherwise the optional, git-ignored deploy/local.env may supply it. No default.
ENV_FILE="$REPO/deploy/local.env"
if [ -z "${DEPLOY_HOST:-}" ] && [ -f "$ENV_FILE" ]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
fi
if [ -z "${DEPLOY_HOST:-}" ]; then
  echo "deploy: DEPLOY_HOST is not set. Set it (e.g. 'DEPLOY_HOST=myhost make deploy') or create deploy/local.env containing 'DEPLOY_HOST=<ssh-host>'." >&2
  exit 1
fi

HOST="$DEPLOY_HOST"
DEST="/opt/heresay"

# Excluded from the upload: local venv, throwaway/dev state, TLS material, EVERY
# runtime state file (these belong to the server and must never be clobbered by a
# push from a developer's checkout), and dev-only files that have no business in
# production. --delete-excluded (below) also removes any of these that an earlier
# deploy left on the server. The dev-file patterns are ANCHORED (leading /) so
# they match only at the repo root — deploy/install/config.example.toml, for one, still ships.
EXCLUDES=(
  # ⛔⛔ THE HOUSE IS CONTENT AND THIS SCRIPT CARRIES CODE (v1.59.0, the split).
  #
  # `make deploy` and `make deploy-content` have always meant two different
  # things; until the engine and the house lived in separate trees, nothing
  # made that TRUE — this rsync carried house/ along with the code, and it
  # happened not to matter because the house in the oak was the house on the
  # server. It is not any more: the ENGINE oak ships a skeleton house
  # (world/rooms/void.txt and the templates), so without this line a code
  # deploy from the engine repo would REPLACE a live house with the skeleton —
  # every room, the map, the greeting, the agreement and the words, gone, and
  # the daemon left standing in the void.
  #
  # ⚠ THE `protect` FILTER IS NOT OPTIONAL AND THE EXCLUDE ALONE IS WORSE THAN
  # NOTHING. `--delete-excluded` (below) deletes excluded paths AT THE
  # DESTINATION, so `--exclude '/house/'` on its own does not spare the live
  # house — it DELETES it. Measured, both ways, before this line was written;
  # `tests/test_deploy_scripts.py` pins the pair together for exactly that
  # reason. Content reaches the server by `deploy-content.sh` and by nothing
  # else.
  --exclude '/house/'
  --filter='protect /house/'
  --exclude '/deploy/local.env'
  --exclude '.venv/'
  # The monthly dependency scan's own venv (heresay-depscan.sh). It lives beside
  # the daemon's, is not in the repo, and would therefore be DELETED by
  # --delete-excluded on every push — rebuilt on the next run, so nothing breaks,
  # but rebuilt monthly for no reason. ⚠ `.venv/` above does not cover it: that
  # pattern matches a directory named exactly `.venv`.
  --exclude '.audit-venv/'
  --exclude 'devstate/'
  --exclude 'certs/'
  --exclude '__pycache__/'
  --exclude '*.pyc'
  --exclude '.pytest_cache/'
  --exclude 'accounts.json'
  --exclude 'mailboxes.json'
  --exclude 'mail.key'
  --exclude 'revtells.json'
  --exclude 'bans.json'
  --exclude 'state.json'
  # ⛔ v1.58.0 — the report CAPTURE files. They live in the state dir in
  # production (/var/lib/heresay/reports), which is nowhere near this rsync; the
  # line is here for the DEV case, where a repo-root config puts the state dir at
  # the repo root and a stray capture would otherwise be pushed to the server.
  # Same reasoning as the runtime files above it, and the same consequence if it
  # were missing: an accusation in plaintext, copied to a machine it was not
  # written on.
  --exclude 'reports/'
  --exclude 'backups/'
  # Dev-only files kept out of /opt/heresay (and cleaned up by --delete-excluded):
  --exclude '/CLAUDE.md'
  # The local dev-tool state directory, and any git worktrees under it. Never in
  # the repo, and it has no business on the runtime host — ⚠ it was reaching it,
  # because an untracked directory in the working oak is still something rsync
  # copies. --delete-excluded sweeps the existing /opt/heresay/.claude on the
  # next push; nothing has to be removed by hand.
  --exclude '/.claude/'
  --exclude '/.git'
  --exclude '/.gitignore'
  --exclude '/scripts'
  --exclude '/Makefile'
  --exclude '/pyproject.toml'
  --exclude '/tests'
  --exclude '/config.toml'
  --exclude '/config.dev.toml'
  # A developer's own permissions.toml, which sits beside their config.toml at
  # the repo root. The SERVER's lives beside /etc/heresay/config.toml — outside
  # this rsync entirely — so --delete-excluded can only remove a stale copy that
  # an earlier deploy left in /opt/heresay, where nothing reads it.
  --exclude '/permissions.toml'
  --exclude '/deploy/Caddyfile.dev'
  # ⛔ THE PRIVATE DOC SET, and it is a BLOCKLIST: a private doc that is not
  # named here is rsynced onto the production host. That is not a hypothetical —
  # `docs/CODE_AUDIT.md` was missing from this list and was being copied to
  # /opt/heresay on every push until v1.55.1. `tests/test_deploy_scripts.py`
  # now classifies EVERY file in docs/ as public or private and fails on an
  # unclassified one, so the next doc cannot ship by being forgotten.
  # ⚠ docs/OPERATIONS.md, CONFIGURING.md, COMMANDS.md and DEVELOPMENT.md are
  # PUBLIC and deploy as usual. --delete-excluded also
  # sweeps any stale copy an earlier deploy left on the box.
  #
  # The private audit ledger: a map of what has NOT been examined and what is
  # still open.
  --exclude '/docs/SECURITY_CHECKLIST.md'
  # The DIY security-testing runbook: what to attack and where the bounds are.
  # A development note, and not a map to hand the runtime host.
  --exclude '/docs/PENTEST.md'
  # The run-by-hand security tooling (fuzzer + load harness). Test tooling, and
  # nothing that points at parser bounds belongs on the runtime host.
  --exclude '/scripts/pentest/'
  # Version-migration history: what an operator does when crossing releases.
  # A private operator note, not part of the shipped manual.
  --exclude '/docs/UPGRADING.md'
  # Forward work, and the version history it was split from. Private planning
  # notes: they name what is not built yet and what went wrong on the way, which
  # is nobody's business on the runtime host.
  --exclude '/docs/ROADMAP.md'
  --exclude '/docs/CHANGELOG.md'
  # Standing positions — what we deliberately do NOT do, and why. Private
  # planning of the same class as the two above. ⚠ This list is a BLOCKLIST: a
  # private doc that is not named here is rsynced onto the production host.
  --exclude '/docs/POLICY.md'
)

echo ">> rsync $REPO/ -> $HOST:$DEST/"
rsync -az --delete --delete-excluded "${EXCLUDES[@]}" "$REPO/" "$HOST:$DEST/"

# ⛔ THE DEPENDENCIES ARE PINNED AND HASH-VERIFIED (v1.48.0, audit G2).
#
# This step used to be `pip install argon2-cffi websockets cryptography` —
# unpinned, unhashed, re-resolving from PyPI as root on the production host on
# every deploy. A compromised release or a maintainer-account takeover landed
# here on the next unrelated push, and what was actually running could not be
# read off the checkout at all.
#
# ⚠ `--require-hashes` is all-or-nothing across one install command, which is
# why the pip self-upgrade stays a separate line, and why requirements.txt
# carries the full transitive closure rather than just the three direct deps.
#
# ⚠ RUNTIME ONLY. requirements-dev.txt is never installed here; a test runner on
# a live host is code running as the daemon's user for no reason anyone can
# state.
echo ">> sync venv + deps on $HOST (pinned, hash-verified)"
ssh "$HOST" bash -euo pipefail <<'REMOTE'
  cd /opt/heresay
  if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
  fi
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet --require-hashes -r requirements.txt
  chown -R heresay:heresay /opt/heresay/.venv
REMOTE

# ⛔ permissions.toml is CONFIG, in /etc, and the daemon may not write it — the
# hardened unit mounts /etc read-only, and a service that must be able to
# rewrite its own configuration is one whose configuration is not protected. So
# the file is created and topped up HERE, as root, BEFORE the restart: a new
# release's commands get their lines now, rather than the daemon trying (and, in
# v1.37.0, crash-looping on Errno 30). Idempotent — an already-complete file is
# left byte-identical, and an operator's values and comments are never touched.
# Safe to run by hand at any time — ⚠ FROM /opt/heresay, see below:
#     cd /opt/heresay && sudo /opt/heresay/.venv/bin/python -m heresay \
#         -c /etc/heresay/config.toml --write-permissions
#
# ⚠ THE `cd` IS LOAD-BEARING. The venv holds the three DEPENDENCIES only — the
# `heresay` package itself is never installed into it (deliberately: what is
# deployed is the code, not a copy of it), so `-m heresay` resolves purely
# because the working directory is on sys.path. The systemd unit gets that from
# `WorkingDirectory=/opt/heresay`; an `ssh host '<cmd>'` starts in the login
# user's HOME, which is why v1.37.1's deploy died on `No module named heresay`.
# This line mirrors the unit exactly: same interpreter, same working directory,
# same `-c`.
#
# ⛔ AND IT MUST ABORT THE DEPLOY IF IT FAILS (v1.43.0). Since permissions.toml
# is names-only, a value that is not a rank REFUSES THE BOOT — so this step is
# where such a file has to be caught: here, with the old code still serving,
# rather than at the restart below as a house that will not open. `set -e`
# already stops the script on a non-zero exit; the explicit check is here to say
# WHY, because "deploy stopped" and "your permissions file is wrong" are not the
# same message to wake up to. Nothing is restarted after this line fails.
echo ">> permissions.toml on $HOST (written as root; never by the daemon)"
if ! ssh "$HOST" 'cd /opt/heresay && /opt/heresay/.venv/bin/python -m heresay -c /etc/heresay/config.toml --write-permissions'; then
  echo "deploy: --write-permissions FAILED on $HOST. Nothing has been restarted;" >&2
  echo "        the old code is still running. Fix /etc/heresay/permissions.toml" >&2
  echo "        (every value must be a rank name from theme.toml [ranks] order)" >&2
  echo "        and deploy again." >&2
  exit 1
fi

# ⚡ GIVE THE HOUSE ITS WARNING (v1.46.0). The engine is the only thing holding
# the connections, so the countdown has to be broadcast BY it; this asks it to,
# over its local control socket, and BLOCKS until the last checkpoint. Marks
# come from `[world] restart_checkpoints` (default 120/60/10 seconds).
#
# ⚠ ORDER IS THE POINT: rsync, then --write-permissions (which aborts the deploy
# on a bad file, with the OLD code still serving), then the countdown, then the
# restart. Warning people and THEN discovering the permissions file is broken
# would have announced a restart that never came.
#
# Never fatal: an older daemon has no control socket, and a deploy that refused
# to proceed because it could not say goodbye would be worse than a quiet one.
echo ">> warning the house on $HOST (blocks for the countdown)"
ssh "$HOST" 'cd /opt/heresay && /opt/heresay/.venv/bin/python -m heresay -c /etc/heresay/config.toml --restart-countdown' \
  || echo "   (no countdown — control socket unavailable; restarting anyway)" >&2

echo ">> restart heresay on $HOST"
ssh "$HOST" 'systemctl restart heresay && sleep 1 && systemctl is-active heresay'

echo ">> deployed. recent log:"
ssh "$HOST" 'journalctl -u heresay -n 8 --no-pager'
