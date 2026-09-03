#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Follow events.jsonl; push ONE alert when the server-wide auth-failure ceiling
# trips. Installs to /usr/local/bin/heresay-ceiling-watch.sh (0755) and runs as
# heresay-ceiling-watch.service.
#
# ⛔ IT KEYS ON THE STRUCTURED EVENT, NOT LOG PROSE. v1.53.0 made the ceiling's
# trip a fact in the engine's own store — `{"type":"auth_ceiling","phase":
# "tripped",…}` — precisely so a watcher could match a stable TYPE. The previous
# shape of this watcher tailed the journal for a sentence, which any reword of
# that sentence would have broken in silence. The engine serialises with no
# spaces (`separators=(",",":")`) and a test pins this exact substring, so the
# match below is a contract rather than a hope.
#
# ⛔⛔ THE INVARIANT: A GIVEN EVENT ALERTS AT MOST ONCE, EVER.
#
# Not "once per watcher run". Not "unless the file is replaced". Once, full
# stop. ⚠ This is the SEVENTH pass at this bug, and each earlier one satisfied a
# narrower rule that the next failure walked straight through. The narrower rules
# were: match the prose (broke on a reword), track a byte offset (reset by the
# very trim that causes the replay), and compare the event's `ts` against the
# watcher's START (an event NEWER than start stays eligible for ever — so when a
# retention trim rewrote the file and `tail -F` reopened the replacement at its
# top, the same line passed the gate a second time and re-alerted).
#
# TWO values are persisted, and the first is the one that closes it:
#
#   HIGH  the HIGH-WATER MARK: the highest event `ts` PROCESSED. ⚠ Read it as
#         THE SENT-FLAG FOR EVERY EVENT UP TO THAT POINT — everything at or
#         below it has already had whatever attention it was going to get.
#         ⛔ It advances on EVERY QUALIFYING EVENT, sent or not. That "or not" is
#         the specific hole in a one-value design: an event swallowed by the
#         debounce below never got recorded anywhere, stayed eligible, and
#         re-alerted hours later when a trim replayed it.
#   STAMP the LAST-NOTIFY time, and nothing else. It exists only for the
#         30-minute burst debounce.
#
# The gate is `ts >= START` AND `ts > HIGH`. That is sound because this log is
# APPEND-ONLY and SINGLE-WRITER: one daemon appends, timestamps only move
# forward, so "greater than the highest I have processed" is exactly "new".
# ⚠ `>=` on START and `>` on HIGH, deliberately: a trip in the SAME SECOND as
# the watcher's start is a real trip and still alerts (`>` there would lose it),
# while a replay of something already processed cannot be.
#
# ⚠ 30-MINUTE DEBOUNCE, because the thing this reports is a SUSTAINED run. The
# ceiling can trip, lapse and trip again every few minutes for an hour under a
# distributed guessing attack; one buzz says everything the tenth would.
#
# ---- failure modes, each handled or accepted on purpose -------------------
#
# * /run IS TMPFS — both values vanish on reboot. SAFE: START is re-taken at
#   startup too, so it lands at ~boot and every line already in the log is older
#   than it. A cleared debounce is also right — a box that has just come back
#   should be able to tell you the door is under attack whatever it said before.
# * A RETENTION TRIM REPLACES THE FILE and `tail -F` reopens it at the top.
#   ⛔ THIS IS THE CASE THAT HAS BEEN BITING. Every replayed line sits at or
#   below HIGH, so the whole replay is silent.
# * A TORN OR PARTIAL LINE (a read that caught an append mid-flight) fails the
#   numeric check below and is skipped. It must never alert, and it must never
#   poison HIGH.
# * THE CLOCK STEPPED BACKWARDS by NTP: HIGH is now ahead of real time, so a
#   genuine trip inside the step could be suppressed. ⚠ ACCEPTED — a missed beat
#   is better than crying wolf, and the alternative (trusting an event older
#   than one already processed) is exactly the bug this file exists to fix.
#   Noted here rather than left to be rediscovered.
# * THE STAMP OR HIGH-WATER WRITE FAILS (a full /run): the value stays behind and
#   one event can alert twice. ⚠ Accepted: an alert repeated once is a nuisance,
#   an alert never sent is the failure this whole file guards against.
set -uo pipefail
NOTIFY="${NOTIFY:-/usr/local/bin/heresay-notify.sh}"
LOG="${CEILING_LOG:-/var/lib/heresay/events.jsonl}"
# Both in /run (tmpfs) — see the reboot note above.
STAMP="${CEILING_STAMP:-/run/heresay-ceiling.last}"
HIGHWATER="${CEILING_HIGHWATER:-/run/heresay-ceiling.high}"
COOLDOWN=1800
# The instant the watcher started. Any trip stamped before this is history — from
# a prior run, or re-surfaced by a trim — and is not ours to report.
START="${CEILING_START:-$(date +%s)}"
HOST="$(hostname -s 2>/dev/null || echo this host)"

# The follow command is a seam so a test can feed a finite stream (`cat`); in
# production it is `tail -n0 -F` — -F not -f because the engine TRIMS this file in
# place (event_retention_days), which replaces the inode; `tail -f` would go on
# watching a file nobody writes to any more and report nothing, forever.
${CEILING_TAIL:-tail -n0 -F} "$LOG" 2>/dev/null | while IFS= read -r line; do
  case "$line" in
    *'"type":"auth_ceiling","phase":"tripped"'*)
      # The event's own epoch — integer part is plenty. Anything we cannot parse
      # is dropped here: a torn line must neither alert nor move the high-water
      # mark past events that have not been seen.
      ts=${line#*'"ts":'}; ts=${ts%%,*}; ts=${ts%.*}
      case "$ts" in ''|*[!0-9]*) continue ;; esac

      # ⛔ THE GATE. Older than our start → history. At or below the high-water
      # mark → already processed, whatever became of it.
      [ "$ts" -ge "$START" ] || continue
      high=$(cat "$HIGHWATER" 2>/dev/null || echo 0)
      case "$high" in ''|*[!0-9]*) high=0 ;; esac
      [ "$ts" -gt "$high" ] || continue

      # ⛔ ADVANCE THE HIGH-WATER MARK FIRST — BEFORE the debounce, and whether or
      # not a notification follows. An event that the debounce swallows has still
      # been PROCESSED, and if that is not recorded it stays eligible and comes
      # back through the next file replacement. That was the bug.
      echo "$ts" > "$HIGHWATER"

      now=$(date +%s)
      if [ -f "$STAMP" ] && [ $(( now - $(cat "$STAMP" 2>/dev/null || echo 0) )) -lt "$COOLDOWN" ]; then
        continue
      fi
      echo "$now" > "$STAMP"
      "$NOTIFY" high "Heresay: auth spray" \
        "Server-wide auth ceiling tripped on ${HOST} — likely distributed password-guessing. New logins throttled + creation paused automatically. Look: .security in-world."
      ;;
  esac
done
