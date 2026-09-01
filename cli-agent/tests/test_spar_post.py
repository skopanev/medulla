"""The post hook: what counts as a delivery.

`test -s` counted a 32-byte artifact — 'NONE / GO' after 547 seconds of work, with no
reason given. A bare verdict cannot be argued with.
"""
import os
import subprocess
from pathlib import Path

import pytest
import yaml as pyyaml
from conftest import MINIMAL, fake_script, load_err, read_manifest, read_run
from conftest import write_workflow as setup
from medulla.v2.classify import Verdict, classify_attempt
from medulla.v2.engine import run_workflow

MARKER = "<!-- spar-delivery-complete -->"


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
                           ("just prose", "no FINDINGS section"),
                           ("## FINDINGS\nNONE\n", "no VERDICT section"),
                           ("## FINDINGS\nNONE\n\n## VERDICT\nmaybe later\n",
                            "not one of GO")):
        rc, err = _post(tmp_path, body)
        assert rc != 0 and expected in err, (body, err)



# ── the machine channel ──────────────────────────────────────────────────────

WORKFLOW = Path(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"


def _post(tmp_path, body, complete=True):
    """Run the panel node's post hook against one panelist file."""
    node = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["panel"]["post"]
    if body and complete:
        body = f"{body.rstrip()}\n{MARKER}\n"
    (tmp_path / "x.md").write_text(body)
    res = subprocess.run(["bash", "-c", node], capture_output=True, text=True,
                         env={**os.environ, "ROUND_DIR": str(tmp_path),
                              "MEDULLA_INPUT_SLUG": "x"}, check=False)
    return res.returncode, res.stderr.strip()




def test_the_hook_reads_a_verdict_at_any_heading_level(tmp_path):
    """Gemini wrote `### VERDICT`. The hook demanded exactly two hashes, failed a
    complete artifact for it, and the round then reported one fewer delivered than it
    held — `artifacts=4, quorum.delivered=3, decided=4` as seen from the field.
    """
    body = ("### FINDINGS\n(R) HIGH — inverted text passes — a.test.ts:250 — why — "
            "FIX: assert equality\n\n### VERDICT\nNO-GO — 1\n")
    rc, err = _post(tmp_path, body)
    assert rc == 0, err


def test_a_fenced_verdict_passes_the_hook_too(tmp_path):
    """The collector and the hook must agree on what a verdict is: the hook vetoing a
    file the collector can read is how a delivered artifact became ok=false."""
    rc, err = _post(tmp_path, "## FINDINGS\nNONE\n\n## VERDICT\n\n```\n## VERDICT\n"
                              "GO — nothing blocks it\n```\n")
    assert rc == 0, err


def test_the_hook_requires_the_terminal_delivery_marker(tmp_path):
    rc, err = _post(tmp_path, "## FINDINGS\nNONE\n\n## VERDICT\nGO — complete\n",
                    complete=False)
    assert rc != 0
    assert f"last non-empty line must be {MARKER}" in err


def test_both_panel_prompts_require_the_same_terminal_marker():
    workflow = pyyaml.safe_load(WORKFLOW.read_text())
    shared = (WORKFLOW.parent / "prompts/spar.md").read_text()
    assert MARKER in workflow["nodes"]["panel"]["prompt"]
    assert MARKER in shared


@pytest.mark.parametrize(("extra", "expected"), [
    ("    post_confirms_delivery: false\n", "requires inputs"),
    ("    inputs: [x]\n    post_confirms_delivery: false\n", "requires post"),
    ("    post_confirms_delivery: \"yes\"\n", "must be a boolean"),
])
def test_delivery_confirmation_has_strict_scope(tmp_path, extra, expected):
    text = MINIMAL.replace("    on_signal:", f"{extra}    on_signal:")
    if "inputs:" in extra:
        text = text.replace("{ok: __exit_ok__}", "{__done__: __exit_ok__}")
    assert expected in load_err(tmp_path, text)


@pytest.mark.parametrize(("rc", "timed_out", "post_rc", "confirmed", "verdict", "reason"), [
    (124, True, 0, True, Verdict.SILENT, None),
    (124, True, 0, False, Verdict.RETRY, "timeout"),
    (124, True, 1, False, Verdict.RETRY, "timeout"),
    (7, False, 0, True, Verdict.RETRY, "rc"),
])
def test_only_validated_delivery_rescues_a_pool_timeout(
        rc, timed_out, post_rc, confirmed, verdict, reason):
    decision = classify_attempt("shell", rc, timed_out, None, post_rc, None, False,
                                pool_mode=True, delivery_confirmed=confirmed)
    assert decision.verdict is verdict
    assert decision.failure_class == reason


def _pool(tmp_path, shell, *, confirms=True, attempts=1):
    post = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["panel"]["post"]
    node = {"inputs": [{"slug": "x"}], "shell": shell, "timeout": 1,
            "max_attempts": attempts, "post": post,
            "on_signal": {"__done__": "__exit_ok__"}}
    if confirms is not None:
        node["post_confirms_delivery"] = confirms
    prepare = {"shell": "mkdir -p \"$MEDULLA_RUN_DIR/artifacts\"; "
                          "echo \"<signal:var key=ROUND_DIR>"
                          "$MEDULLA_RUN_DIR/artifacts</signal:var>\"; "
                          "echo '<signal:ready>ok</signal:ready>'",
               "on_signal": {"ready": "panel"}}
    text = pyyaml.safe_dump({"version": "2", "start": "prepare",
                             "nodes": {"prepare": prepare, "panel": node}},
                            sort_keys=False)
    return setup(tmp_path, text)


def _row(path, step="002-panel"):
    run, _, _ = read_run(path.parent)
    return read_manifest(run, step)[0]


def test_complete_artifact_beats_timeout_once(tmp_path):
    shell = ("printf '## FINDINGS\\nNONE\\n\\n## VERDICT\\nGO — complete\\n"
             f"{MARKER}\\n' > \"$ROUND_DIR/${{MEDULLA_INPUT_SLUG}}.md\"; sleep 30")
    path, work = _pool(tmp_path, shell)
    assert run_workflow(path, workdir=work) == 0
    row = _row(path)
    assert row["ok"] is True and row["timed_out"] is True and row["attempts"] == 1


def test_missing_marker_retries_and_reports_the_veto(tmp_path):
    shell = ("printf '## FINDINGS\\nNONE\\n\\n## VERDICT\\nGO — t\\n' > "
             "\"$ROUND_DIR/${MEDULLA_INPUT_SLUG}.md\"; sleep 30")
    path, work = _pool(tmp_path, shell)
    assert run_workflow(path, workdir=work) == 2
    row = _row(path)
    assert row["reason"] == "timeout" and row["timed_out"] is True
    assert f"last non-empty line must be {MARKER}" in row["message"]


def test_partial_first_attempt_retries_then_delivers(tmp_path):
    shell = ("if [ -f \"$ROUND_DIR/first\" ]; then printf '## FINDINGS\\nNONE\\n\\n"
             f"## VERDICT\\nGO — complete\\n{MARKER}\\n' > "
             "\"$ROUND_DIR/${MEDULLA_INPUT_SLUG}.md\"; exit 0; fi; "
             "touch \"$ROUND_DIR/first\"; printf '## FINDINGS\\nNONE\\n\\n"
             "## VERDICT\\nGO — t\\n' > \"$ROUND_DIR/${MEDULLA_INPUT_SLUG}.md\"; sleep 30")
    path, work = _pool(tmp_path, shell, attempts=2)
    assert run_workflow(path, workdir=work) == 0
    row = _row(path)
    assert row["ok"] is True and row["timed_out"] is False and row["attempts"] == 2


def test_plain_post_cannot_rescue_a_pool_timeout(tmp_path):
    path, work = _pool(tmp_path, "sleep 30", confirms=None)
    assert run_workflow(path, workdir=work) == 2
    row = _row(path)
    assert row["ok"] is False and row["reason"] == "timeout"


def test_post_veto_preserves_watchdog_cause(tmp_path):
    script = fake_script(tmp_path, "hung.sh", "echo started; sleep 30\n")
    text = f"""
version: "2"
start: panel
nodes:
  panel:
    inputs: [x]
    agent: {{harness: fake, model: {script}, idle_timeout: 1}}
    prompt: work
    post: 'test -s missing-artifact.txt'
    on_signal: {{__done__: __exit_ok__}}
"""
    path, work = setup(tmp_path, text)
    assert run_workflow(path, workdir=work) == 2
    row = _row(path, "001-panel")
    assert row["reason"] == "watchdog" and row["timed_out"] is True
