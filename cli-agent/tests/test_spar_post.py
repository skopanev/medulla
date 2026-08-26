"""The post hook: what counts as a delivery.

`test -s` counted a 32-byte artifact — 'NONE / GO' after 547 seconds of work, with no
reason given. A bare verdict cannot be argued with.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml as pyyaml


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



# ── the machine channel ──────────────────────────────────────────────────────

WORKFLOW = Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"


def _post(tmp_path, body):
    """Run the panel node's post hook against one panelist file."""
    node = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["panel"]["post"]
    (tmp_path / "x.md").write_text(body)
    res = subprocess.run(["bash", "-c", node], capture_output=True, text=True,
                         env={**os.environ, "ROUND_DIR": str(tmp_path),
                              "MEDULLA_INPUT_SLUG": "x"}, check=False)
    return res.returncode, res.stderr.strip()


