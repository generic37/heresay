#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Pull the newest server backup to this machine, unattended.
#
# Run by the Windows scheduled task, which invokes it through `wsl.exe`. Doing
# the scheduling on the Windows side is the point: WSL has no always-running
# init to hold a cron, but Task Scheduler survives reboots and — with
# StartWhenAvailable — catches up a run the PC slept through.
#
# ⛔ THIS SCRIPT CANNOT DETECT ITS OWN DEATH. It has been killed mid-run
# (STATUS_CONTROL_C_EXIT) and a killed process runs no error handler, writes no
# log line and sends nothing. So the design does not try: it is IDEMPOTENT and
# CHEAP, so it can run often, and the only thing it promises is to make a noise
# when the newest remote tarball is not sitting here in one piece.
#
# Four outcomes, one line of log each:
#
#   ok already-current   the newest remote tarball is here and opens. SILENT.
#   ok fetched           it was not here, now it is, and it opens.
#   FAILED fetch         could not list or could not copy.
#   FAILED verify        it landed empty or truncated — that is NOT a success.
#
# The last two push to ntfy. The first two say nothing to anybody.
#
# The tarballs hold encrypted mail and .revtell buffers and password hashes.
#
# ⛔ They do NOT hold `mail.key` — the key that decrypts the mail (v1.48.0,
# audit E1). It used to ride along, which meant this weekly pull carried the
# ciphertext and its key together to a second machine and the encryption bought
# nothing. The key is excluded at the source now (deploy/bin/heresay-backup.sh).
#
# ⚠ SO A RESTORE NEEDS TWO INPUTS: one of these tarballs AND the one-time copy
# of `mail.key` you made by hand and stored elsewhere (see
# deploy/install/README.md, "The one-time mail.key backup"). If you have never
# made that copy, the mail in these files cannot be read back — do it now.
#
# They stay 0600 in a gitignored 0700 directory; nothing here prints their
# contents, only names and counts.
set -uo pipefail          # NOT -e: a failure must still be logged before exit.

# ⚠ Task Scheduler starts this with a bare environment — no `.bashrc`, no PATH
# worth trusting. Name the standard locations rather than hoping.
PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${FETCH_BACKUP_DEST:-$REPO/backups}"          # overridable for testing
LOG="$DEST/fetch.log"
KEEP="${FETCH_BACKUP_KEEP:-8}"                      # tarballs retained locally
LOG_LINES=50

HOST="${HERESAY_HOST:-commons}"
REMOTE_DIR="/var/backups/heresay"
NOTIFY_ENV="${FETCH_BACKUP_ENV:-$HOME/.config/heresay/notify.env}"

# ⚠ Every call to the network is bounded. A hung ssh must fail and notify, not
# sit there until the scheduler's kill timer — which is the failure this whole
# script exists to stop being silent.
SSH_BIN="${FETCH_BACKUP_SSH:-ssh}"
SCP_BIN="${FETCH_BACKUP_SCP:-scp}"
CONNECT_TIMEOUT=15        # seconds to establish
LIST_TIMEOUT=60           # seconds for the remote `ls`
COPY_TIMEOUT=900          # seconds for the copy; a tarball is small, a stall is not
# ⛔ BatchMode: never prompt. In a bare environment a passphrase prompt would
# hang until the task is killed, which is exactly how this failed silently.
SSH_OPTS=(-o BatchMode=yes -o "ConnectTimeout=$CONNECT_TIMEOUT")

# ⚠ 0700, not the umask's guess (v1.48.0, audit E2). The tarballs are 0600, so
# their contents were never exposed — but the directory was world-listable, and
# a listing of backup filenames is a map of what is worth taking. `chmod` as
# well as `mkdir -m`, so a directory that already exists is corrected too.
mkdir -m 700 -p "$DEST"
chmod 700 "$DEST"

stamp() { date '+%Y-%m-%d %H:%M:%S %Z'; }

# ⚠ `heresay-*` ONLY, everywhere. The two `talker-*.tar.gz` from before the
# v1.15.0 rename are deliberately outside every glob here: not counted toward
# KEEP, never pruned, never verified. They are a hand-kept relic and this
# script does not get an opinion about them.
count() { find "$DEST" -maxdepth 1 -name 'heresay-*.tar.gz' | wc -l; }

# A tarball is present AND whole, or it is not here. An scp that "succeeded"
# into a truncated file is the failure mode this catches.
valid() {
  [ -s "$1" ] && tar tzf "$1" >/dev/null 2>&1
}

# Days since the newest local tarball, or "never" if there is not one.
local_age_days() {
  local newest
  newest="$(find "$DEST" -maxdepth 1 -name 'heresay-*.tar.gz' -printf '%T@\n' \
            2>/dev/null | sort -rn | head -1)"
  if [ -z "$newest" ]; then echo "never"; return; fi
  echo $(( ( $(date +%s) - ${newest%.*} ) / 86400 ))
}

