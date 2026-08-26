"""The collector: panelist files in, verdict.md and verdict.json out.

One pass writes both, so the prose and the machine channel cannot drift apart.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml as pyyaml

WORKFLOW = Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"

PANELIST = """Prose above.

## FINDINGS
- (R) {sev} — {claim} — {slug}.py:1 — it breaks — FIX: change {slug}(), the only caller

## VERDICT
{verdict} — because {claim}
"""


@pytest.fixture
def run_dir(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    for slug, verdict, sev in (("sonnet", "NO-GO", "HIGH"), ("gpt5", "GO", "LOW"),
                               ("gemini", "GO", "MED"), ("glm5", "INSUFFICIENT", "HIGH")):
        (art / f"{slug}.md").write_text(PANELIST.format(
            claim=f"{slug} found a thing", slug=slug, verdict=verdict, sev=sev))
    return tmp_path


COLLECTOR = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/collect_verdict.py"


def synthesize(run_dir, delivered=4, expected=4, min_decided=1):
    """Run the collector over a round, as the workflow node does."""
    res = subprocess.run(
        [sys.executable, str(COLLECTOR), str(run_dir), str(run_dir / "artifacts"),
         "--expected", str(expected), "--delivered", str(delivered),
         "--min-decided", str(min_decided)],
        capture_output=True, text=True, check=False)
    return (run_dir / "verdict.md").read_text(), res.stdout


def test_a_partial_panel_still_carries_every_finding(run_dir):
    """The reported failure: quorum met, one panelist silent, findings gone."""
    out, stdout = synthesize(run_dir, delivered=4, expected=5)
    assert "WARNING" in out and "4 of 5" in out
    for slug in ("sonnet", "gpt5", "gemini", "glm5"):
        assert f"{slug} found a thing" in out, slug
    assert "F4." in out
    assert "decided" in stdout


def test_a_full_panel_says_nothing_about_partial_delivery(run_dir):
    out, _ = synthesize(run_dir)
    assert "WARNING" not in out


def test_findings_are_sorted_by_the_severity_the_finder_gave(run_dir):
    out, _ = synthesize(run_dir)
    order = [line.split(" — ")[0] for line in out.splitlines() if line.startswith("F")]
    assert "sonnet" in order[0] or "glm5" in order[0]      # both HIGH
    assert "gpt5" in order[-1]                             # LOW sinks


def test_verdicts_are_counted(run_dir):
    out, _ = synthesize(run_dir)
    assert "GO 2 · NO-GO 1 · INSUFFICIENT 1" in out
    for slug in ("sonnet", "gpt5", "gemini", "glm5"):
        assert f"- **{slug}** —" in out


def test_the_reading_rules_ride_in_the_file(run_dir):
    """The reader is often an agent handed a path, who never saw the skill."""
    out, _ = synthesize(run_dir)
    assert "HOW TO READ THIS" in out
    assert "Do NOT re-summarise" in out


def test_an_empty_round_still_writes_an_honest_file(tmp_path):
    """Nobody delivered: the file exists and says so, rather than not existing at all —
    a missing file is indistinguishable from a collector that broke."""
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    out, stdout = synthesize(tmp_path, delivered=0, expected=5, min_decided=1)
    assert "0 findings" in out
    assert "GO 0" in out and "NO-GO 0" in out
    assert "only 0 of 5 panelists delivered" in out
    assert "0 decided" in stdout


# ── the post hook: what counts as a delivery ─────────────────────────────────

def test_the_subject_is_optional_and_only_what_was_given_appears(tmp_path):
    """A gate field holding an empty string reads as an answer. Absent stays absent."""
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "x.md").write_text("## FINDINGS\nNONE\n\n## VERDICT\nGO — fine\n")

    def run(*subject):
        (tmp_path / "verdict.json").unlink(missing_ok=True)
        subprocess.run([sys.executable, str(COLLECTOR), str(tmp_path), str(art),
                        "--expected", "1", "--delivered", "1", "--min-decided", "1",
                        *sum((["--subject", s] for s in subject), [])],
                       capture_output=True, text=True, check=False)
        return json.loads((tmp_path / "verdict.json").read_text())

    assert "subject" not in run()
    assert "subject" not in run("ticket=", "head=")          # passed but empty
    assert run("ticket=", "head=abc123")["subject"] == {"head": "abc123"}
    assert run("ticket=T-1", "purpose=review the cache")["subject"] == {
        "ticket": "T-1", "purpose": "review the cache"}

def test_verdict_json_carries_what_a_gate_needs(tmp_path):
    """workflows-aqmq1i6snq: outcome.json held only terminal metadata, so a consumer
    had to parse Markdown back into gate facts."""
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "sonnet.md").write_text(
        "## FINDINGS\n- (R) HIGH — leak — a.py:41 — cross-tenant — FIX: key it\n\n"
        "## VERDICT\nNO-GO — 1 — the cache leaks\n")
    (art / "gpt5.md").write_text("## FINDINGS\nNONE\n\n## VERDICT\nGO — nothing blocking\n")
    (art / "glm5.md").write_text("## FINDINGS\nNONE\n\n## VERDICT\nINSUFFICIENT — no image\n")

    synthesize(tmp_path, delivered=3, expected=5, min_decided=2)
    data = json.loads((tmp_path / "verdict.json").read_text())

    assert data["counts"] == {"GO": 1, "NO-GO": 1, "INSUFFICIENT": 1, "none": 0}
    assert data["quorum"] == {"expected": 5, "delivered": 3, "min_decided": 2,
                              "decided": 2, "met": True}
    assert data["blocking"] == ["F1"]
    assert {p["slug"]: p["verdict"] for p in data["panelists"]} == {
        "sonnet": "NO-GO", "gpt5": "GO", "glm5": "INSUFFICIENT"}
    assert data["findings"][0] == {"id": "F1", "panelist": "sonnet", "confidence": "R",
                                   "severity": "HIGH",
                                   "text": "(R) HIGH — leak — a.py:41 — cross-tenant — FIX: key it"}

def test_verdict_json_is_written_when_the_round_fails(tmp_path):
    """Why it failed is a fact a gate needs — and it must fail CLOSED."""
    art = tmp_path / "artifacts"
    art.mkdir()
    for slug in ("a", "b", "c"):
        (art / f"{slug}.md").write_text(
            "## FINDINGS\nNONE\n\n## VERDICT\nINSUFFICIENT — empty workspace\n")

    synthesize(tmp_path, delivered=3, expected=3, min_decided=3)
    data = json.loads((tmp_path / "verdict.json").read_text())
    assert data["quorum"]["met"] is False
    assert data["quorum"]["decided"] == 0
    assert data["counts"]["INSUFFICIENT"] == 3

def test_the_two_channels_agree(tmp_path):
    """One pass writes both, so they cannot drift apart."""
    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "x.md").write_text(
        "## FINDINGS\n- (G) MED — a — f.py:1 — b — FIX: c\n\n## VERDICT\nGO — fine\n")
    md, _ = synthesize(tmp_path, delivered=1, expected=1, min_decided=1)
    data = json.loads((tmp_path / "verdict.json").read_text())

    assert f"GO {data['counts']['GO']}" in md
    assert f"{len(data['findings'])} findings" in md
    for f in data["findings"]:
        assert f"{f['id']}. {f['panelist']}" in md

EMPTY_WS = "## FINDINGS\nNONE\n\n## VERDICT\nINSUFFICIENT — /workspace was empty\n"

def _synthesize(tmp_path, panelists, min_decided="3"):
    art = tmp_path / "artifacts"
    art.mkdir(exist_ok=True)
    for slug, body in panelists:
        (art / f"{slug}.md").write_text(body)
    manifest = tmp_path / "m.jsonl"
    manifest.write_text("".join('{"key":"%d:x","ok":true}\n' % i
                                for i in range(1, len(panelists) + 1)))
    res = subprocess.run(
        [sys.executable, str(COLLECTOR), str(tmp_path), str(art),
         "--expected", str(len(panelists)), "--delivered", str(len(panelists)),
         "--min-decided", min_decided], capture_output=True, text=True, check=False)
    marker = "<signal:no_quorum>" if res.returncode == 3 else "<signal:ready>"
    return marker, (tmp_path / "verdict.md").read_text()

def test_a_panel_of_insufficient_is_not_a_verdict(tmp_path):
    """Live (workflows-omj8pb7iif): a --mount failed, the retry ran without it, three of
    four panelists reported INSUFFICIENT because their workspace was empty — and a
    verdict.md was produced anyway, looking like an answer because the fourth had
    reconstructed part of the diff from git history."""
    stdout, out = _synthesize(tmp_path, [
        ("gemini", EMPTY_WS), ("glm5", EMPTY_WS), ("gpt5", EMPTY_WS),
        ("sonnet", "## FINDINGS\n- (R) HIGH — x — a.py:1 — breaks — FIX: guard\n\n"
                   "## VERDICT\nNO-GO — 1 — reconstructed from git\n"),
    ])
    assert "<signal:no_quorum>" in stdout
    assert "<signal:ready>" not in stdout       # not an answer, whatever the file says
    assert "INSUFFICIENT 3" in out              # and the file shows who could not see
    assert "/workspace was empty" in out

def test_enough_opinions_still_produce_a_verdict(tmp_path):
    body = "## FINDINGS\n- (R) MED — x — f.py:1 — y — FIX: z\n\n## VERDICT\n%s\n"
    stdout, _out = _synthesize(tmp_path, [
        ("a", body % "GO — fine"), ("b", body % "GO — fine"),
        ("c", body % "NO-GO — 1 — no"), ("d", EMPTY_WS),
    ])
    assert "<signal:ready>" in stdout
    assert "no_quorum" not in stdout
