#!/bin/bash
# Entry point of the wgkex e2e rig bundle. Needs root (uses sudo if not root).
#
#   run.sh local [PLAN]                         everything on this host
#   run.sh agent CONTROLLER_IP                  gateway/node hosts: start the agent and return
#   run.sh controller "GW_IPS" NODES_IP [PLAN]  broker host: run the plan on all hosts
#   run.sh controller-bg ... / run.sh status    same in the background (systemd unit), poll status
#
# GW_IPS has one address per gateway (4 gateways: weights 30,30,20,20), e.g. "10.1.0.5 10.1.0.6
# 10.1.0.7 10.1.0.8"; addresses may repeat to put several gateways on one host. PLAN is a file in
# plans/ (default: $PLAN or stack). Sources to test are expected in src/<name>/ (wgkex repo trees).
# Results: results/<run>/..., results/summary.{json,md}, results/*/timeline.png
set -euo pipefail
# The node simulator runs ~3 fds per concurrent reconnect
ulimit -n 1048576 2>/dev/null || true
[ "$(id -u)" = 0 ] || exec sudo -E "$0" "$@"
HERE=$(cd "$(dirname "$0")" && pwd)
cd "$HERE"
VENV=${E2E_VENV:-/opt/wgkex-e2e/venv}
PY="$VENV/bin/python"
RESULTS=${RESULTS_DIR:-$HERE/results}
mode=${1:-local}
shift || true

run_plan() {
	local plan=$1 ctl_ip=$2 gw_ips=$3 nodes_ip=$4 failed=0
	mkdir -p "$RESULTS"
	cp "$plan" "$RESULTS/plan.txt"
	while read -r name src args; do
		case "$name" in '' | '#'*) continue ;; esac
		if [ ! -d "src/$src" ]; then
			echo "skipping $name: src/$src missing" | tee -a "$RESULTS/controller.log"
			continue
		fi
		echo "=== $(date -u +%FT%TZ) $name ($src) $args" | tee -a "$RESULTS/controller.log"
		# shellcheck disable=SC2086
		if ! "$PY" scenario.py --run "$name" --src "src/$src" --label "$src" --out "$RESULTS" \
			--ctl-ip "$ctl_ip" --gw-ips "$gw_ips" --nodes-ip "$nodes_ip" \
			--nodes "${NODES:-2000}" $args >>"$RESULTS/controller.log" 2>&1; then
			echo "run $name failed" | tee -a "$RESULTS/controller.log"
			failed=1
		fi
		"$PY" analyze.py --no-plots "$RESULTS" >/dev/null 2>&1 || true
	done <"$plan"
	"$PY" analyze.py "$RESULTS" | tee "$RESULTS/summary.txt"
	return $failed
}

agent_addr() {
	case "$1" in *:*) echo "$1" ;; *) echo "$1:7700" ;; esac
}

plan_file() {
	local p=${1:-${PLAN:-stack}}
	[ -f "$p" ] || p="plans/$p.txt"
	echo "$p"
}

case "$mode" in
local)
	bash setup_host.sh
	run_plan "$(plan_file "${1:-}")" "" "" ""
	;;
agent)
	ctl=${1:?controller IP}
	bash setup_host.sh
	unit=wgkex-e2e-agent${AGENT_PORT:+-$AGENT_PORT}
	systemctl stop "$unit" 2>/dev/null || true
	systemctl reset-failed "$unit" 2>/dev/null || true
	mkdir -p "$RESULTS"
	systemd-run --unit "$unit" --working-directory "$HERE" -p LimitNOFILE=1048576 \
		-p StandardOutput=append:"$RESULTS/agent.log" -p StandardError=append:"$RESULTS/agent.log" \
		"$PY" rigops.py agent --base "$RESULTS" --allow "$ctl" --port "${AGENT_PORT:-7700}"
	sleep 2
	systemctl is-active "$unit"
	echo "agent running, results in $RESULTS"
	;;
controller)
	gw_ips=${1:?gateway host IPs}
	nodes_ip=${2:?node host IP}
	plan=$(plan_file "${3:-}")
	gw_ips=${gw_ips//,/ }
	# shellcheck disable=SC2086
	set -- $gw_ips
	ctl_ip=$(ip -o route get "${1%:*}" | sed -n 's/.* src \([0-9.]*\).*/\1/p')
	bash setup_host.sh
	for h in $(printf '%s\n' $gw_ips "$nodes_ip" | sort -u); do
		for _ in $(seq 1 120); do
			curl -s -o /dev/null -m 3 -X POST -d '{"method":"ping","args":[],"kwargs":{}}' \
				"http://$(agent_addr "$h")/" && break
			sleep 10
		done || true
		curl -sf -m 3 -X POST -d '{"method":"ping","args":[],"kwargs":{}}' "http://$(agent_addr "$h")/" ||
			{ echo "agent on $h not reachable"; exit 2; }
		echo
	done
	run_plan "$plan" "$ctl_ip" "$gw_ips" "$nodes_ip"
	;;
controller-bg)
	# same as controller, but as a systemd unit that survives the calling shell; poll with "status"
	mkdir -p "$RESULTS"
	systemctl stop wgkex-e2e-ctl 2>/dev/null || true
	systemctl reset-failed wgkex-e2e-ctl 2>/dev/null || true
	rm -f "$RESULTS/exit_code"
	systemd-run --unit wgkex-e2e-ctl --working-directory "$HERE" -p LimitNOFILE=1048576 -E RESULTS_DIR="$RESULTS" \
		${NODES:+-E NODES="$NODES"} -p StandardOutput=append:"$RESULTS/run.log" \
		-p StandardError=append:"$RESULTS/run.log" \
		bash -c '"$0" controller "$1" "$2" "$3"; echo $? >"$4/exit_code"' \
		"$HERE/run.sh" "${1:?gateway host IPs}" "${2:?node host IP}" "${3:-}" "$RESULTS"
	echo "controller started; poll with: $0 status"
	;;
status)
	if [ -f "$RESULTS/exit_code" ]; then
		echo "FINISHED exit_code=$(cat "$RESULTS/exit_code")"
	elif systemctl is-active -q wgkex-e2e-ctl; then
		echo "RUNNING"
	else
		echo "NOT RUNNING (no exit_code)"
	fi
	grep -a '^===\|MARK\|failed\|skipping' "$RESULTS/controller.log" 2>/dev/null | tail -5
	;;
*)
	echo "usage: $0 local|agent|controller|controller-bg|status ..." >&2
	exit 2
	;;
esac
