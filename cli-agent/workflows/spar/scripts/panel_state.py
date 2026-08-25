"""What a panel run already knows, read from the file the engine keeps current.

`wait` used to report "nothing has finished" while two panelists were done and their
artifacts on disk. That was true of the JOURNAL — written when the whole node ends —
and false of the run. The manifest is the record of deliveries: the engine appends a
row the moment each input concludes.

Python rather than jq: medulla is a Python program, so the interpreter is guaranteed
wherever medulla runs, and jq is one more thing that can be missing on a host.

    panel_state.py <run-dir>

Prints one line per panelist, then a machine-readable summary line:

    @@ <delivered> <total> <slug of everyone still running>...
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def _rows(manifest: Path) -> dict:
    """Whole lines only.

    The last row can be caught half-written — the engine is appending to this file
    while we read it. A truncated tail is not corruption, it is the next panelist
    still working, so it is skipped rather than reported.
    """
    rows: dict[str, dict] = {}
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line.endswith("}"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        inp = row.get("input")
        slug = (inp.get("slug") if isinstance(inp, dict) else None) or str(row.get("index"))
        rows[slug] = row
    return rows


def _expected(inputs_path: Path, fallback: list[str]) -> list[str]:
    try:
        data = json.loads(inputs_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    return [i.get("slug") if isinstance(i, dict) else str(i) for i in data]


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: panel_state.py <run-dir>", file=sys.stderr)
        return 2
    run = Path(argv[0])
    step = next((p for p in sorted(run.glob("steps/*panel*")) if p.is_dir()), None)
    if step is None:
        return 1                          # the pool has not started: nothing to report
    rows = _rows(step / "manifest.jsonl")
    expected = _expected(step / "inputs.json", list(rows))
    if not expected:
        return 1

    running, delivered = [], []
    for slug in expected:
        row = rows.get(slug)
        if row is None:
            running.append(slug)
            print(f"  {slug:<10} running")
        elif row.get("ok"):
            delivered.append(slug)
            print(f"  {slug:<10} ok       {int((row.get('duration_s') or 0) // 60)}m")
        else:
            why = row.get("reason") or "failed"
            # first clause only: the full message carries a stack or a JSON event, and
            # this is a status line, not a report
            msg = (row.get("message") or "").split(";")[0][:60]
            print(f"  {slug:<10} out      {why}: {msg}")

    print(f"@@ {len(delivered)} {len(expected)} {' '.join(running)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
