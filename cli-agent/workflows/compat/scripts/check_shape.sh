#!/usr/bin/env bash
# The artifact must be READABLE before the gate acts on it. A verdict that cannot be
# parsed has to fail the step so the engine retries — the alternative, letting the
# hook parse whatever landed, turns a malformed answer into "no findings", which is
# the exact direction a gate must never fail in.
set -uo pipefail

f="${OUT:-$MEDULLA_RUN_DIR/artifacts}/verdict.json"
[ -s "$f" ] || { echo "no verdict.json was written to $f" >&2; exit 1; }

python3 - "$f" <<'PY' || exit 1
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"verdict.json is not readable JSON: {exc}", file=sys.stderr); sys.exit(1)
missing = [k for k in ("breaking", "breaking_changes", "project_names") if k not in data]
if missing:
    print(f"verdict.json is missing {', '.join(missing)}", file=sys.stderr); sys.exit(1)
if not isinstance(data["breaking"], bool):
    print("breaking must be true or false", file=sys.stderr); sys.exit(1)
# A claimed break with nothing named is unactionable, and it is the shape a model
# produces when it is hedging. Make it say WHAT breaks or say that nothing does.
if data["breaking"] and not data["breaking_changes"]:
    print("breaking=true with an empty breaking_changes list", file=sys.stderr); sys.exit(1)
PY
