#!/bin/bash
set -e

# Install Python dependencies
pip install --user -r /workspaces/wgkex/requirements_lock.txt


# Set up WireGuard interfaces for testing
interface_linklocal() {
  # Generate a predictable v6 address
  local macaddr="$(echo $1 | md5sum | sed 's/^\(..\)\(..\)\(..\)\(..\)\(..\).*$/02:\1:\2:\3:\4:\5/')"
  local oldIFS="$IFS"; IFS=':'; set -- $macaddr; IFS="$oldIFS"
  echo "fe80::$1$2:$3ff:fe$4:$5$6"
}

sysctl -w net.ipv6.conf.all.addr_gen_mode=1

for iface in wg-nodes wg-welt; do
    wireguard-go $iface
    wg genkey | wg set $iface private-key /dev/stdin
    addr=$(interface_linklocal $(wg show $iface public-key))
    ip addr add $addr dev $iface
    ip link set $iface up
done

wg set wg-nodes listen-port 40000
wg set wg-welt listen-port 20100

# TODO get address from wg interface
# ip link add vx-welt type vxlan id 99 dstport 0 local $addr dev wg-welt
# ip addr add fe80::1/64 dev vx-welt
# ip link set vx-welt up


# Start NetBox in the background (pulling the images and initial setup may take a while, and we don't want to block the container startup)
cd /workspaces/netbox-docker && SKIP_SUPERUSER=false docker-compose up -d &

exec "$@"
