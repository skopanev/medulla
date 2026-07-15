#!/usr/bin/env bash
# two simultaneous runs of ONE pipeline: separate run dirs, both green
set -u
cd "$(dirname "$0")"
rm -rf runs
w1=$(mktemp -d); w2=$(mktemp -d)
(cd "$w1" && medulla -w "$OLDPWD" >/dev/null 2>&1) & p1=$!
(cd "$w2" && medulla -w "$OLDPWD" >/dev/null 2>&1) & p2=$!
wait $p1; rc1=$?; wait $p2; rc2=$?
dirs=$(ls -d runs/*/ | wc -l | tr -d ' ')
[ "$rc1" = 0 ] && [ "$rc2" = 0 ] && [ "$dirs" = 2 ] && echo "PASS: two concurrent runs, $dirs isolated dirs" || { echo "FAIL rc1=$rc1 rc2=$rc2 dirs=$dirs"; exit 1; }
