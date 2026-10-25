#!/bin/bash
# swarm spike: install HTCondor execute point on the desktop. Run as root.
set -e
PW=$(cat /home/owner/.config/swarm/pool-password)
GET_HTCONDOR_PASSWORD="$PW" bash /home/owner/.cache/get-htcondor.sh --no-dry-run --ep 10.0.0.1
cat > /etc/condor/config.d/99-swarm-spike.config <<CFG
NETWORK_INTERFACE = 10.0.0.2
CFG
systemctl restart condor
