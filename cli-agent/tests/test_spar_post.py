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


def _post_body():
    return pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["panel"]["post"]


def _run_post(round_dir, slug, attempt):
    return subprocess.run(["bash", "-c", _post_body()], capture_output=True, text=True,
                          env={**os.environ, "ROUND_DIR": str(round_dir),
                               "MEDULLA_INPUT_SLUG": slug,
                               "MEDULLA_ATTEMPT_ID": attempt}, check=False)


def test_a_retry_cannot_erase_the_verdict_the_panelist_gave_first(tmp_path):
    """Measured twice in the field. sonnet.md changed finding ids and severities
    under one filename; gemini.md flipped GO with 3 LOW into NO-GO with 2 HIGH —
    same panelist, same run, opposite verdict.

    The flip is the dangerous one: a reader cannot defend against it by re-judging
    findings, because a GO carries none to re-judge. And keeping a copy only on
    REJECTION missed exactly this case — the first artifact PASSES the hook and is
    overwritten anyway, because the body died or timed out after writing it.
    """
    art = tmp_path / "artifacts"
    art.mkdir()
    art.joinpath("gemini.md").write_text(
        "## FINDINGS\nNONE\n\n## VERDICT\nGO — nothing blocking\n"
        "<!-- spar-delivery-complete -->\n")
    assert _run_post(art, "gemini", "001.i1.p1").returncode == 0, "a valid GO passes"

    art.joinpath("gemini.md").write_text(
        "## FINDINGS\n- (R) HIGH — a — f:1 — w — FIX: x\n\n"
        "## VERDICT\nNO-GO — 1 — reason\n<!-- spar-delivery-complete -->\n")
    _run_post(art, "gemini", "001.i1.p2")

    kept = sorted(p.name for p in (art / "superseded").iterdir())
    assert kept == ["gemini.001.i1.p1.md", "gemini.001.i1.p2.md"], kept
    first = (art / "superseded" / "gemini.001.i1.p1.md").read_text()
    assert "GO — nothing blocking" in first, "the first verdict must stay recoverable"


def test_superseded_copies_are_not_counted_as_panelists(tmp_path):
    """They live in a subdirectory precisely so the collector's *.md glob misses
    them — otherwise one panelist retried would inflate the roster."""
    art = tmp_path / "artifacts"
    art.mkdir()
    art.joinpath("gemini.md").write_text(
        "## FINDINGS\nNONE\n\n## VERDICT\nGO — fine\n<!-- spar-delivery-complete -->\n")
    _run_post(art, "gemini", "001.i1.p1")
    assert (art / "superseded").is_dir()
    assert sorted(p.name for p in art.glob("*.md")) == ["gemini.md"]


# ── why a panelist wrote nothing ────────────────────────────────────────────────

import sys as _sys  # noqa: E402
from pathlib import Path as _P  # noqa: E402

_sys.path.insert(0, str(_P(__file__).resolve().parent.parent / "workflows/spar/scripts"))
from provider_error import provider_error  # noqa: E402

OPENCODE_429 = (
    '[out] [rtk] rtk binary not found in PATH — plugin disabled\n'
    '[out] {"type":"error","timestamp":1789047005833,"sessionID":"ses_f747",'
    '"error":{"name":"APIError","data":{"message":"Weekly/Monthly Limit Exhausted. '
    'Your limit will reset at 2026-09-13 10:00:29","statusCode":429,'
    '"responseHeaders":{"set-cookie":"acw_tc=SECRETCOOKIEVALUE;path=/",'
    '"x-request-id":"bcd8105a-1f6c-45d6-8f71-fdc24a7833d7"},'
    '"responseBody":"{\\"error\\":{\\"code\\":\\"1310\\"}}"}}}\n')


def test_the_reason_is_extracted_from_STDOUT(tmp_path):
    """The harness exits 1 with an EMPTY stderr and reports the failure as JSON on
    stdout, so the manifest carried "stderr:" and nothing after it. Three panels were
    escalated as unexplained while this line sat in a file nobody knew to open."""
    log = tmp_path / "attempt-2-opencode.txt"
    log.write_text(OPENCODE_429)
    out = provider_error(log)
    assert "Weekly/Monthly Limit Exhausted" in out and "HTTP 429" in out


def test_it_does_not_leak_cookies_or_request_ids(tmp_path):
    """That JSON also carries response headers: session cookies and request ids. A
    manifest is read by a whole fleet and travels into tickets and chat."""
    log = tmp_path / "attempt-1-opencode.txt"
    log.write_text(OPENCODE_429)
    out = provider_error(log)
    assert "SECRETCOOKIEVALUE" not in out and "set-cookie" not in out
    assert "bcd8105a" not in out, "request id leaked"
    assert len(out) <= 200


def test_a_clean_log_yields_nothing_to_say(tmp_path):
    """No invented reason: a body that failed without saying why must not be given
    words it never said."""
    log = tmp_path / "attempt-1-opencode.txt"
    log.write_text('[out] {"type":"step_start","sessionID":"ses_x"}\n')
    assert provider_error(log) == ""


