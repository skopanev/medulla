"""`medulla ps` — what is running right now, and on what.

The question this answers is an owner's: "how many lanes are up, and which task is
each one on". It was answerable before only by archaeology — `docker ps` listed
containers named for a timestamp and eight random hex, and the task had to be dug out
of a mount list with `docker inspect`. Containers now carry labels, so docker's own
index is the source of truth: nothing here derives liveness from files.

That last point is the trap this file exists to avoid. A run directory without
outcome.json is NOT a running run — measured on this machine at a moment when three
containers were up: ten such directories. Seven were abandoned or killed. Counting
them would have reported activity at three times life.
"""
from __future__ import annotations

import json
import subprocess
import sys

LABEL = "medulla.workflow"


def _docker_ps(*filters: str) -> list[dict]:
    cmd = ["docker", "ps", "--format", "{{json .}}"]
    for f in filters:
        cmd.extend(["--filter", f])
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise RuntimeError("docker is not on PATH") from None
    if out.returncode != 0:
        raise RuntimeError((out.stderr or "docker ps failed").strip().splitlines()[0])
    rows = []
    for line in out.stdout.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue                    # one unreadable row must not lose the rest
    return rows


def _label(row: dict, key: str) -> str:
    # docker renders labels as a comma-joined k=v string in the ps template
    for part in (row.get("Labels") or "").split(","):
        if part.startswith(key + "="):
            return part[len(key) + 1:]
    return ""


def ps(argv: list[str]) -> int:
    want, as_json = None, False
    rest = list(argv)
    while rest:
        a = rest.pop(0)
        if a in ("-w", "--workflow"):
            if not rest:
                print("error: -w needs a workflow name", file=sys.stderr)
                return 1
            want = rest.pop(0)
        elif a == "--json":
            as_json = True
        elif a in ("-h", "--help"):
            print("usage: medulla ps [-w <workflow>] [--json]")
            return 0
        else:
            print(f"error: unknown argument: {a}", file=sys.stderr)
            return 1

    # TWO QUERIES, SEPARATELY SURVIVABLE. One `docker ps` failed while writing this —
    # a transient snapshotter error from the daemon — and the second answered fine a
    # second later. Wrapping both in one try meant a blip on either killed the whole
    # answer, including the part that was already in hand. Report what came back, name
    # what did not, and fail only when nothing did.
    errors = []
    try:
        labelled = _docker_ps(f"label={LABEL}")
    except RuntimeError as exc:
        labelled, _ = [], errors.append(f"labelled runs: {exc}")
    try:
        # Containers from before labels existed, and any started by a caller that
        # bypassed the launcher. Reporting only the labelled ones would answer "how
        # many are running" with a number that is quietly too small — the same error
        # class this command exists to end.
        all_medulla = _docker_ps("name=^medulla-")
    except RuntimeError as exc:
        all_medulla, _ = [], errors.append(f"unlabelled containers: {exc}")
    if len(errors) == 2:
        print(f"error: {errors[0]}", file=sys.stderr)
        return 1

    known = {r.get("ID") for r in labelled}
    unlabelled = [r for r in all_medulla if r.get("ID") not in known]

    runs = [{"id": (r.get("ID") or "")[:12],
             "workflow": _label(r, LABEL),
             "lane": _label(r, "medulla.run_dir_name") or (r.get("Names") or ""),
             "workspace": _label(r, "medulla.workspace"),
             "up": r.get("Status", "")} for r in labelled]
    if want:
        runs = [r for r in runs if r["workflow"] == want]

    if as_json:
        print(json.dumps({"running": runs, "unlabelled": len(unlabelled)}, indent=1))
        return 0

    if not runs:
        print(f"no running {want or 'medulla'} runs")
    else:
        width = max(len(r["workflow"]) for r in runs)
        lane_w = max(len(r["lane"]) for r in runs)
        for r in runs:
            print(f"{r['workflow']:<{width}}  {r['lane']:<{lane_w}}  "
                  f"{r['up']:<16}  {r['workspace']}")
        print(f"\n{len(runs)} running" + (f" (workflow {want})" if want else ""))
    if unlabelled:
        print(f"note: {len(unlabelled)} more medulla container(s) carry no labels — "
              "started before 4.75.0, or outside `medulla --docker`", file=sys.stderr)
    for err in errors:
        print(f"warning: could not list {err} — this list may be short",
              file=sys.stderr)
    return 0
