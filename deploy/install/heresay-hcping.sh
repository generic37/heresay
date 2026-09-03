#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Ping Healthchecks.io for the backup dead-man's-switch.
# Reads HC_BACKUP_URL from /etc/heresay/notify.env. Arg: success (default)|fail|start.
#
# ⚠ THE POINT IS THE SILENCE, not the ping. Every other alert here fires when
# something goes wrong; this one fires when something goes RIGHT, and the alarm
# is Healthchecks noticing that it stopped arriving. That is the only shape of
# monitoring that catches a backup timer which was quietly disabled, or a box
# that is off — neither of which can send you anything.
#
# Installs to /usr/local/bin/heresay-hcping.sh (0755); exits 0 on every path for
# the same reason heresay-notify.sh does — it runs as the backup's
# ExecStartPost, and a non-zero here would fail a backup that had just succeeded.
set -uo pipefail
# shellcheck source=/dev/null
. /etc/heresay/notify.env 2>/dev/null || exit 0
[ -n "${HC_BACKUP_URL:-}" ] || { echo "hcping: HC_BACKUP_URL unset; skipping" >&2; exit 0; }
url="$HC_BACKUP_URL"
case "${1:-success}" in
  fail)  url="$url/fail" ;;
  start) url="$url/start" ;;
esac
curl -fsS --max-time 10 --retry 3 "$url" >/dev/null 2>&1 || echo "hcping: ping failed" >&2
exit 0
