#!/usr/bin/env python3
"""Fail if a Python file exceeds the LOC budget.

LOC here = lines of CODE: blank lines and comment-only lines do not count
(docstrings and inline `x = 1  # note` code DO count — a comment is only free
when it is the whole line). This is the pre-commit gate; the module split that
brings the grandfathered files under budget is tracked separately.

Usage: check_file_loc.py [--limit N] FILE...
Exit 1 (with a report) if any non-grandfathered file is over the limit.
"""
from __future__ import annotations

import sys
from pathlib import Path

LIMIT = 250

# Files that predate the rule. The gate holds the line for everything else; these
# are split-debt, not a licence to grow — each must come DOWN under the limit, and
# leave this set when it does. Do not add new entries: a new file over budget is
# exactly what the gate exists to stop.
GRANDFATHERED = {
    "cli-agent/medulla/v2/engine.py",     # 916 -> split the node loop / seam / scan
    "cli-agent/medulla/v2/harness.py",    # 343 -> one module per adapter
    "cli-agent/medulla/v2/contract.py",   # 326 -> parse vs validate
    "cli-agent/medulla/init.py",          # 255 -> just over; carve out templating
}


def sloc(path: Path) -> int:
    """Non-blank, non-comment-only lines."""
    n = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        n += 1
    return n


def repo_rel(path: Path) -> str:
    """Path relative to the git repo root, so GRANDFATHERED keys are stable
    regardless of the caller's cwd (pre-commit passes repo-relative paths)."""
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def main(argv: list[str]) -> int:
    limit = LIMIT
    files: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--limit":
            limit = int(next(it))
        else:
            files.append(arg)

    offenders = []
    for f in files:
        p = Path(f)
        if p.suffix != ".py" or not p.is_file():
            continue
        rel = repo_rel(p)
        if rel in GRANDFATHERED:
            continue
        n = sloc(p)
        if n > limit:
            offenders.append((rel, n))

    if offenders:
        print(f"LOC budget exceeded (limit {limit}, blank + comment-only lines excluded):",
              file=sys.stderr)
        for rel, n in sorted(offenders, key=lambda x: -x[1]):
            print(f"  {n:>5}  {rel}  (+{n - limit})", file=sys.stderr)
        print("Split the file into focused modules to bring it under budget.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
