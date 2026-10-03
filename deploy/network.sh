#!/usr/bin/env bash
# Only agent-br traffic is affected. Public SSH and unrelated Docker networks remain untouched.
set -Eeuo pipefail
umask 077
[[ ${EUID} -eq 0 ]] || { printf '%s\n' 'network.sh requires root.' >&2; exit 1; }

BRIDGE=agent-br
NETWORK=agent-net
SUBNET=172.29.0.0/24
GATEWAY=172.29.0.1
CONFIG_DIR=/etc/agent-cloud/docker-client
install -d -m 0750 /run/agent-cloud
exec 9>/run/agent-cloud/network.lock
flock -x 9
docker_cmd() { docker --config "$CONFIG_DIR" "$@"; }
ipt() { iptables -w 10 "$@"; }
ip6t() { ip6tables -w 10 "$@"; }

if ! docker_cmd network inspect "$NETWORK" >/dev/null 2>&1; then
    docker_cmd network create --driver bridge --subnet "$SUBNET" --gateway "$GATEWAY" \
        --opt com.docker.network.bridge.name="$BRIDGE" \
        --opt com.docker.network.bridge.enable_icc=false "$NETWORK" >/dev/null
fi
docker_cmd network inspect "$NETWORK" | python3 -c '
import json, sys
n = json.load(sys.stdin)[0]
options = n.get("Options", {})
valid = (n.get("Driver") == "bridge" and not n.get("EnableIPv6")
         and any(c.get("Subnet") == "172.29.0.0/24" and c.get("Gateway") == "172.29.0.1"
                 for c in n.get("IPAM", {}).get("Config", []))
         and options.get("com.docker.network.bridge.name") == "agent-br"
         and options.get("com.docker.network.bridge.enable_icc") == "false")
if not valid:
    raise SystemExit("agent-net configuration differs from the required isolated network; refusing to continue.")
'

# Docker must have installed its forward chain. Do not silently skip restrictions.
ipt -nL DOCKER-USER >/dev/null
guard_comment=agent-cloud-network-update
for parent in DOCKER-USER INPUT; do
    while ipt -C "$parent" -i "$BRIDGE" -m comment --comment "$guard_comment" -j DROP 2>/dev/null; do
        ipt -D "$parent" -i "$BRIDGE" -m comment --comment "$guard_comment" -j DROP
    done
    ipt -I "$parent" 1 -i "$BRIDGE" -m comment --comment "$guard_comment" -j DROP
done
# If a later operation fails these guards remain, so a partially installed policy fails closed.
for chain in AGENT-CLOUD-FWD AGENT-CLOUD-HOST; do
    ipt -nL "$chain" >/dev/null 2>&1 || ipt -N "$chain"
    ipt -F "$chain"
done

# Every packet from a sandbox to the host must be to the dedicated model proxy.
# Deliberately do not allow arbitrary established host connections.
ipt -A AGENT-CLOUD-HOST -s "$SUBNET" -d "$GATEWAY" -p tcp --dport 8090 -j ACCEPT
ipt -A AGENT-CLOUD-HOST -j DROP

ipt -A AGENT-CLOUD-FWD -m conntrack --ctstate INVALID -j DROP
# Filter destination ranges before established-connection handling, including Alibaba IMDS.
for destination in 100.100.100.200/32 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 \
                   127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.168.0.0/16 \
                   192.0.0.0/24 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4; do
    ipt -A AGENT-CLOUD-FWD -d "$destination" -j DROP
done
# Public DNS and HTTPS only; package repositories in the image build use the host network.
# Runner must specify public DNS (e.g. --dns=223.5.5.5 --dns=1.1.1.1), not a private VPC resolver.
ipt -A AGENT-CLOUD-FWD -p udp --dport 53 -j RETURN
ipt -A AGENT-CLOUD-FWD -p tcp --dport 53 -j RETURN
ipt -A AGENT-CLOUD-FWD -p tcp --dport 443 -j RETURN
ipt -A AGENT-CLOUD-FWD -j DROP

# Disable the IPv6 escape path even when the host itself supports IPv6.
for parent in INPUT FORWARD; do
    while ip6t -C "$parent" -i "$BRIDGE" -m comment --comment agent-cloud-no-ipv6 -j DROP 2>/dev/null; do
        ip6t -D "$parent" -i "$BRIDGE" -m comment --comment agent-cloud-no-ipv6 -j DROP
    done
    ip6t -I "$parent" 1 -i "$BRIDGE" -m comment --comment agent-cloud-no-ipv6 -j DROP
done

while ipt -C INPUT -i "$BRIDGE" -j AGENT-CLOUD-HOST 2>/dev/null; do
    ipt -D INPUT -i "$BRIDGE" -j AGENT-CLOUD-HOST
done
ipt -I INPUT 1 -i "$BRIDGE" -j AGENT-CLOUD-HOST
while ipt -C DOCKER-USER -i "$BRIDGE" -j AGENT-CLOUD-FWD 2>/dev/null; do
    ipt -D DOCKER-USER -i "$BRIDGE" -j AGENT-CLOUD-FWD
done
ipt -I DOCKER-USER 1 -i "$BRIDGE" -j AGENT-CLOUD-FWD
for parent in DOCKER-USER INPUT; do
    while ipt -C "$parent" -i "$BRIDGE" -m comment --comment "$guard_comment" -j DROP 2>/dev/null; do
        ipt -D "$parent" -i "$BRIDGE" -m comment --comment "$guard_comment" -j DROP
    done
done
printf '%s\n' 'agent-net ready: peer/private/metadata/IPv6 blocked; gateway:8090 and public DNS/HTTPS allowed.'
