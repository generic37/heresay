#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Monthly housekeeping for the heresay box: patch, prune, report, reboot.
#
# Installed to /usr/local/sbin/heresay-maintenance (root, 0755) and fired by
# heresay-maintenance.timer on the 1st at 09:10 UTC — a quiet hour, when the
# rooms are as empty as they ever are.
#
# The reboot at the end is deliberate and unconditional: a clean kernel and a
# clean slate. Nothing is lost by it — by covenant the world holds no state that
# survives a restart anyway (rooms empty, locks/gags/topics/.rev evaporate), and
# heresay + caddy both come back on their own. `--no-reboot` skips only that last
# step, for a manual trial run.
#
# The report is FACTS ONLY: counts, sizes, dates. No room text ever passes
# through here, and nothing this writes could contain any.
set -uo pipefail          # NOT -e: a failed step must still be reported, and
                          # must not skip the reboot the box is waiting on.

LOG="/var/log/heresay-maintenance.log"
BACKUP_DIR="/var/backups/heresay"
KEEP_RUNS=24              # trim the log to roughly the last two years of runs
STALE_HOURS=48            # warn if the newest backup is older than this
REBOOT=1

for arg in "$@"; do
  case "$arg" in
    --no-reboot) REBOOT=0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# Everything the script prints is collected, written to the log as one block,
# and echoed to stdout/journal for `systemctl status`.
report=()
say() { report+=("$*"); }

say "=== heresay maintenance $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

# ---- 1. patch -----------------------------------------------------------
export DEBIAN_FRONTEND=noninteractive
before="$(dpkg-query -W -f='${binary:Package} ${Version}\n' 2>/dev/null | sort)"

apt_ok=1
apt-get update -qq                                   || apt_ok=0
apt-get -y -qq full-upgrade                          || apt_ok=0
apt-get -y -qq autoremove --purge                    || apt_ok=0

after="$(dpkg-query -W -f='${binary:Package} ${Version}\n' 2>/dev/null | sort)"
changed="$(comm -13 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | wc -l)"
removed="$(comm -23 <(printf '%s\n' "$before") <(printf '%s\n' "$after") | wc -l)"
say "apt: $changed package(s) installed/upgraded, $removed removed/replaced$([ "$apt_ok" = 1 ] || echo '  [APT REPORTED AN ERROR]')"

# A kernel or libc change is exactly why the reboot is unconditional; note it.
if [ -f /var/run/reboot-required ]; then
  say "apt: the system flagged a reboot as required"
fi

# ---- 2. prune -----------------------------------------------------------
# Belt to the journald drop-in's braces (MaxRetentionSec=7day): the policy is
# 7 days of PROCESS output, and this makes sure that is true on disk today.
#
# The engine's DOMAIN facts are no longer in here (v1.30.0) — they live in
# events.jsonl, which the daemon trims itself on its own window
# (`event_retention_days`). Nothing to vacuum there, and nothing to install:
# reporting its size is enough to notice if it ever stops trimming.
vac="$(journalctl --vacuum-time=7d 2>&1 | tail -1)"
say "journal: ${vac:-nothing to vacuum}"
if [ -f /var/lib/heresay/events.jsonl ]; then
  ev_lines="$(wc -l < /var/lib/heresay/events.jsonl 2>/dev/null || echo '?')"
  ev_size="$(du -h /var/lib/heresay/events.jsonl 2>/dev/null | cut -f1)"
  # A count and a size — facts about the file, never a line out of it.
  say "event log: ${ev_lines} event(s), ${ev_size:-?} (self-trimming)"
fi

# ---- 2b. backup freshness ----------------------------------------------
if [ -d "$BACKUP_DIR" ]; then
  count="$(find "$BACKUP_DIR" -maxdepth 1 -name 'heresay-*.tar.gz' | wc -l)"
  newest="$(find "$BACKUP_DIR" -maxdepth 1 -name 'heresay-*.tar.gz' -printf '%T@ %p\n' \
            2>/dev/null | sort -rn | head -1)"
  if [ -n "$newest" ]; then
    newest_path="${newest#* }"
    age_h=$(( ( $(date +%s) - ${newest%%.*} ) / 3600 ))
    if [ "$age_h" -gt "$STALE_HOURS" ]; then
      say "backups: $count present, newest ${age_h}h old  [WARNING: STALE, expected under ${STALE_HOURS}h]"
    else
      say "backups: $count present, newest ${age_h}h old ($(basename "$newest_path"))"
    fi
  else
    say "backups: directory exists but holds NO tarballs  [WARNING]"
  fi
else
  say "backups: $BACKUP_DIR is missing  [WARNING]"
fi

# ---- 3. report ----------------------------------------------------------
say "disk: $(df -h / | awk 'NR==2 {print $3 " used of " $2 " (" $5 "), " $4 " free"}')"
say "uptime before this run: $(uptime -p 2>/dev/null || echo unknown)"
say "reboot: $([ "$REBOOT" = 1 ] && echo 'yes, now' || echo 'skipped (--no-reboot)')"

printf '%s\n' "${report[@]}"
{
  printf '%s\n' "${report[@]}"
  echo
} >> "$LOG" 2>/dev/null || true
chmod 640 "$LOG" 2>/dev/null || true

# Trim the log to the last $KEEP_RUNS blocks. Runs are separated by the "==="
# header line, so keep from the Nth-from-last header onward.
if [ -f "$LOG" ]; then
  start="$(grep -n '^=== heresay maintenance' "$LOG" | tail -n "$KEEP_RUNS" \
           | head -1 | cut -d: -f1)"
  if [ -n "${start:-}" ] && [ "$start" -gt 1 ]; then
    tmp="$(mktemp)"
    tail -n +"$start" "$LOG" > "$tmp" && cat "$tmp" > "$LOG" && rm -f "$tmp"
  fi
fi

# ---- 4. reboot ----------------------------------------------------------
if [ "$REBOOT" = 1 ]; then
  sync
  systemctl reboot
fi
