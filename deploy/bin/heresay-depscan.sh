#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Audit the RUNNING dependencies for known CVEs. Run monthly by
# heresay-depscan.timer; runs in place from /opt/heresay/deploy/bin, exactly
# like heresay-backup.sh and heresay-cert-copy.sh.
#
# ⛔ IT AUDITS WHAT IS INSTALLED, NOT WHAT IS PINNED. `make audit` on a
# workstation runs pip-audit over requirements.txt, which answers "is the file
# we intend to install clean?". This answers the question that actually matters
# on a live box — "is what is running clean?" — by freezing the daemon's own
# venv and auditing that. The two agree right up until the moment they don't,
# and the moment they don't is the one worth hearing about.
#
# ⚠ A FINDING IS A FAILED UNIT, ON PURPOSE. pip-audit exits non-zero when it
# finds a vulnerability, the oneshot unit inherits that, and the box then says
# so in `systemctl --failed` — which is a thing an operator already looks at.
# Nothing here mails anybody, and nothing here upgrades anything: an automatic
# dependency bump on a live host is a bigger decision than a cron job may take.
#     journalctl -u heresay-depscan     # what it found
#
# ⚠ THE AUDIT TOOLING LIVES IN ITS OWN VENV, deliberately. `pip-audit` and its
# dependency oak have no business inside the venv the daemon imports from — the
# production venv holds the three runtime dependencies and nothing else
# (requirements-dev.txt is never installed on a server, and this keeps that
# true). The two venvs never meet: this one only ever READS a freeze of that one.
set -euo pipefail

DAEMON_VENV=/opt/heresay/.venv
# ⚠ Excluded from the deploy rsync (see deploy.sh) so a push does not delete it
# every time. It is rebuilt on the next run if it does go — the line below is
# the whole recovery — but rebuilding it monthly for no reason is just noise.
AUDIT_VENV=/opt/heresay/.audit-venv

FREEZE="$(mktemp)"; trap 'rm -f "$FREEZE"' EXIT

[ -x "$AUDIT_VENV/bin/pip-audit" ] || python3 -m venv "$AUDIT_VENV"
"$AUDIT_VENV/bin/pip" install -q --upgrade pip pip-audit >/dev/null

"$DAEMON_VENV/bin/pip" freeze > "$FREEZE"
echo "auditing $(wc -l < "$FREEZE") installed packages"
exec "$AUDIT_VENV/bin/pip-audit" -r "$FREEZE"