def test_a_missing_log_is_not_an_error(tmp_path):
    """The hook runs on failures of every kind, including ones with no log at all."""
    assert provider_error(tmp_path / "nope.txt") == ""


def test_the_LAST_error_wins(tmp_path):
    """Two attempts, two errors: the reader wants the one that ended it."""
    log = tmp_path / "attempt-1-opencode.txt"
    log.write_text(
        '[out] {"type":"error","error":{"name":"E","data":{"message":"transient blip","statusCode":500}}}\n'
        + OPENCODE_429)
    assert "Limit Exhausted" in provider_error(log)


def test_the_hook_still_FAILS_when_it_explains_itself(tmp_path, monkeypatch):
    """Explaining a failure must not soften it. The seat produced nothing; the round
    is still short one panelist, and the hook still exits non-zero."""
    import subprocess
    import yaml as _yaml
    wf = _P(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"
    post = _yaml.safe_load(wf.read_text())["nodes"]["panel"]["post"]
    round_dir = tmp_path / "artifacts"
    round_dir.mkdir()
    log = tmp_path / "attempt-1-opencode.txt"
    log.write_text(OPENCODE_429)
    res = subprocess.run(["bash", "-c", post], capture_output=True, text=True, check=False,
                         env={**os.environ,
                              "ROUND_DIR": str(round_dir),
                              "MEDULLA_INPUT_SLUG": "glm5",
                              "MEDULLA_ATTEMPT_LOG": str(log),
                              "MEDULLA_WORKFLOW_DIR": str(wf.parent)})
    assert res.returncode == 1, "a failure that explains itself is still a failure"
    assert "no artifact written" in res.stderr
    assert "Limit Exhausted" in res.stderr, res.stderr


def test_the_engine_hands_the_hook_the_attempt_log():
    """The hook cannot read what it is not told about — the path is the engine's to
    provide, and without it the explanation silently never appears."""
    src = (_P(__file__).resolve().parent.parent
           / "medulla/v2/engine_attempts.py").read_text()
    assert "MEDULLA_ATTEMPT_LOG" in src
    assert 'f"attempt-{total}-{tag}.txt"' in src.split("MEDULLA_ATTEMPT_LOG")[1][:200]


# ── the hook and the collector must answer the same question the same way ───────

def _post_verdict(tmp_path, body):
    """Run the real post hook over one artifact; returns (rc, stderr)."""
    import subprocess
    import yaml as _yaml
    wf = _P(__file__).resolve().parent.parent / "workflows/spar/workflow.yaml"
    post = _yaml.safe_load(wf.read_text())["nodes"]["panel"]["post"]
    round_dir = tmp_path / "artifacts"
    round_dir.mkdir(exist_ok=True)
    (round_dir / "sonnet.md").write_text(body)
    res = subprocess.run(["bash", "-c", post], capture_output=True, text=True, check=False,
                         env={**os.environ, "ROUND_DIR": str(round_dir),
                              "MEDULLA_INPUT_SLUG": "sonnet",
                              "MEDULLA_WORKFLOW_DIR": str(wf.parent)})
    return res.returncode, res.stderr


COMPLETE = """## {findings}
- (R) HIGH — a real problem — a.py:1 — it breaks — FIX: change it

## {verdict}
NO-GO — 1

<!-- spar-delivery-complete -->
"""


def test_a_lowercase_findings_heading_is_accepted(tmp_path):
    """`## Findings` had a COMPLETE review rejected by the hook while the collector
    would have parsed it whole — the system refusing what it can already read. Five
    of the thirteen stored "no FINDINGS section" vetoes were this and nothing else,
    and the retry never helped: the panelist writes the same heading again."""
    rc, err = _post_verdict(tmp_path, COMPLETE.format(findings="Findings", verdict="VERDICT"))
    assert rc == 0, err


def test_a_lowercase_verdict_heading_is_accepted(tmp_path):
    rc, err = _post_verdict(tmp_path, COMPLETE.format(findings="FINDINGS", verdict="Verdict"))
    assert rc == 0, err


def test_the_hook_and_the_collector_agree(tmp_path):
    """The property that matters is not the flag, it is that ONE file gets ONE answer.
    Both are asked; disagreement in either direction is the defect."""
    from verdict_parse import read_panelist
    for headings in (("Findings", "VERDICT"), ("FINDINGS", "Verdict"),
                     ("findings", "verdict"), ("FINDINGS", "VERDICT")):
        body = COMPLETE.format(findings=headings[0], verdict=headings[1])
        rc, err = _post_verdict(tmp_path, body)
        art = tmp_path / "artifacts" / "sonnet.md"
        art.write_text(body)
        parsed = read_panelist(art)
        collector_ok = not parsed["malformed"]
        assert (rc == 0) == collector_ok, f"{headings}: hook rc={rc}, collector={parsed['malformed']} ({err})"


def test_a_missing_findings_section_is_STILL_rejected(tmp_path):
    """The fix aligns two checks; it does not remove one. An artifact with no findings
    section at all is still incomplete."""
    rc, err = _post_verdict(tmp_path, "## VERDICT\nGO — because\n")
    assert rc == 1 and "no FINDINGS section" in err
