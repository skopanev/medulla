"""The panel's verdict is assembled by the workflow's own synthesize node.

It used to call a script the workflow ships, which meant the script had to reach the
container — it did not, only prompts/ was mounted, and a live panel reported success
having written no verdict at all. The tool now lives in the node, where it cannot be
left behind.

These run the node's shell exactly as the engine would: same text, from the yaml.
"""
import os
import subprocess
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


def synthesize(run_dir, delivered=4, expected=4):
    """Run the node's own shell, with the manifest the engine would have handed it."""
    node = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["synthesize"]["shell"]
    manifest = run_dir / "manifest.jsonl"
    rows = [f'{{"key":"{i}:x","ok":{"true" if i <= delivered else "false"}}}'
            for i in range(1, expected + 1)]
    manifest.write_text("\n".join(rows) + "\n")
    env = {**os.environ, "MEDULLA_RUN_DIR": str(run_dir),
           "ROUND_DIR": str(run_dir / "artifacts"),
           "MEDULLA_MANIFEST_PANEL": str(manifest)}
    res = subprocess.run(["bash", "-c", node], capture_output=True, text=True,
                         env=env, cwd=run_dir, check=False)
    assert res.returncode == 0, res.stderr
    return (run_dir / "verdict.md").read_text(), res.stdout


def test_a_partial_panel_still_carries_every_finding(run_dir):
    """The reported failure: quorum met, one panelist silent, findings gone."""
    out, stdout = synthesize(run_dir, delivered=4, expected=5)
    assert "WARNING" in out and "4 of 5" in out
    for slug in ("sonnet", "gpt5", "gemini", "glm5"):
        assert f"{slug} found a thing" in out, slug
    assert "F4." in out
    assert "<signal:ready>" in stdout


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


def test_no_panelists_at_all_does_not_claim_a_verdict(tmp_path):
    (tmp_path / "artifacts").mkdir()
    node = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["synthesize"]["shell"]
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text('{"key":"1:x","ok":false}\n')
    env = {**os.environ, "MEDULLA_RUN_DIR": str(tmp_path),
           "ROUND_DIR": str(tmp_path / "artifacts"),
           "MEDULLA_MANIFEST_PANEL": str(manifest)}
    res = subprocess.run(["bash", "-c", node], capture_output=True, text=True,
                         env=env, cwd=tmp_path, check=False)
    # a file is still written (it carries the warning), and it is honest about content
    out = (tmp_path / "verdict.md").read_text()
    assert "0 findings" in out
    assert "<signal:ready>" in res.stdout          # the file exists, so ready is true


# ── the post hook: what counts as a delivery ─────────────────────────────────

def _post(tmp_path, body):
    """Run the panel node's post hook against one panelist file."""
    node = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["panel"]["post"]
    (tmp_path / "x.md").write_text(body)
    res = subprocess.run(["bash", "-c", node], capture_output=True, text=True,
                         env={**os.environ, "ROUND_DIR": str(tmp_path),
                              "MEDULLA_INPUT_SLUG": "x"}, check=False)
    return res.returncode, res.stderr.strip()


def test_a_verdict_without_a_reason_is_not_a_delivery(tmp_path):
    """Live: gemini returned 32 bytes — "NONE / GO" — after 547 seconds, and `test -s`
    counted it. A bare GO cannot be argued with, and a reader cannot tell a considered
    pass from a panelist that gave up."""
    rc, err = _post(tmp_path, "## FINDINGS\nNONE\n\n## VERDICT\nGO\n")
    assert rc != 0 and "no reason" in err


def test_a_reason_of_any_shape_counts(tmp_path):
    """Including bare citations: "NO-GO — 1" says which finding, which is the point."""
    for body in ("## FINDINGS\nNONE\n\n## VERDICT\nGO — nothing blocking\n",
                 "## FINDINGS\n- (R) HIGH — x — a.py:1 — y — FIX: z\n\n## VERDICT\nNO-GO — 1\n",
                 "## FINDINGS\nNONE\n\n## VERDICT\nINSUFFICIENT — no image was mounted\n"):
        rc, err = _post(tmp_path, body)
        assert rc == 0, (body, err)


def test_the_sections_the_collector_reads_must_exist(tmp_path):
    for body, expected in (("", "no artifact"),
                           ("just prose", "## FINDINGS"),
                           ("## FINDINGS\nNONE\n", "## VERDICT"),
                           ("## FINDINGS\nNONE\n\n## VERDICT\nmaybe later\n",
                            "not one of GO")):
        rc, err = _post(tmp_path, body)
        assert rc != 0 and expected in err, (body, err)
