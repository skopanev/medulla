"""Pre-push gate: would this push break the people who already run us, and does it
name anyone it should not.

WHY A GATE AND NOT A CONVENTION. Downstream users upgrade on their own schedule and
find out what changed by their runs failing. A breaking change is not forbidden here —
it is a DECISION, and the only thing this refuses is making that decision silently: it
stops, says what breaks and for whom, and hands the choice back. Either ship it as a
major bump, or ship it knowingly with the guard turned off for that push.

WHAT IS DETERMINISTIC AND WHAT IS NOT. Known names are a grep — exact, instant, and
never wrong about the words it knows, so it runs FIRST and costs nothing. The model is
there for the rest: a project name nobody put on the list, and compatibility, which
cannot be decided by pattern. Both run; neither replaces the other.

THE MODEL RUNS THROUGH THE ENGINE, not through a subprocess of its own. Credentials,
timeout, retry, the attempt log and an artifact that outlives the terminal are things
this repository already does properly once; a hook that reimplemented them would be a
second, worse copy — and the run directory is what lets anyone ask afterwards what the
gate actually saw. The tree goes in read-only: the reviewer has no business editing
what it is judging.

FAIL-CLOSED. A check that could not run is not a pass — the day this file was written
we had already been burned by a quota check whose failure branch was `exit 0`. If the
model cannot answer, the push stops with a way to proceed on purpose.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

WORKFLOW = os.environ.get("MEDULLA_COMPAT_WORKFLOW", "compat")
TIMEOUT_S = int(os.environ.get("MEDULLA_COMPAT_TIMEOUT_S", "900"))
RUNS = Path(os.environ.get("MEDULLA_COMPAT_RUNS",
                           Path.home() / ".medulla" / "compat-runs"))
SCRATCH = Path(os.environ.get("MEDULLA_COMPAT_SCRATCH",
                              Path.home() / ".medulla" / "compat-gate"))
EMPTY = "0" * 40

# Names that must never appear in this repository: other people's projects, their
# tickets, their teams. A comment saying WHY something was done is welcome; whose
# outage taught it to us is not ours to publish.
FORBIDDEN = [r"finik", r"fback-[a-z0-9]+", r"whitebit", r"healthium"]

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def parse_push_refs(text: str) -> list[tuple[str, str]]:
    """pre-push feeds `<local ref> <local sha> <remote ref> <remote sha>` per line."""
    refs = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[1] != EMPTY:      # deletions push nothing new
            refs.append((parts[1], parts[3]))
    return refs


def range_for(local: str, remote: str) -> str:
    """What git is about to reconcile — the one fact only the hook knows."""
    if remote != EMPTY:
        return f"{remote}..{local}"
    # A branch the remote has never seen: judge it on its own commits, not on the
    # whole history behind it.
    base = run(["git", "merge-base", local, "origin/HEAD"]).stdout.strip()
    return f"{base}..{local}" if base else local


def diff_for(rng: str) -> str:
    """A local copy for the grep only. The ENGINE collects its own subject from the
    same range, so nothing is handed across and the two cannot drift apart."""
    return run(["git", "diff", "--no-color", rng]).stdout


def version_at(sha: str) -> str:
    if sha == EMPTY:
        return ""
    out = run(["git", "show", f"{sha}:pyproject.toml"]).stdout
    m = re.search(r'^version\s*=\s*"([^"]+)"', out, re.M)
    return m.group(1) if m else ""


def major_bumped(old: str, new: str) -> bool:
    """A major bump is the sanctioned way to break: 4.x -> 5.0.0."""
    try:
        return int(new.split(".")[0]) > int(old.split(".")[0])
    except (ValueError, IndexError):
        return False


# The gate itself has to SPELL the words it forbids, and so do its tests. Without
# this the very commit that adds the list cannot be pushed — the check would fire on
# its own definition, and the only ways out would be obfuscating the list or turning
# the gate off to install the gate. Narrow on purpose: two exact paths, not a pattern
# anyone can widen by naming a file conveniently.
SELF = ("cli-agent/scripts/hooks/compat_guard.py", "cli-agent/tests/test_compat_guard.py")


def added_lines(diff: str) -> list[tuple[str, str]]:
    """(file, line) for every ADDED line — the file, so the gate can exempt itself."""
    out, current = [], ""
    for ln in diff.splitlines():
        if ln.startswith("+++ b/"):
            current = ln[6:]
        elif ln.startswith("+") and not ln.startswith("+++"):
            out.append((current, ln))
    return out


def forbidden_names(diff: str) -> list[str]:
    """Known names, found exactly — in ADDED lines only, or history stays red forever."""
    hits = []
    for path, line in added_lines(diff):
        if path in SELF:
            continue
        for pattern in FORBIDDEN:
            for found in re.findall(pattern, line, re.I):
                hits.append(f"{found}  ->  {line.strip()[:100]}")
    return sorted(set(hits))


def ask_engine(rng: str, old: str, new: str) -> dict:
    """Run the gate workflow and return its verdict, or raise so we fail closed.

    ON THE HOST, and from an EMPTY DIRECTORY. A container would give a read-only tree,
    but it re-checks and can reinstall the engine on every start, and this runs on
    every push: measured, 17s of actual review inside a 71s run. Running it from a
    scratch directory is what replaces the read-only mount — the gate is told where to
    ask git and which range, and never has the repository open in front of it.
    """
    RUNS.mkdir(parents=True, exist_ok=True)
    repo = run(["git", "rev-parse", "--show-toplevel"]).stdout.strip() or "."
    # A FIXED empty directory, not a fresh temp one: agy refuses to run unattended in
    # a workspace it has never been trusted in, and a new random path is untrusted
    # every single time. This one is trusted once, at install, and stays empty — the
    # diff and the verdict live in the run directory, not here.
    SCRATCH.mkdir(parents=True, exist_ok=True)
    res = run(["medulla", "--runs-folder", str(RUNS), "--print-run-dir",
               "-w", WORKFLOW, "--var", f"RANGE={rng}", "--var", f"REPO={repo}",
               "--var", f"OLD={old}", "--var", f"NEW={new}"],
              timeout=TIMEOUT_S, cwd=str(SCRATCH))
    run_dir = next((ln.strip() for ln in res.stdout.splitlines()
                    if ln.strip().startswith("/")), "")
    if not run_dir:
        raise RuntimeError((res.stderr or res.stdout or "medulla printed no run dir")
                           .strip().splitlines()[-1][:300])
    verdict = Path(run_dir) / "artifacts" / "verdict.json"
    if not verdict.is_file():
        # The engine's own post hook already refuses a malformed artifact, so getting
        # here means the round never produced one at all.
        raise RuntimeError(f"the gate produced no verdict (rc={res.returncode}); "
                           f"see {run_dir}")
    try:
        return json.loads(verdict.read_text())
    except ValueError as exc:
        raise RuntimeError(f"{verdict} is not readable JSON: {exc}") from None


def report(breaking, names, old, new) -> str:
    """What stopped the push, and the two ways forward. Both are legitimate."""
    out = ["", "=" * 72, "PUSH STOPPED", "=" * 72]
    if names:
        out += ["", "PROJECT NAMES IN ADDED LINES — these belong to other people:", ""]
        out += [f"  {n}" for n in names]
        out += ["", "  A comment explaining WHY a thing was done is welcome. Whose outage",
                "  taught it to us is not ours to publish. Reword and commit again."]
    if breaking:
        out += ["", f"BACKWARD COMPATIBILITY BROKEN  ({old or '?'} -> {new or '?'}):", ""]
        for ch in breaking:
            out.append(f"  - {ch.get('what', '(unnamed change)')}")
            if ch.get("who_breaks"):
                out.append(f"      breaks: {ch['who_breaks']}")
            if ch.get("migration"):
                out.append(f"      migration: {ch['migration']}")
        nxt = (old or "0.0.0").split(".")[0]
        nxt = f"{int(nxt) + 1}.0.0" if nxt.isdigit() else "the next major"
        out += ["", "  This is a DECISION, not a verdict. Two honest ways forward:", "",
                f"    1. Ship it as a break: bump the MAJOR version to {nxt} in",
                "       pyproject.toml and say so in the commit message.", "",
                "    2. Ship it anyway, knowingly, this once:",
                "         MEDULLA_COMPAT_GUARD=off git push"]
    out += ["", "=" * 72, ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if os.environ.get("MEDULLA_COMPAT_GUARD", "").lower() in ("off", "0", "no"):
        print("compat guard: OFF by request", file=sys.stderr)
        return 0

    refs = parse_push_refs(sys.stdin.read() if not sys.stdin.isatty() else "")
    if not refs:
        return 0
    local, remote = refs[0]
    rng = range_for(local, remote)
    diff = diff_for(rng)
    if not diff.strip():
        return 0
    old, new = version_at(remote), version_at(local)

    # THE CHEAP CHECK FIRST. The known names are a grep: instant, exact, needing no
    # model at all. Finding one here saves a minute of engine time and a paid round,
    # and asking anyone would not improve the answer.
    names = forbidden_names(diff)
    if names:
        print(report([], names, old, new), file=sys.stderr)
        return 1

    print(f"compat guard: {WORKFLOW} is reading {len(diff) // 1000}kB of changes "
          f"({old or '?'} -> {new or '?'})...", file=sys.stderr)
    try:
        verdict = ask_engine(rng, old, new)
    except Exception as exc:                       # noqa: BLE001 — every failure is one answer
        # A CHECK THAT COULD NOT RUN IS NOT A PASS. "The gate was down, so presumably
        # nothing breaks" is how a gate becomes decoration.
        print(f"\ncompat guard: COULD NOT CHECK - {exc}\n"
              "  This is not a pass. Fix it, or push on purpose:\n"
              "    MEDULLA_COMPAT_GUARD=off git push\n", file=sys.stderr)
        return 1

    model_names = sorted({f"{n.get('name')}  ->  {n.get('where', '')}"[:140]
                          for n in verdict.get("project_names") or []})
    breaking = verdict.get("breaking_changes") or [] if verdict.get("breaking") else []
    if breaking and major_bumped(old, new):
        breaking = []                              # declared and versioned: that IS the way

    if not model_names and not breaking:
        print("compat guard: no break, no names - pushing", file=sys.stderr)
        return 0
    print(report(breaking, model_names, old, new), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
