#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Follow events.jsonl; push ONE nudge when a report is filed. Installs to
# /usr/local/bin/heresay-report-watch.sh (0755) and runs as
# heresay-report-watch.service.
#
# ⛔⛔ THE NUDGE IS NEVER THE CONTENT.
#
# The message below says a report is waiting and where to go and read it. It
# carries NO reporter, NO subject, NO accused, and not one word of the text —
# and it could not, because the event it keys on has no such field. That is
# the design, at both ends: `report_filed` is `{ts, type}` and nothing else
# (see heresay/events.py), so there is no path by which anything anybody wrote
# could reach a phone, an ntfy topic, or this script's memory. A report is read
# in-world, by somebody who ran `.read_report`, and nowhere else.
#
# ⚠ THE NTFY TOPIC IS A PUBLIC URL — knowing it is permission to read every
# alert this box sends. That is precisely why the body is a doorbell rather
# than a summary: the worst case for a leaked topic is a stranger learning that
# a report exists, which is nothing at all.
#
# ⛔ IT KEYS ON THE STRUCTURED EVENT, NOT LOG PROSE — the same contract the
# ceiling watcher runs on. The engine serialises with no spaces
# (`separators=(",",":")`) and a test matches this exact substring against
# bytes a real EventStore writes, so the two cannot drift apart.
#
# ⛔⛔ THE INVARIANT: A GIVEN EVENT ALERTS AT MOST ONCE, EVER.
#
# ⚠ THIS IS NOT A NEW GATE. It is `heresay-ceiling-watch.sh`'s, verbatim in
# shape, and it is copied rather than shared for a reason argued below. That
# gate took SEVEN attempts to get right; a report watcher has the identical
# trim/replay problem (the engine trims events.jsonl in place, `tail -F`
# reopens the replacement AT ITS TOP, and every surviving line arrives again),
# so re-deriving one here would be re-earning a bug that is already paid for.
#
# TWO values are persisted, and the first is the one that closes it:
#
#   HIGH  the HIGH-WATER MARK: the highest event `ts` PROCESSED. ⚠ Read it as
#         THE SENT-FLAG FOR EVERY EVENT UP TO THAT POINT.
#         ⛔ It advances on EVERY QUALIFYING EVENT, sent or not — that "or not"
#         is the specific hole in a one-value design: an event swallowed by the
#         debounce is never recorded anywhere, stays eligible, and re-alerts
#         hours later when a trim replays it.
#   STAMP the LAST-NOTIFY time, and nothing else. It exists only for the
#         burst debounce.
#
# The gate is `ts >= START` AND `ts > HIGH`, sound because this log is
# APPEND-ONLY and SINGLE-WRITER. ⚠ `>=` on START and `>` on HIGH, deliberately:
# a report filed in the SAME SECOND as the watcher's start is a real report and
# still alerts, while a replay of something already processed cannot be.
#
# ⚠ WHY A SIBLING SCRIPT RATHER THAN A SHARED LIBRARY. The common part is about
# a dozen lines of shell; what differs is the match, the message, the cooldown
# and the four state paths. Factoring it out would mean a FIFTH file in
# /usr/local/bin that both watchers source — a new install step, and a new
# failure mode in which a watcher dies because its library is missing or stale,
# which is exactly the class of failure this layer exists to notice. The
# guarantee is bought instead by a TEST that reads BOTH scripts and asserts the
# same ordering in each (advance-before-debounce-before-notify), which also
# covers a third watcher written years from now — something a shared library
# could not do.
#
# ⚠ 10-MINUTE DEBOUNCE, and it is SHORTER than the ceiling's 30. The two are
# reporting different things: the ceiling reports a sustained attack, where the
# tenth buzz says nothing the first did not, and this reports that a person is
# waiting for an answer. A burst of reports in one minute is still one trip to
# `.read_report` — but somebody who files at 14:00 and somebody else who files
# at 14:20 are two people, and half an hour of silence is too long to leave the
# second one unnoticed.
#
# ⛔ AND THE MESSAGE DOES NOT COUNT THEM. "A report is waiting" is true whether
# it is one or nine, it stays true until somebody looks, and a count would be
# the first step toward the alert describing the box rather than pointing at it.
#
# ---- failure modes, each handled or accepted on purpose -------------------
#
# * /run IS TMPFS — both values vanish on reboot. SAFE: START is re-taken at
#   startup too, so it lands at ~boot and every line already in the log is
#   older than it. ⚠ The cost is real and worth stating: a report filed while
#   the box was down is never nudged for. It is not lost — it is in the box,
#   and the reader is told at login and by the live in-world ping — but the
#   PHONE stays quiet. The alternative (alerting on everything in the file at
#   startup) would buzz for every report of the last ten days on every reboot,
#   which is worse, and would train the reader to ignore it.
# * A RETENTION TRIM REPLACES THE FILE and `tail -F` reopens it at the top.
#   ⛔ Every replayed line sits at or below HIGH, so the whole replay is silent.
# * A TORN OR PARTIAL LINE fails the numeric check below and is skipped. It
#   must never alert, and it must never poison HIGH.
# * THE CLOCK STEPPED BACKWARDS by NTP: HIGH is now ahead of real time, so a
#   genuine report inside the step could be suppressed. ⚠ ACCEPTED, and the
#   in-world notices are what make it survivable — a missed buzz is not a
#   missed report.
# * THE STAMP OR HIGH-WATER WRITE FAILS (a full /run): the value stays behind
#   and one event can alert twice. ⚠ Accepted: a repeated nudge is a nuisance,
#   a missing one is the failure this file exists to prevent.
set -uo pipefail
NOTIFY="${NOTIFY:-/usr/local/bin/heresay-notify.sh}"
LOG="${REPORT_LOG:-/var/lib/heresay/events.jsonl}"
# Both in /run (tmpfs) — see the reboot note above. ⚠ Their OWN paths, not the
# ceiling watcher's: two watchers sharing one high-water mark would each
# silence the other's events.
STAMP="${REPORT_STAMP:-/run/heresay-report.last}"
HIGHWATER="${REPORT_HIGHWATER:-/run/heresay-report.high}"
COOLDOWN=600
# The instant the watcher started. Anything stamped before this is history.
START="${REPORT_START:-$(date +%s)}"
HOST="${REPORT_HOUSE:-$(hostname -s 2>/dev/null || echo this host)}"

