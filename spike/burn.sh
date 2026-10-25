#!/bin/sh
# 4 busy loops for 5 minutes
for i in 1 2 3 4; do timeout 300 sh -c 'while :; do :; done' & done
wait
