"""The panel's collector: verdict.md must carry the panel, not just a warning.

Seen live on a 4/5 panel — the file held one line, `WARNING: only 4/5 panelists
delivered`, and none of the findings. Two writers, two different paths: the collector
writes <run>/verdict.md, while the workflow node prepended its warning to
<run>/artifacts/verdict.md and `cat`-ed a file that was not there.
"""
import os
import subprocess
from pathlib import Path

import pytest

COLLECTOR = Path(__file__).resolve().parent.parent / "workflows/spar/scripts/spar-run.sh"

PANELIST = """Prose above.

## FINDINGS
- (R) HIGH — {claim} — {file}:1 — it breaks — FIX: change {func}(), it is the only caller

## VERDICT
{verdict} — because {claim}
"""


@pytest.fixture
def run_dir(tmp_path):
    art = tmp_path / "artifacts"
    art.mkdir()
    for slug, verdict in (("sonnet", "NO-GO"), ("gpt5", "GO"),
                          ("gemini", "GO"), ("glm5", "INSUFFICIENT")):
        (art / f"{slug}.md").write_text(PANELIST.format(
            claim=f"{slug} found a thing", file=f"{slug}.py", func=slug, verdict=verdict))
    return tmp_path


def collect(run_dir, expected=None, delivered=None):
    env = {**os.environ}
    if expected is not None:
        env["SPAR_EXPECTED"], env["SPAR_DELIVERED"] = str(expected), str(delivered)
    res = subprocess.run(["bash", str(COLLECTOR), "findings", str(run_dir)],
                         capture_output=True, text=True, env=env, check=False)
    assert res.returncode == 0, res.stderr
    return (run_dir / "verdict.md").read_text()


def test_a_partial_panel_still_carries_every_finding(run_dir):
    """The reported bug: quorum reached, one panelist silent, findings gone."""
    out = collect(run_dir, expected=5, delivered=4)
    assert "WARNING" in out and "4 of 5" in out
    for slug in ("sonnet", "gpt5", "gemini", "glm5"):
        assert f"{slug} found a thing" in out, slug
    assert out.count("F") >= 4                      # F1..F4 present
    assert "F4." in out


def test_a_full_panel_says_nothing_about_partial_delivery(run_dir):
    out = collect(run_dir, expected=4, delivered=4)
    assert "WARNING" not in out
    assert "F4." in out


def test_the_collector_alone_still_works(run_dir):
    """`medulla launch spar findings <dir>` is run by hand, without the env vars."""
    out = collect(run_dir)
    assert "WARNING" not in out
    assert "sonnet found a thing" in out


def test_verdicts_are_counted_and_sorted_by_severity(run_dir):
    out = collect(run_dir, expected=4, delivered=4)
    assert "GO 2 · NO-GO 1 · INSUFFICIENT 1" in out
    # every panelist's verdict line is present, one each
    for slug in ("sonnet", "gpt5", "gemini", "glm5"):
        assert f"- **{slug}** —" in out


def test_no_stray_verdict_file_is_left_in_artifacts(run_dir):
    collect(run_dir, expected=5, delivered=4)
    assert not (run_dir / "artifacts" / "verdict.md").exists()