# ---- the notifier ---------------------------------------------------------
# ⛔ The topic is read at run time from an untracked 0600 file and is NEVER
# echoed, logged or put in a message. On ntfy.sh the topic IS the authentication.
notify_ready=0
if [ -f "$NOTIFY_ENV" ]; then
  # shellcheck disable=SC1090
  . "$NOTIFY_ENV" 2>/dev/null || true
  [ -n "${NTFY_TOPIC:-}" ] && notify_ready=1
fi

notify() {   # $1 = title, $2 = message
  if [ -n "${FETCH_BACKUP_NOTIFY:-}" ]; then
    "$FETCH_BACKUP_NOTIFY" "$1" "$2"
    return
  fi
  [ "$notify_ready" -eq 1 ] || return 1
  curl -fsS --max-time 20 \
       -H "Title: $1" -H "Priority: high" -H "Tags: warning" \
       -d "$2" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1
}

# ---- 1. what is the newest tarball on the server? -------------------------
outcome=""
detail=""
remote_name=""

listing="$(timeout "$LIST_TIMEOUT" "$SSH_BIN" "${SSH_OPTS[@]}" "$HOST" \
           "ls -1t $REMOTE_DIR/heresay-*.tar.gz 2>/dev/null | head -1" 2>/dev/null)"
ssh_status=$?

if [ "$ssh_status" -ne 0 ] || [ -z "$listing" ]; then
  outcome="FAILED fetch"
  if [ "$ssh_status" -eq 124 ]; then
    detail="no answer from $HOST within ${LIST_TIMEOUT}s"
  elif [ "$ssh_status" -ne 0 ]; then
    detail="cannot reach $HOST (ssh exit $ssh_status)"
  else
    detail="no tarball in $REMOTE_DIR"
  fi
else
  remote_name="$(basename "$listing")"

  # ---- 2. already here and whole? then there is nothing to do -------------
  if [ -e "$DEST/$remote_name" ] && valid "$DEST/$remote_name"; then
    outcome="ok already-current"
    detail="$remote_name"
  else
    # A local file that does not open is not a copy. Drop it and re-fetch.
    [ -e "$DEST/$remote_name" ] && rm -f -- "$DEST/$remote_name"

    # ---- 3. fetch it ------------------------------------------------------
    if timeout "$COPY_TIMEOUT" "$SCP_BIN" "${SSH_OPTS[@]}" \
         "$HOST:$REMOTE_DIR/$remote_name" "$DEST/" >/dev/null 2>&1; then
      # ---- 4. verify what landed -----------------------------------------
      if valid "$DEST/$remote_name"; then
        chmod 600 "$DEST/$remote_name" 2>/dev/null || true
        outcome="ok fetched"
        detail="$remote_name"
      else
        # ⛔ Do not keep a truncated file: next run would see the name present
        # and, without the verify, call it current for ever.
        rm -f -- "$DEST/$remote_name"
        outcome="FAILED verify"
        detail="$remote_name arrived empty or truncated"
      fi
    else
      outcome="FAILED fetch"
      detail="could not copy $remote_name"
    fi
  fi
fi

# ---- 5. prune only after a verified success -------------------------------
# ⛔ Never before. Pruning on a run that then fails to fetch would trade a good
# old copy for nothing.
case "$outcome" in
  ok*)
    find "$DEST" -maxdepth 1 -name 'heresay-*.tar.gz' -printf '%T@ %p\n' 2>/dev/null \
      | sort -rn | tail -n +$((KEEP + 1)) | cut -d' ' -f2- \
      | while read -r old; do rm -f -- "$old"; done
    ;;
esac

# ---- the log: one line per run, including the silent case -----------------
echo "$(stamp)  $outcome  $detail  (retained $(count))" >> "$LOG"

if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt "$LOG_LINES" ]; then
  tmp="$(mktemp)"
  tail -n "$LOG_LINES" "$LOG" > "$tmp" && cat "$tmp" > "$LOG" && rm -f "$tmp"
fi

# ---- the only notification path -------------------------------------------
status=0
case "$outcome" in
  ok*) ;;
  *)
    age="$(local_age_days)"
    if [ "$age" = "never" ]; then
      age_phrase="there is no local copy at all"
    elif [ "$age" -eq 0 ]; then
      age_phrase="the newest local copy is from today"
    else
      age_phrase="the newest local copy is ${age} day(s) old"
    fi
    notify "Heresay: backup pull failed" \
           "${detail}. Now ${age_phrase}. Server-side backups are unaffected; this is the off-box copy."
    status=1
    ;;
esac

# ⚠ A notifier that cannot notify must not look healthy. Say so even on a run
# that otherwise succeeded — the safety net being down is the thing to know.
if [ "$notify_ready" -ne 1 ] && [ -z "${FETCH_BACKUP_NOTIFY:-}" ]; then
  echo "fetch-backup: NTFY_TOPIC is unset or $NOTIFY_ENV is missing —" \
       "this run could not have reported a failure." >&2
  status=1
fi

exit "$status"
