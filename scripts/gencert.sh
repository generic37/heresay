#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# Generate a self-signed certificate for the dev TLS listener.
# NOT for production — for a real deployment use a proper CA (Let's Encrypt).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERTDIR="$DIR/certs"
mkdir -p "$CERTDIR"

CN="${1:-localhost}"

openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$CERTDIR/key.pem" \
    -out "$CERTDIR/cert.pem" \
    -days 825 \
    -subj "/CN=$CN" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"

chmod 600 "$CERTDIR/key.pem"
echo "Wrote $CERTDIR/cert.pem and $CERTDIR/key.pem (CN=$CN)"
