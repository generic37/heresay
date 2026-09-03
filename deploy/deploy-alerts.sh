#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Refresh the alerting layer on the server — the /usr/local/bin scripts AND the
# systemd units that drive them. ⛔ THE DAEMON IS NOT TOUCHED: no restart of
# heresay, no --write-permissions, nothing written under /opt/heresay or
# /var/lib/heresay. Run this — and only this — when an alert script or an
# alerting unit changed.
#
# ⛔ THE WHOLE POINT IS THE DECOUPLING. The alert scripts live in
# /usr/local/bin, deliberately OUTSIDE the /opt/heresay deploy rsync target, so
# `make deploy --delete` can never wipe them and a code push never disturbs the
# monitoring. The price of that decoupling is that a changed alert script is NOT
# carried by `make deploy`; this is the one command that carries it. Like
# `make deploy-content` is content-only, this is alerts-only.
#
# ⚠ THE UNITS HAVE THE SAME PROBLEM AND IT IS WORSE. /etc/systemd/system holds
# whatever was hand-copied at install time and nothing propagates a correction
# there — by 2026-09-01 SEVEN had drifted from this repo, one of which had
# stopped a monthly CVE scan from ever running (SECURITY_CHECKLIST L22, L51).
# This script is that missing path.
#
# It stages the current files to a throwaway temp dir on the box, compares each
# against what is installed, and writes only what differs. Idempotent: a second
# run reports `unchanged` for everything and touches nothing.
#
# The script set is exactly the five that RUN FROM /usr/local/bin — the same
# five the install README first installs there. The run-in-place scripts
# (heresay-backup/cert-copy/depscan, under /opt/heresay/deploy/bin) are NOT here:
# a normal `make deploy` already refreshes those, because they live inside the
# rsync target.
#
# Host resolution is identical to deploy.sh / deploy-content.sh: DEPLOY_HOST from
# the environment, else deploy/local.env, else refuse — a deploy script does not
# guess which machine to write to.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "${DEPLOY_HOST:-}" ] && [ -f "$REPO/deploy/local.env" ]; then
  # shellcheck source=/dev/null
  source "$REPO/deploy/local.env"
fi
if [ -z "${DEPLOY_HOST:-}" ]; then
  echo "deploy-alerts: DEPLOY_HOST is not set (env, or deploy/local.env). Refusing to guess." >&2
  exit 1
fi
HOST="$DEPLOY_HOST"
SRC="$REPO/deploy/install"

# The alert scripts that run from /usr/local/bin (see the install README).
SCRIPTS=(
  heresay-notify.sh
  heresay-hcping.sh
  heresay-ceiling-watch.sh
  heresay-report-watch.sh
  heresay-healthcheck.sh
  heresay-boot-notify.sh
)

# ⛔ THE UNIT SET IS ENUMERATED, NEVER GLOBBED, and the difference is the whole
# safety property: `deploy/install/*.service` would sweep up heresay.service —
# the daemon's own unit — and a silent overwrite of that from an alerts refresh
# is precisely the wrong path for it. A change to the daemon's unit belongs to
# `make deploy`, which restarts through the graceful countdown.
#
# The rule that generated this list: a unit is here if its job is to TELL THE
# OPERATOR SOMETHING. notify@ is the messenger; boot-notify says the box came
# back; ceiling-watch and report-watch follow the event log; healthcheck reports
# unhealthy; depscan reports a CVE. ⚠ The units that DO something rather than
# say something — backup, cert-copy, maintenance — are not alerting and are not
# here, even though `make deploy` does not carry them either.
UNITS=(
  heresay-notify@.service
  heresay-boot-notify.service
  heresay-ceiling-watch.service
  heresay-report-watch.service
  heresay-healthcheck.service
  heresay-healthcheck.timer
  heresay-depscan.service
  heresay-depscan.timer
)

# ⚠ The drop-ins are alerting even where they land on units that are not:
# onfailure.conf adds ONE line, `OnFailure=heresay-notify@%n.service`, and
# hcping.conf adds ONE ExecStartPost that pings the dead-man's switch. Neither
# edits the unit it sits beside — that is what a .d/ fragment is for, and it is
# why shipping one into heresay.service.d is not shipping heresay.service.
ONFAILURE_UNITS=(heresay heresay-depscan heresay-backup caddy heresay-cert-copy)

# ⛔ THE GUARD. The list above is a positive enumeration, so the daemon's unit is
# excluded by not being named — but a later hand-edit could add it, and this is
# the one mistake this script must not be able to make. Refuse rather than ship.
# (tests/test_deploy_scripts.py asserts the same thing against this file.)
for u in "${UNITS[@]}"; do
  case "$u" in
    heresay.service)
      echo "deploy-alerts: heresay.service is the DAEMON's unit and must never ship from here." >&2
      echo "               Use 'make deploy', which restarts through the graceful countdown." >&2
      exit 1
      ;;
  esac
done

echo ">> refresh the alerting layer on $HOST   (scripts + units; the daemon is not touched)"

