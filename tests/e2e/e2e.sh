#!/usr/bin/env bash
# Acceptance 2 on the coordinator: submit toy, mark when to kill the worker, wait for merge, diff.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
toy=$here/toy
worker=NathanielSong
rm -f "$toy/merged.txt"

swarm submit "$toy"

spool=$(condor_config_val SPOOL)
deadline=$((SECONDS+1800))
marked=0
until [ -f "$toy/merged.txt" ]; do
  if ((SECONDS>deadline)); then echo "TIMEOUT: no merged.txt after 30 min" >&2; exit 2; fi
  if ((!marked)); then
    st=$(swarm status toy || true)
    if grep -q "$worker" <<<"$st" && [ -n "$(find "$spool" -name n -type f 2>/dev/null | head -n1)" ]; then
      echo "KILL WORKER NOW"; marked=1
    fi
  fi
  sleep 5
done
echo "merged.txt appeared"
diff "$toy/merged.txt" "$here/expected.txt"
