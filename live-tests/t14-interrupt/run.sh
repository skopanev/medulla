#!/usr/bin/env bash
# audit R1 live: SIGINT to medulla must kill the live agent's process group,
# write outcome interrupted, and leave the run resumable.
# NB: a backgrounded job of a NON-INTERACTIVE shell inherits SIGINT=SIG_IGN
# (python then never installs KeyboardInterrupt) — so the harness is python.
set -u
cd "$(dirname "$0")"
rm -rf runs
exec python3 - <<'PY'
import json, os, signal, subprocess, sys, tempfile, time, glob

here = os.getcwd()
work = tempfile.mkdtemp()
proc = subprocess.Popen(["medulla", "-w", here], cwd=work,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(12)                                   # let the live agent start
run_dirs = glob.glob(os.path.join(here, "runs", "*"))
proc.send_signal(signal.SIGINT)
rc = proc.wait(timeout=30)
time.sleep(2)
orphans = subprocess.run(["pgrep", "-f", run_dirs[0] if run_dirs else "prompt.md"],
                         capture_output=True).stdout.decode().split()
outcome = ""
try:
    outcome = json.load(open(os.path.join(run_dirs[0], "outcome.json")))["outcome"]
except Exception:
    pass
ok = rc == 130 and not orphans and outcome == "interrupted"
print(("PASS" if ok else "FAIL") +
      f": rc={rc} orphans={len(orphans)} outcome={outcome}")
sys.exit(0 if ok else 1)
PY
