#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Push house/ to the server and let the RUNNING engine pick it up. No restart.
#
# ⛔ THE WHOLE POINT IS THAT IT DOES NOT RESTART. Since v1.39.0 the daemon
# watches its content and reloads what changes, so a room's prose, a help page,
# a greeting or the house's words can be edited in the repo, pushed, and be live
# a few seconds later — with nobody disconnected, nobody moved, no locks
# dropped, no topics lost and the entry room's memory intact. Restarting to
# change a paragraph is what this replaces.
#
# Use `make deploy` for CODE. Code is not watched and never will be: swapping a
# running Python process's own modules under it is a different and far riskier
# feature than re-reading a text file.
#
# What lands live, within `[world] reload_poll_seconds` (3):
#   house/world/rooms/*.txt   the rooms          (see the occupied-room rule)
#   house/world/map.txt       the map            (+ .narrow)
#   house/world/echoes.txt    the atmospherics
#   house/greeting.txt        the login banner   (+ .narrow)
#   house/help/*.txt          the help pages
#   house/reboot.txt          the .reboot script
#   house/theme.toml          the house's words + favicon  (NOT [ranks] —
#                             a rank change still needs a restart)
#   house/agreement.txt       the terms — new version live at the door, and
#                             everyone re-consents at their NEXT login
#
# ⚠ Same --delete semantics as `make deploy`: house/ on the server is made to
# match the repo, so a file deleted here is deleted there. The repo is the
# durable source for content; an edit made ON the server survives only until the
# next push of either kind.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Same host resolution as deploy.sh: DEPLOY_HOST, else deploy/local.env, else
# refuse. Guessing which machine to write to is not a thing a deploy script does.
if [[ -z "${DEPLOY_HOST:-}" && -f "$REPO/deploy/local.env" ]]; then
  # shellcheck disable=SC1091
  source "$REPO/deploy/local.env"
fi
if [[ -z "${DEPLOY_HOST:-}" ]]; then
  echo "DEPLOY_HOST is not set (env, or deploy/local.env). Refusing to guess." >&2
  exit 1
fi
HOST="$DEPLOY_HOST"
DEST="/opt/heresay/house"

echo ">> rsync $REPO/house/ -> $HOST:$DEST/   (no restart)"
rsync -az --delete \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$REPO/house/" "$HOST:$DEST/"

echo ">> pushed. The running engine reloads it within reload_poll_seconds."
echo "   Check what it made of it, in-world:  .load_report"
echo "   Or from here:"
ssh "$HOST" 'journalctl -u heresay -n 12 --no-pager | grep -i reload || true'
