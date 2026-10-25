#!/usr/bin/env bash
# usage: run.sh SHARD OUT ; resumes from OUT/n, counts to 240, one step per TOY_STEP_SECONDS (default 1)
set -euo pipefail
shard=$1 out=$2
step=${TOY_STEP_SECONDS:-1}
mkdir -p "$out"
n=$(cat "$out/n" 2>/dev/null || echo 0)
echo "shard $shard starting at n=$n"
sum=0; for ((i=1;i<=n;i++)); do sum=$((sum+i)); done
while ((n<240)); do
  sleep "$step"
  n=$((n+1)); sum=$((sum+n))
  if ((n%10==0)); then echo "$n" > "$out/n.tmp" && mv "$out/n.tmp" "$out/n"; fi
done
echo "$shard $sum" > "$out/result.tmp" && mv "$out/result.tmp" "$out/result"
