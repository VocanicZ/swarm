#!/bin/bash
# swarm spike: install HTCondor execute point on the desktop. Run as root.
set -e
PW=$(cat /home/nathanielsong/.config/swarm/pool-password)
GET_HTCONDOR_PASSWORD="$PW" bash /home/nathanielsong/.cache/get-htcondor.sh --no-dry-run --ep 192.168.1.111
cat > /etc/condor/config.d/99-swarm-spike.config <<CFG
NETWORK_INTERFACE = 192.168.1.123
CFG
systemctl restart condor
