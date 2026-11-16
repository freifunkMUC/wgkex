#!/bin/bash
# Installs everything the e2e rig needs on an Ubuntu 24.04 host (idempotent). Needs root.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
VENV=${E2E_VENV:-/opt/wgkex-e2e/venv}

export DEBIAN_FRONTEND=noninteractive
if ! command -v mosquitto >/dev/null || ! command -v wg >/dev/null || ! command -v busybox >/dev/null ||
	! command -v jq >/dev/null; then
	apt-get update -q
	apt-get install -y -q --no-install-recommends mosquitto wireguard-tools jq busybox iproute2 \
		python3-venv python3-pip util-linux procps
fi
# The rig runs its own mosquitto
systemctl disable --now mosquitto >/dev/null 2>&1 || true
for m in wireguard vxlan macvlan dummy; do modprobe "$m"; done

if [ ! -x "$VENV/bin/python" ]; then
	python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip
reqs=()
for r in "$HERE"/src/*/requirements.txt; do
	[ -f "$r" ] && reqs+=(-r "$r")
done
"$VENV/bin/pip" install -q "${reqs[@]}" paho-mqtt PyYAML matplotlib
echo "setup done: $("$VENV/bin/python" --version), kernel $(uname -r)"