# Stage to a temp dir the login user can write, then install into the root-owned
# destinations with sudo — the same `install -m 0755 -o root -g root` the
# README documents, just carried from the local repo rather than from a copy a
# prior deploy happened to leave on the box.
STAGE="$(ssh "$HOST" 'mktemp -d')"
trap 'ssh "$HOST" "rm -rf \"$STAGE\"" >/dev/null 2>&1 || true' EXIT

rsync -az "${SCRIPTS[@]/#/$SRC/}" "${UNITS[@]/#/$SRC/}" \
         "$SRC/onfailure.conf" "$SRC/hcping.conf" "$HOST:$STAGE/"

# ⚠ EVERY FILE IS COMPARED BEFORE IT IS WRITTEN, and the line says which of new /
# changed / unchanged it was. A run that prints "installed" for a dozen identical
# files teaches the operator to stop reading the output, which is how a real
# change goes past unread.
#
# ⛔ The restarts are scoped to what actually needs one. ceiling-watch and
# report-watch are LONG-RUNNING followers: they hold their script in memory, so a
# refreshed script or unit does nothing for them until they are restarted. Timers
# do not need it — daemon-reload is enough — and notify@ is instantiated per
# failure, so it picks up its new definition on its next firing.
# ⛔ AND NOTHING IS EVER ENABLED OR STARTED. A watcher that is stopped is left
# stopped: starting it would change what the box does without being asked, which
# is an install-time decision (heresay-report-watch's manual enable is deliberate
# — docs/OPERATIONS.md).
ssh "$HOST" "set -e
changed=0; ceiling=0; report=0

classify() {   # \$1 staged, \$2 installed -> echoes new|changed|unchanged
  if [ ! -e \"\$2\" ]; then echo new
  elif ! cmp -s \"\$1\" \"\$2\"; then echo changed
  else echo unchanged; fi
}

for f in ${SCRIPTS[*]}; do
  state=\$(classify \"$STAGE/\$f\" \"/usr/local/bin/\$f\")
  if [ \"\$state\" != unchanged ]; then
    sudo install -m 0755 -o root -g root \"$STAGE/\$f\" \"/usr/local/bin/\$f\"
    changed=1
    case \"\$f\" in heresay-ceiling-watch.sh) ceiling=1;; heresay-report-watch.sh) report=1;; esac
  fi
  printf '   %-9s /usr/local/bin/%s\n' \"\$state\" \"\$f\"
done

for f in ${UNITS[*]}; do
  state=\$(classify \"$STAGE/\$f\" \"/etc/systemd/system/\$f\")
  if [ \"\$state\" != unchanged ]; then
    sudo install -m 0644 -o root -g root \"$STAGE/\$f\" \"/etc/systemd/system/\$f\"
    changed=1
    case \"\$f\" in heresay-ceiling-watch.service) ceiling=1;; heresay-report-watch.service) report=1;; esac
  fi
  printf '   %-9s /etc/systemd/system/%s\n' \"\$state\" \"\$f\"
done

for u in ${ONFAILURE_UNITS[*]}; do
  d=/etc/systemd/system/\$u.service.d
  state=\$(classify \"$STAGE/onfailure.conf\" \"\$d/onfailure.conf\")
  if [ \"\$state\" != unchanged ]; then
    sudo install -d \"\$d\"
    sudo install -m 0644 -o root -g root \"$STAGE/onfailure.conf\" \"\$d/onfailure.conf\"
    changed=1
  fi
  printf '   %-9s %s/onfailure.conf\n' \"\$state\" \"\$d\"
done

hcd=/etc/systemd/system/heresay-backup.service.d
state=\$(classify \"$STAGE/hcping.conf\" \"\$hcd/hcping.conf\")
if [ \"\$state\" != unchanged ]; then
  sudo install -d \"\$hcd\"
  sudo install -m 0644 -o root -g root \"$STAGE/hcping.conf\" \"\$hcd/hcping.conf\"
  changed=1
fi
printf '   %-9s %s/hcping.conf\n' \"\$state\" \"\$hcd\"

if [ \"\$changed\" = 0 ]; then
  echo '   nothing changed — no daemon-reload, no restart'
else
  sudo systemctl daemon-reload
  echo '   daemon-reload'
  for w in heresay-ceiling-watch:\$ceiling heresay-report-watch:\$report; do
    unit=\${w%:*}; want=\${w#*:}
    if [ \"\$want\" = 0 ]; then
      echo \"   \$unit: unchanged, not restarted\"
    elif systemctl is-active --quiet \"\$unit\"; then
      sudo systemctl restart \"\$unit\"
      echo \"   restarted \$unit\"
    else
      echo \"   \$unit: not running — left stopped (enablement is not this script's call)\"
    fi
  done
fi"

echo ">> done. Timers pick up their new definitions from the reload; the two"
echo "   long-running watchers were restarted only if their own files changed."
echo "   Prove it now:  ssh $HOST 'sudo systemctl start heresay-healthcheck.service'"
