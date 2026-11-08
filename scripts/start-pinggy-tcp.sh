#!/usr/bin/env bash
set -euo pipefail
# Run on the machine that can reach the Modbus service (usually the local Docker host).
# This creates a temporary public TCP tunnel. Stop with Ctrl+C immediately after the authorized test.
# Install OpenSSH client first. Pinggy may display a generated public host/port after connection.
MODBUS_HOST="${MODBUS_HOST:-127.0.0.1}"
MODBUS_PORT="${MODBUS_PORT:-5020}"
echo "WARNING: this publishes an unauthenticated synthetic Modbus service to the Internet."
echo "Use only for a short authorized test; do not tunnel real OT networks."
read -r -p "Type I-OWN-THIS-LAB to continue: " confirm
[[ "$confirm" == "I-OWN-THIS-LAB" ]] || { echo "Cancelled"; exit 1; }
ssh -p 443 -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R0:"${MODBUS_HOST}":"${MODBUS_PORT}" tcp@a.pinggy.io
