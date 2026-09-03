#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Daily lightweight checks. Push ONLY on a breach; silent when healthy.
# Installs to /usr/local/bin/heresay-healthcheck.sh (0755), run daily by
# heresay-healthcheck.timer.
#
# ⚠ SILENT WHEN HEALTHY is the whole design. An alert that arrives every day is
# an alert nobody reads by the second week, and everything checked here is
# slow-moving: a disk fills over months, the state store grows over months, a
# cert expires on a schedule. If this buzzes, something is actually wrong.
#
# ⛔ THE STATE-SIZE CHECK IS THE ANSWER TO AUDIT-3 F3. accounts.json and
# ban_history.json grow unbounded IN PRINCIPLE — but they are durable records
# (who has an account; every ban ever placed, for recidivism), and a size cap on
# a durable record trades real function to bound a number that is fine almost
# always. So it is a WATCH, not a cap: two triggers on the same filesystem — free
# space AND the state-dir size — and the alert names accounts.json and
# ban_history.json by size, so a signup flood reads as "the store exploded"
# rather than a bare "disk filling". The size trigger catches growth on a big
# disk before free space even moves.
set -uo pipefail
# Overridable only so a dry run can point it at an echo stub; the box uses the
# real notifier, which reads its secret out-of-band from /etc/heresay/notify.env.
NOTIFY="${NOTIFY:-/usr/local/bin/heresay-notify.sh}"
DISK_WARN="${DISK_WARN:-85}"          # percent full
STATE_WARN_MB="${STATE_WARN_MB:-500}" # alert when the state dir grows past this
STATE_DIR="${STATE_DIR:-/var/lib/heresay}"
CERT_WARN_DAYS="${CERT_WARN_DAYS:-14}"

# ⚠ The domain comes from the SAME file the cert-copy job reads, so there is one
# place on the box that names it and no second copy to drift. A house that has
# not written that file yet gets the placeholder, the probe finds no matching
# certificate, and the cert check simply says nothing — which is why the disk
# check above it is written first and independently.
DOMAIN="your.domain.example"
# shellcheck source=/dev/null
[ -r /etc/heresay/cert-copy.env ] && . /etc/heresay/cert-copy.env
HOST="$(hostname -s 2>/dev/null || echo this host)"

# Disk + state store. Sizes are du -h (human) in the message so a 28-byte
# accounts.json reads "4.0K", not the "1 MB" du -m rounds it to; a numeric du -sm
# is kept ONLY for the state-size threshold compare. A missing file reports 0.
DISK_PCT=$(df --output=pcent "$STATE_DIR" 2>/dev/null | tail -1 | tr -dc '0-9')
STATE_MB=$(du -sm "$STATE_DIR" 2>/dev/null | cut -f1)
STATE_H=$(du -sh "$STATE_DIR" 2>/dev/null | cut -f1)
file_h() { [ -f "$1" ] && du -h "$1" 2>/dev/null | cut -f1 || echo "0"; }
ACC_H=$(file_h "$STATE_DIR/accounts.json")
BAN_H=$(file_h "$STATE_DIR/ban_history.json")

DISK_BREACH=""
if [ -n "${DISK_PCT:-}" ] && [ "$DISK_PCT" -ge "$DISK_WARN" ]; then
  DISK_BREACH="disk ${DISK_PCT}% full (>= ${DISK_WARN}%)"
fi
if [ -n "${STATE_MB:-}" ] && [ "$STATE_MB" -gt "$STATE_WARN_MB" ]; then
  DISK_BREACH="${DISK_BREACH:+$DISK_BREACH; }state dir ${STATE_MB} MB (> ${STATE_WARN_MB} MB)"
fi
if [ -n "$DISK_BREACH" ]; then
  "$NOTIFY" high "Heresay: disk/state" \
    "Disk pressure on ${HOST}: ${DISK_BREACH}. State ${STATE_H:-?} (accounts.json ${ACC_H}, ban_history.json ${BAN_H}). Check: df -h ${STATE_DIR} ; du -sh ${STATE_DIR}/*"
fi

# The cert the RAW TLS DOOR is actually serving on :2323 — not the one Caddy
# holds. That is the point of checking it here: cert-copy is what carries the
# renewed cert across, and this notices when that has quietly stopped happening.
END=$(echo | openssl s_client -connect 127.0.0.1:2323 \
        -servername "$DOMAIN" 2>/dev/null \
      | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$END" ]; then
  END_EPOCH=$(date -d "$END" +%s 2>/dev/null || echo 0)
  DAYS=$(( (END_EPOCH - $(date +%s)) / 86400 ))
  if [ "$END_EPOCH" -gt 0 ] && [ "$DAYS" -le "$CERT_WARN_DAYS" ]; then
    "$NOTIFY" high "Heresay: cert expires in ${DAYS}d" \
      "TLS cert on the public door expires in ${DAYS} day(s) — renewal/cert-copy may have failed. Check: systemctl status caddy heresay-cert-copy.timer"
  fi
fi
exit 0
