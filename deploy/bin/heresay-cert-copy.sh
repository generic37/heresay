#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Copy Caddy's Let's Encrypt cert for the domain into the heresay TLS door's dir,
# and restart the daemon only when the cert actually changed. Idempotent — run
# daily by heresay-cert-copy.timer (09:00 UTC, quiet hours). This is cert option
# (b): Caddy is the single ACME client; the :2323 socket door reuses its cert.
set -euo pipefail

# Which domain's cert to copy. Override WITHOUT editing this file (it is
# overwritten on every deploy) by creating /etc/heresay/cert-copy.env with:
#     DOMAIN=your.real.domain
# That file is sourced below and wins; the baked value is only a placeholder.
DOMAIN="your.domain.example"   # EDIT THIS: your certificate's domain
[ -r /etc/heresay/cert-copy.env ] && . /etc/heresay/cert-copy.env

CADDY_DATA="/var/lib/caddy/.local/share/caddy"
DEST="/etc/heresay/tls"

CRT="$(ls "$CADDY_DATA"/certificates/*/"$DOMAIN"/"$DOMAIN".crt 2>/dev/null | head -1 || true)"
KEY="$(ls "$CADDY_DATA"/certificates/*/"$DOMAIN"/"$DOMAIN".key 2>/dev/null | head -1 || true)"
if [ -z "$CRT" ] || [ -z "$KEY" ]; then
	echo "cert-copy: Caddy cert for $DOMAIN not present yet; nothing to do." >&2
	exit 0
fi

mkdir -p "$DEST"
chmod 750 "$DEST"; chown root:heresay "$DEST"

changed=0
if [ ! -f "$DEST/fullchain.pem" ] || ! cmp -s "$CRT" "$DEST/fullchain.pem"; then
	install -m 640 -o root -g heresay "$CRT" "$DEST/fullchain.pem"; changed=1
fi
if [ ! -f "$DEST/privkey.pem" ] || ! cmp -s "$KEY" "$DEST/privkey.pem"; then
	install -m 640 -o root -g heresay "$KEY" "$DEST/privkey.pem"; changed=1
fi

if [ "$changed" = 1 ]; then
	echo "cert-copy: cert for $DOMAIN changed; restarting heresay (graceful countdown)."
	# ⛔ GRACEFUL, exactly as deploy.sh restarts (audit-3 F5). A bare
	# `systemctl restart` here kicked everyone with no warning; instead ask the
	# RUNNING daemon to announce and count down first — it holds the connections,
	# so the warning has to be broadcast BY it over its control socket. This
	# blocks until the last checkpoint, then the restart lands.
	#
	# ⚠ No chicken-and-egg: the cert on disk is already swapped above, and the
	# countdown window is served by the still-valid OLD cert (Let's Encrypt renews
	# weeks before expiry), so only the restart that LOADS the new cert waits.
	# ⚠ Same tolerance as deploy.sh: if the control socket is unavailable (daemon
	# down, or too old to have one) the countdown fails and we restart anyway — a
	# missed warning is better than a skipped cert swap. `cd /opt/heresay` because
	# `-m heresay` resolves only from the deployed oak (the venv installs the
	# dependencies, not the package), the same reason WorkingDirectory is set.
	cd /opt/heresay && /opt/heresay/.venv/bin/python -m heresay \
		-c /etc/heresay/config.toml --restart-countdown \
		|| echo "cert-copy: no countdown (control socket unavailable); restarting anyway." >&2
	systemctl restart heresay
else
	echo "cert-copy: cert unchanged; no restart."
fi
