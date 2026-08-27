"""What the collector does with files that do not follow the format.

A model's formatting must never become silent data loss: a lowercase heading dropped a
whole panelist's findings without a word.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml as pyyaml

COLLECTOR = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/collect_verdict.py"

def collect(tmp_path, panelists, *, expected=None, delivered=None, min_decided=3):
    """Run the collector over a round: returns (markdown, parsed json, result)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    for slug, body in panelists:
        (art / f"{slug}.md").write_text(body)
    n = len(panelists)
    res = subprocess.run(
        [sys.executable, str(COLLECTOR), str(tmp_path), str(art),
         "--expected", str(n if expected is None else expected),
         "--delivered", str(n if delivered is None else delivered),
         "--min-decided", str(min_decided)],
        capture_output=True, text=True, check=False)
    return ((tmp_path / "verdict.md").read_text(),
            json.loads((tmp_path / "verdict.json").read_text()), res)


def test_a_lowercase_heading_does_not_lose_a_panelist(tmp_path):
    """Live: sonnet wrote `## Findings` and its whole list was dropped without a word.
    Exact matching turns a model's formatting into silent data loss."""
    md, data, _res = collect(tmp_path, [
        ("sonnet", "## Findings\n- (R) HIGH — leak — a.py:1 — breaks — FIX: guard it\n\n"
                   "## Verdict\nNO-GO — 1 — the leak\n"),
    ], min_decided=1)
    assert len(data["findings"]) == 1
    assert data["panelists"][0]["verdict"] == "NO-GO"
    assert data["blocking"] == ["F1"]
    assert "F1. sonnet" in md


def test_a_file_the_parser_could_not_read_is_named(tmp_path):
    """Never silently omit a delivered model: say what could not be parsed."""
    _md, data, _res = collect(tmp_path, [("x", "just prose, no sections at all\n")],
                              min_decided=1)
    assert data["parser"]["malformed"]["x"] == ["no FINDINGS heading", "no VERDICT heading"]


def test_state_is_the_field_a_gate_branches_on(tmp_path):
    """Any NO-GO, any unresolved verified HIGH, or a round short of quorum."""
    ok = "## FINDINGS\n- (G) LOW — cosmetic — a.py:1 — noise — FIX: drop\n\n## VERDICT\nGO — fine\n"
    _md, clear, _ = collect(tmp_path / "a", [("a", ok), ("b", ok)], min_decided=2)
    assert clear["state"] == "CLEAR"

    high = ("## FINDINGS\n- (R) HIGH — real defect — a.py:1 — breaks — FIX: guard\n\n"
            "## VERDICT\nGO — I would still ship it\n")
    _md, data, _ = collect(tmp_path / "b", [("a", high), ("b", ok)], min_decided=2)
    assert data["state"] == "REVIEW_REQUIRED"      # a verified HIGH outweighs the word
    assert data["verified_high"] == ["F1"]

    _md, short, _ = collect(tmp_path / "c", [("a", ok)], min_decided=3)
    assert short["state"] == "REVIEW_REQUIRED"     # no quorum is not a pass
