#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# One buzz when the host comes up, carrying THE SERVICE STATES rather than an
# errand. Installs to /usr/local/bin/heresay-boot-notify.sh (0755); run once per
# boot by heresay-boot-notify.service.
#
# ⛔ IT ALWAYS FIRES, healthy or not, and there is no switch to change that. The
# reboot itself is the fact worth knowing — an unplanned one most of all — so a
# silent-when-healthy mode would drop exactly the signal this alert exists for.
# ⚠ What changed is the CONTENT: the message used to end
# `verify: systemctl status heresay caddy`, which is a notification handing the
# operator an errand it could have run itself.
#
# ⛔ WHY IT POLLS, AND WHY THE UNIT'S `After=` IS NOT ENOUGH ON ITS OWN.
# `After=` orders the START of a job, not the reaching of a healthy state:
# `heresay.service` is `Type=simple`, so systemd calls it started the instant it
# execs — before the world is loaded or a socket is bound. And a service that
# fails and comes back (`Restart=on-failure`, `RestartSec=2`) passes through
# active → failed → activating → active well AFTER its first start completed.
# Sampling at the ordering point would report `activating` or `failed` for
# services that are perfectly healthy a second later, and ⚠ a false alarm is
# worse than the nag it replaces: the operator learns to disbelieve the alert.
#
# ⛔ SO THE WAIT IS BOUNDED AND THE MESSAGE IS DEFINITE EITHER WAY. It settles
# the moment both are `active` — a few seconds in the ordinary case — and at
# BOOT_WAIT it reports whatever is true then and says that it waited. A boot
# notice that hangs is a boot notice that never arrives.
#
# ⚠ `failed` does NOT end the wait early, deliberately: with RestartSec=2 a
# failed reading is often a service on its way back up, and ending there would
# reintroduce the false alarm this poll exists to prevent.
set -uo pipefail

# Overridable only so a dry run can point it at an echo stub; the box uses the
# real notifier, which reads its secret out-of-band from /etc/heresay/notify.env.
NOTIFY="${NOTIFY:-/usr/local/bin/heresay-notify.sh}"
# 30s is ~10 restart cycles at RestartSec=2 — long enough to tell a slow start
# from a broken one, short enough that the notice is never the reason a boot
# looks stalled. The unit's TimeoutStartSec is the hard ceiling above it.
BOOT_WAIT="${BOOT_WAIT:-30}"
BOOT_POLL="${BOOT_POLL:-2}"
# ⛔ The two the message has always named. Widening this set is a decision, not
# a tweak: every name added is another thing that can make the phone say DOWN.
SERVICES="${SERVICES:-heresay caddy}"

# The unit passes systemd's %H; `hostname -s` is the fallback for a hand run.
HOST="${1:-}"
[ -n "$HOST" ] || HOST="$(hostname -s 2>/dev/null || echo this host)"

# `is-active` exits non-zero for anything but `active` and still PRINTS the
# state, which is the part we want — hence `|| true`.
state() { systemctl is-active "$1" 2>/dev/null || true; }

deadline=$(( SECONDS + BOOT_WAIT ))
all_up=0
while :; do
  all_up=1
  for s in $SERVICES; do
    [ "$(state "$s")" = active ] || all_up=0
  done
  [ "$all_up" = 1 ] && break
  [ "$SECONDS" -ge "$deadline" ] && break
  sleep "$BOOT_POLL"
done

# States first, no shell syntax — this is read on a lock screen.
body=""
for s in $SERVICES; do
  st="$(state "$s")"
  [ -n "$st" ] || st=unknown
  body="${body:+$body, }$s $st"
done

if [ "$all_up" = 1 ]; then
  "$NOTIFY" high "Heresay: $HOST rebooted" "$body"
else
  # ⚠ `max` rather than `high`: the priority is the difference the phone itself
  # can act on, so a bad boot is distinguishable before a word is read.
  "$NOTIFY" max "Heresay: $HOST rebooted — SERVICE DOWN" \
    "$body — still not up after ${BOOT_WAIT}s"
fi

# ⛔ Always 0. A oneshot that exits non-zero at boot marks itself failed, which
# is a second problem reported in place of the first.
exit 0
