#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Push a phone alert via ntfy. Reads NTFY_URL from /etc/heresay/notify.env.
# Usage: heresay-notify.sh <priority> <title> <message>
#
# Installs to /usr/local/bin/heresay-notify.sh (0755) — OUTSIDE /opt/heresay, so
# `deploy.sh`'s rsync --delete can never reach it and a code push never disturbs
# the alerting. See the install README.
#
# ⛔ IT NEVER FAILS ITS CALLER. Every path exits 0: no env file, no NTFY_URL, a
# curl that times out, ntfy.sh being down. This is called from `OnFailure=` and
# from `ExecStartPost=`, so a notifier that returned non-zero would mark the very
# unit it was reporting on as failed — turning one problem into two, and the
# second one into a loop. A missed buzz is a smaller failure than a cascade.
set -uo pipefail
ENV_FILE="/etc/heresay/notify.env"
[ -r "$ENV_FILE" ] || { echo "notify: no $ENV_FILE; skipping" >&2; exit 0; }
# shellcheck source=/dev/null
. "$ENV_FILE"
[ -n "${NTFY_URL:-}" ] || { echo "notify: NTFY_URL unset; skipping" >&2; exit 0; }
curl -fsS --max-time 10 \
  -H "Title: ${2:-Heresay}" \
  -H "Priority: ${1:-max}" \
  -H "Tags: rotating_light" \
  -d "${3:-alert}" \
  "$NTFY_URL" >/dev/null 2>&1 || echo "notify: post failed" >&2
exit 0
