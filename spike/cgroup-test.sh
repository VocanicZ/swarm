#!/bin/bash
# swarm spike step 4: set CPU/memory caps on HTCondor's job cgroup at runtime. Run as root.
set -e
systemctl start condor
CG=/sys/fs/cgroup/system.slice/condor.service/htcondor
echo "waiting for burn job..."
until pgrep -u nobody -f 'while :; do :; done' >/dev/null; do sleep 2; done
sleep 5
cores() { a=$(awk '/usage_usec/{print $2}' $CG/cpu.stat); sleep 5; b=$(awk '/usage_usec/{print $2}' $CG/cpu.stat); echo "scale=2; ($b-$a)/5000000" | bc; }
echo "cpu.max=$(cat $CG/cpu.max) memory.high=$(cat $CG/memory.high) -> cores used: $(cores)"
echo "150000 100000" > $CG/cpu.max      # cap 1.5 cores
echo 512M > $CG/memory.high
echo "cpu.max=$(cat $CG/cpu.max) memory.high=$(cat $CG/memory.high) -> cores used: $(cores)"
echo "max 100000" > $CG/cpu.max
echo max > $CG/memory.high
echo "reset: cpu.max=$(cat $CG/cpu.max) -> cores used: $(cores)"
