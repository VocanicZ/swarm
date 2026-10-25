#!/usr/bin/env bash
# usage: merge.sh OUT_DIR... ; writes merged.txt in cwd (the toy dir)
set -euo pipefail
for d in "$@"; do cat "$d/result"; done | sort > merged.txt.tmp
mv merged.txt.tmp merged.txt
