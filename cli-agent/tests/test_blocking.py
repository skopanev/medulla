"""A NO-GO has to name what it rests on, or it cannot be acted on.

A landing went through on `GO 2 · NO-GO 2`: the reader saw a tie, read "verdicts are
not a vote", went to the findings, found nothing that passed the contract's blocker
test, and shipped. Formally correct — nothing tied the two NO-GO verdicts to anything
checkable. Now a NO-GO cites findings by their position in its own list, and the
collector turns those into the ids the reader acts on.
"""
import os
import subprocess
from pathlib import Path

import pytest
import yaml as pyyaml

WORKFLOW = Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"

TEMPLATE = """## FINDINGS
{findings}

## VERDICT
{verdict}
"""


def panel(tmp_path, panelists):
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    for slug, findings, verdict in panelists:
        (art / f"{slug}.md").write_text(TEMPLATE.format(
            findings="\n".join(findings) if findings else "NONE", verdict=verdict))
    manifest = tmp_path / "m.jsonl"
    manifest.write_text("".join(
        '{"key":"%d:x","ok":true}\n' % i for i in range(1, len(panelists) + 1)))
    node = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["synthesize"]["shell"]
    res = subprocess.run(["bash", "-c", node], capture_output=True, text=True, cwd=tmp_path,
                         env={**os.environ, "MEDULLA_RUN_DIR": str(tmp_path),
                              "ROUND_DIR": str(art), "MEDULLA_MANIFEST_PANEL": str(manifest)},
                         check=False)
    assert res.returncode == 0, res.stderr
    return (tmp_path / "verdict.md").read_text(), res.stdout


CACHE = "- (R) HIGH — cache key omits the tenant — src/cache.py:41 — cross-tenant read — FIX: add tenant_id in cache_key()"
NOISE = "- (G) LOW — noisy log — worker.py:12 — clutter — FIX: drop it"


def test_a_cited_no_go_becomes_a_blocking_id(tmp_path):
    out, stdout = panel(tmp_path, [
        ("sonnet", [CACHE], "NO-GO — 1 — the cache defect leaks across tenants"),
        ("gemini", [NOISE], "GO — nothing blocking"),
    ])
    assert "BLOCKING: F1" in out
    assert "NO-GO (F1) — the cache defect" in out      # the local number is translated
    assert "BLOCKING" in stdout                        # and surfaced as an update signal


def test_an_uncited_no_go_is_named_as_an_opinion(tmp_path):
    """The half of the tie nobody could act on."""
    out, _ = panel(tmp_path, [
        ("gpt5", [CACHE], "NO-GO — the change feels rushed"),
        ("gemini", [], "GO — fine"),
    ])
    assert "cites no finding" in out
    assert "Unsupported NO-GO (cites no finding): gpt5" in out
    assert "BLOCKING: none cited." in out


def test_the_reported_tie_now_says_what_to_do(tmp_path):
    """GO 2 · NO-GO 2 — the exact shape that shipped."""
    out, _ = panel(tmp_path, [
        ("sonnet", [CACHE, NOISE], "NO-GO — 1 — leaks across tenants"),
        ("gpt5", ["- (R) HIGH — no down migration — db/0142.sql — cannot roll back — FIX: write it"],
         "NO-GO — 1 — irreversible"),
        ("gemini", [], "GO — fine"),
        ("glm5", [], "GO — fine"),
    ])
    assert "GO 2 · NO-GO 2" in out
    blocking = next(l for l in out.splitlines() if l.startswith("BLOCKING:"))
    assert "F1" in blocking and "F2" in blocking       # both cited findings, whatever the order
    assert "NOT a vote" in out
    assert "not a draw to break by" in out


def test_findings_keep_their_ids_across_the_file(tmp_path):
    out, _ = panel(tmp_path, [("sonnet", [CACHE, NOISE], "NO-GO — 2 — the log is noisy")])
    # the SECOND finding of sonnet is cited; severity sorting puts it last
    assert "BLOCKING: F2" in out
    assert "F2. sonnet — (G) LOW — noisy log" in out
