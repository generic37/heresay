#!/usr/bin/env bash
# Copyright (C) 2026 BigPoetry
# SPDX-License-Identifier: AGPL-3.0-or-later

# make lan — serve the web client to the home LAN.
#
# Starts the heresay daemon and a static server for web/ (port 8099) together, in
# the foreground; Ctrl-C stops both. Prints the exact URL other machines must use
# (with ?ws=), filled in with the real Windows LAN IP.
#
# Usage: scripts/lan.sh [config.toml]     (make lan uses the real config.toml)
# For dev verification, pass a throwaway config so real state is never touched.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
CONFIG="${1:-config.toml}"
PY="$DIR/.venv/bin/python"
WEB_PORT=8099

# WS port comes from the chosen config (default 8765).
WS_PORT="$(grep -E '^[[:space:]]*ws_port[[:space:]]*=' "$CONFIG" 2>/dev/null \
            | head -1 | grep -oE '[0-9]+' || true)"
WS_PORT="${WS_PORT:-8765}"

# Discover the Windows LAN IP (192.168 / 10.x / 172.16-31), skipping virtual
# adapters (WSL vEthernet, Hyper-V, loopback, link-local).
lan_ip() {
    powershell.exe -NoProfile -Command "
        Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { \$_.IPAddress -match '^(192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)' -and
                       \$_.InterfaceAlias -notmatch 'WSL|Loopback|vEthernet|Hyper-V' } |
        Sort-Object -Property @{Expression={ if (\$_.IPAddress -like '192.168.*'){0} elseif (\$_.IPAddress -like '10.*'){1} else {2} }} |
        Select-Object -First 1 -ExpandProperty IPAddress
    " 2>/dev/null | tr -d '\r\n'
}

LANIP="$(lan_ip || true)"
[ -z "$LANIP" ] && LANIP="<your-windows-lan-ip>"

echo "======================================================================"
echo " heresay LAN portal"
echo "   config: $CONFIG"
echo
echo " LAN portal: http://$LANIP:$WEB_PORT/?ws=ws://$LANIP:$WS_PORT"
echo " (other machines must use this exact URL, including the ?ws= part)"
echo
echo " If the LAN URL stops working after a Windows reboot, re-run"
echo " scripts/lan-forward.ps1 as admin (the WSL IP changes across reboots)."
echo " Ctrl-C stops both the daemon and the web server."
echo "======================================================================"

# Start both; Ctrl-C (or either exiting) tears down the other.
"$PY" -m heresay -c "$CONFIG" &
DAEMON=$!
"$PY" -m http.server "$WEB_PORT" --directory web --bind 0.0.0.0 &
WEBSRV=$!

cleanup() { trap - INT TERM EXIT; kill "$DAEMON" "$WEBSRV" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup INT TERM EXIT

# If either process dies, stop the other too.
wait -n "$DAEMON" "$WEBSRV" 2>/dev/null || true
cleanup
