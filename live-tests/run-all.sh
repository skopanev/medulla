#!/usr/bin/env bash
# Live battle tests: run before every release push. Real CLIs, real money
# (cents). Work dir is scratch; t7 runs twice (crash then resume) by design.
set -u
cd "$(dirname "$0")"
work=$(mktemp -d)
pass=0; fail=0
for t in t1-claude t2-codex t3-opencode t4-agy t5-panel t6-fallback t8-foreach-dynamic t9-fold t10-array-source t11-chain t12-preguard t13-retry-post t15-timeout-live t17-dirty-data t18-empty-live t19-stress-pool t20-hetero; do
  rm -rf "$t/runs"
  if (cd "$work" && medulla -w "$OLDPWD/$t" >/dev/null 2>&1); then
    echo "PASS $t"; pass=$((pass+1))
  else
    echo "FAIL $t (runs/ has the logs)"; fail=$((fail+1))
  fi
done
rm -rf t7-resume/runs
(cd "$work" && medulla -w "$OLDPWD/t7-resume" >/dev/null 2>&1)
touch "$work/unstick"
if (cd "$work" && medulla -w "$OLDPWD/t7-resume" --resume >/dev/null 2>&1) \
   && [ "$(cat "$work/count-slowpoke" | wc -l | tr -d ' ')" = "2" ] \
   && [ "$(cat "$work/count-one" | wc -l | tr -d ' ')" = "1" ]; then
  echo "PASS t7-resume"; pass=$((pass+1))
else
  echo "FAIL t7-resume"; fail=$((fail+1))
fi
for s in t14-interrupt/run.sh t16-concurrent/run.sh; do
  if "$s" >/dev/null 2>&1; then echo "PASS $(dirname $s)"; pass=$((pass+1));
  else echo "FAIL $(dirname $s)"; fail=$((fail+1)); fi
done
echo "── $pass passed, $fail failed"
exit $((fail > 0))