# The follow command is a seam so a test can feed a finite stream (`cat`); in
# production it is `tail -n0 -F` — -F not -f because the engine TRIMS this file
# in place, which replaces the inode; `tail -f` would go on watching a file
# nobody writes to any more and report nothing, forever.
${REPORT_TAIL:-tail -n0 -F} "$LOG" 2>/dev/null | while IFS= read -r line; do
  case "$line" in
    *'"type":"report_filed"'*)
      # The event's own epoch — integer part is plenty. Anything unparseable is
      # dropped here: a torn line must neither alert nor move the high-water
      # mark past events that have not been seen.
      ts=${line#*'"ts":'}; ts=${ts%%,*}; ts=${ts%.*}
      case "$ts" in ''|*[!0-9]*) continue ;; esac

      # ⛔ THE GATE. Older than our start → history. At or below the high-water
      # mark → already processed, whatever became of it.
      [ "$ts" -ge "$START" ] || continue
      high=$(cat "$HIGHWATER" 2>/dev/null || echo 0)
      case "$high" in ''|*[!0-9]*) high=0 ;; esac
      [ "$ts" -gt "$high" ] || continue

      # ⛔ ADVANCE THE HIGH-WATER MARK FIRST — BEFORE the debounce, and whether
      # or not a notification follows. An event the debounce swallows has still
      # been PROCESSED; not recording that is what let a trim replay re-alert.
      echo "$ts" > "$HIGHWATER"

      now=$(date +%s)
      if [ -f "$STAMP" ] && [ $(( now - $(cat "$STAMP" 2>/dev/null || echo 0) )) -lt "$COOLDOWN" ]; then
        continue
      fi
      echo "$now" > "$STAMP"
      # ⛔ THE BODY. A doorbell and a pointer — no name, no subject, no words,
      # no count. Everything in this string is either engine-fixed or the
      # HOSTNAME; nothing here came out of the event, because the event has
      # nothing in it.
      "$NOTIFY" default "Heresay: new report" \
        "A new report was filed on ${HOST} — log in and .read_report to read it."
      ;;
  esac
done
