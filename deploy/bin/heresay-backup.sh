#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Nightly backup of the WHOLE INSTANCE — state, configuration and house.
#
# ⛔ THE TARBALL DELIBERATELY DOES NOT CONTAIN `mail.key` (v1.48.0, audit E1).
#
# Mail and .revtell buffers are encrypted at rest with Fernet, and `mail.key` is
# what decrypts them. Tarring the whole state dir put the key in the same file as
# the ciphertext — and that file is the one artifact that LEAVES the machine
# (`make fetch-backup` pulls it weekly to a second host). Encryption that travels
# with its key is not encryption; it is a filename. So the key is excluded here.
#
# ⚠ THE KEY THEREFORE NEEDS ONE DELIBERATE COPY, MADE BY HAND, ONCE.
# It is generated on first run and never rotates, so it does not want a nightly
# copy — it wants a single careful one, stored somewhere these tarballs are not
# (a password manager, offline media). The exact step is in
# deploy/install/README.md under "The one-time mail.key backup". Until it is
# done, the key exists ONLY on this server, and the encrypted mail in these
# tarballs cannot be read back by anyone, including you.
#
# ⚠ RESTORING NOW NEEDS TWO INPUTS: this tarball AND that separately stored key.
# A restore that only unpacks the tarball comes back with everything except the
# ability to read a single message. The annual restore test is what proves you
# still have both — it is load-bearing now, not a formality.
#
# What IS in here: password hashes (argon2id), encrypted mail and .revtell
# ciphertext, accounts, bans, ban history, the event log, resume-token hashes,
# the operational state, ⚠ the REPORT CAPTURES (plaintext accusations, by
# design — see heresay/capture.py), the /etc configuration including
# notify.env's two secrets, and the whole authored house. Sensitive; kept 0600,
# root-owned, local.
#
# ⛔ A FULL RESTORE IS THIS TARBALL **PLUS `mail.key` FROM BITWARDEN**, and the
# failure without the key is silent: the house comes back with every account,
# ban and room intact and not one readable message.
set -euo pipefail

# ⛔ THE WHOLE INSTANCE, in three trees (v1.60.0). The settled model is that
# GIT HOLDS THE CODE and THE BACKUP HOLDS THE INSTANCE, with nothing in both —
# so anything that makes this server THIS house, rather than a fresh checkout,
# has to be in here:
#
#   /var/lib/heresay   accounts, mail, .revtell, bans + history, events,
#                      resume tokens, the report captures, state.json
#   /etc/heresay       config.toml, permissions.toml, forbidden_names.txt, and
#                      notify.env — ⚠ the ntfy topic and the hc-ping URL are
#                      instance secrets that exist NOWHERE else, not even in
#                      the repo (it ships placeholders only)
#   /opt/heresay/house rooms, map, echoes, greeting, agreement, help, theme
#
# ⚠ `/opt/heresay/house` is the ONE thing here that also has a copy in a git
# checkout — and it is in the backup anyway, because the checkout is a
# developer's machine rather than a guarantee, and because an edit made live
# through `make deploy-content` is the house whether or not anyone committed it.
# Nothing else under /opt is included: that IS the code, and git has it.
SRCS=("/var/lib/heresay" "/etc/heresay" "/opt/heresay/house")
DIR="/var/backups/heresay"
KEEP=14

mkdir -p "$DIR"; chmod 700 "$DIR"; chown root:root "$DIR"
STAMP="$(date -u +%Y%m%d)"
OUT="$DIR/heresay-$STAMP.tar.gz"

# ⛔ Every exclude here is key material. A new secret in the state dir means a
# new line here — tests/test_deploy_scripts.py reads these, so a key that lands
# in the tarball fails the suite rather than quietly riding offsite for a year.
# ⚠ Absolute paths with a leading `/` stripped by tar, so the tarball unpacks
# to `var/…`, `etc/…`, `opt/…` — a restore is a deliberate copy out of those,
# never an accidental `tar -x` over the running system's root.
MISSING=()
for s in "${SRCS[@]}"; do [ -e "$s" ] || MISSING+=("$s"); done
if [ ${#MISSING[@]} -gt 0 ]; then
	echo "backup: FATAL — missing ${MISSING[*]}; refusing a partial instance backup." >&2
	exit 1
fi
tar -czf "$OUT" -P --transform='s,^/,,' \
	--exclude='mail.key' \
	--exclude='*/mail.key' \
	"${SRCS[@]}"
chmod 600 "$OUT"; chown root:root "$OUT"

# Belt, because the cost of being wrong here is silent and lasts as long as the
# backup does: look inside and refuse to keep a tarball with a key in it.
if tar -tzf "$OUT" | grep -q 'mail\.key$'; then
	echo "backup: FATAL — mail.key is inside $OUT; refusing to keep it." >&2
	rm -f "$OUT"
	exit 1
fi

# Keep only the newest $KEEP.
ls -1t "$DIR"/heresay-*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup: wrote $OUT (mail.key excluded — see deploy/install/README.md)"
