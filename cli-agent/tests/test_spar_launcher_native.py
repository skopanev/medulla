"""The panel must survive a dead container runtime.

A colima content-store fault took Docker down and spar went with it — not because
the panel needs a container, but because the launcher demanded one and the prepare
guard checked a path that only exists inside one. Every harness runs on the host.
"""
import time
import signal
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml as pyyaml

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "workflows/spar/scripts/spar-run.sh"
WORKFLOW = ROOT / "workflows/spar/workflow.yaml"


def _prepare(tmp_path, question="q"):
    """Run the prepare node's shell body in tmp_path, as medulla would."""
    body = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         cwd=tmp_path, env={**os.environ, "QUESTION": question},
                         check=False)
    return res.returncode, res.stdout + res.stderr


def test_prepare_accepts_a_non_empty_tree_outside_a_container(tmp_path):
    """The guard used to check /workspace, which exists only inside the container.
    Natively it failed on its own check and the run died before any panelist."""
    (tmp_path / "somefile.py").write_text("x = 1\n")
    rc, out = _prepare(tmp_path)
    assert rc == 0, out
    assert "ready" in out


def test_prepare_still_refuses_an_empty_tree(tmp_path):
    """The guard exists because a failed --mount once produced a verdict from five
    agents reasoning about nothing. Re-pointing it must not disarm it."""
    rc, out = _prepare(tmp_path)
    assert rc != 0
    assert "empty" in out


def test_prepare_ignores_bookkeeping_directories(tmp_path):
    """`.medulla`, `.git` and `box` are ours, not the tree under review."""
    for d in (".medulla", ".git", "box"):
        (tmp_path / d).mkdir()
    rc, out = _prepare(tmp_path)
    assert rc != 0, "a tree holding only our own directories is still empty"


def test_prepare_requires_a_question(tmp_path):
    (tmp_path / "somefile.py").write_text("x = 1\n")
    body = pyyaml.safe_load(WORKFLOW.read_text())["nodes"]["prepare"]["shell"]
    env = {k: v for k, v in os.environ.items() if k != "QUESTION"}
    res = subprocess.run(["bash", "-c", body], capture_output=True, text=True,
                         cwd=tmp_path, env=env, check=False)
    assert res.returncode != 0
    assert "question" in (res.stdout + res.stderr)


def test_launcher_does_not_hard_require_docker():
    """`command -v docker || die` made a container-runtime outage fatal to a panel
    that does not need a container."""
    src = LAUNCHER.read_text()
    assert 'die "docker is not installed' not in src
    assert 'die "the docker daemon is not responding' not in src
    assert "DOCKER_OK" in src, "the launcher must decide a mode, not assume one"


def test_launcher_still_prefers_docker_when_healthy():
    src = LAUNCHER.read_text()
    assert 'if [ "$DOCKER_OK" = yes ]; then' in src
    assert "--docker --cwd-ro" in src, "the container path keeps the read-only tree"


def test_launcher_announces_the_reduced_native_panel():
    """A thinner panel is a fact the caller must be told, not discover."""
    src = LAUNCHER.read_text()
    assert "3 of 5" in src
    assert "NOT mounted read-only" in src


def test_launcher_liveness_check_works_without_docker():
    """The wait watchdog counted containers; natively that reports every healthy
    panel as dead sixty seconds in."""
    src = LAUNCHER.read_text()
    assert "alive_count()" in src
    assert "docker ps -q --filter 'name=^medulla-'" not in src.split("alive_count() {")[0]


def test_the_workflow_ships_exactly_one_launcher():
    """`medulla launch spar start ...` is the owner's command, and medulla picks the
    launcher by the executable bit — several executables and it refuses to guess. A
    helper script added to scripts/ with a stray chmod +x took that command out: it
    started answering "workflow 'spar' ships several launchers — name one". The helpers
    beside it (collect_verdict.py, quota_precheck.sh) are run BY the workflow and are
    not executable; only spar-run.sh is."""
    import os
    from pathlib import Path
    scripts = Path(__file__).resolve().parent.parent / "workflows/spar/scripts"
    executable = sorted(p.name for p in scripts.iterdir()
                        if p.is_file() and os.access(p, os.X_OK))
    assert executable == ["spar-run.sh"], executable


def _start(question, *extra):
    """Run `start` with medulla absent from PATH: preflight dies right after the
    litter guard, so the guard's verdict is observable without a real panel — and
    without the docker image build that a live start triggers."""
    env = {**os.environ, "PATH": "/bin:/usr/bin"}   # bash and git yes, medulla no
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "start", str(question), *extra],
                         capture_output=True, text=True, cwd=ROOT, env=env, check=False)
    return res.returncode, res.stdout + res.stderr


def test_start_refuses_a_question_file_left_in_the_tree(tmp_path):
    """`start` copies the question into the run's box, so the caller's file is dead
    weight the moment the panel begins — and left in a repo it stays for months. One
    workspace collected 71 stray panel-question/panel-handout/BRIEF files, 292 MB, in
    its root. The skill asks for $TMPDIR; this is what makes it true."""
    litter = ROOT / "test-question-litter.md"
    litter.write_text("brief\n")
    try:
        rc, out = _start(litter)
    finally:
        litter.unlink()
    assert rc != 0, out
    assert "mktemp" in out, "the refusal must say where the brief belongs"
    assert "--allow-outside-tmp" in out, "a human with a hand-written brief needs the door"


def test_start_accepts_a_question_under_tmp(tmp_path):
    """pytest's tmp_path lives under /var/folders on macOS and /tmp on Linux — both
    are canonical temp roots. A guard that rejected them would block the very place
    the skill now tells every caller to write."""
    q = tmp_path / "q.md"
    q.write_text("brief\n")
    rc, out = _start(q)
    assert "refusing to litter" not in out, out
    assert "medulla is not installed" in out, "the guard must pass before preflight"


def test_the_override_flag_is_not_passed_through_to_medulla():
    """--allow-outside-tmp is the launcher's own; medulla would reject it as unknown."""
    src = LAUNCHER.read_text()
    assert "--allow-outside-tmp" in src
    body = src.split("cmd_start() {")[1]
    assert "rotate each argument exactly once" in body, "the flag must be stripped, not forwarded"


def test_the_wait_outlives_the_workflow_deadline(tmp_path):
    """How long to wait is not an independent opinion — it is the workflow's own
    deadline plus room to conclude. They had drifted: the wait gave up at 2700s while
    the run was entitled to 3600, so a lane read "timed out, no verdict" fifteen
    minutes before the engine would have stopped anything. Measured live downstream:
    container up 56 minutes, wait expired at 45, three of four delivered and
    min_success was already met — a finished round whose last worker was still inside
    its budget, pinning a lane slot with no work left in it."""
    wf = tmp_path / ".medulla" / "workflows" / "spar"
    wf.mkdir(parents=True)
    (wf / "workflow.yaml").write_text('version: "2"\ntimeout: 1234\nstart: n\n')
    body = "\n".join([
        'WORKFLOW=spar', 'FALLBACK_TIMEOUT=3900',
        _extract(LAUNCHER.read_text(), "default_timeout"),
        'default_timeout',
    ])
    out = subprocess.run(["/bin/bash", "-c", body], cwd=tmp_path,
                         capture_output=True, text=True, check=False).stdout.strip()
    assert out == "1534", f"deadline+grace expected, got {out!r}"


def _extract(src, fname):
    start = src.index(f"{fname}() {{")
    end = src.index("\n}", start) + 2
    return src[start:end]


def test_alive_count_asks_about_one_run_not_every_panel():
    """`--filter name=^medulla-` answered for EVERY panel on the machine, so a wait
    sat quietly through its own round's death whenever a stranger's round was up.
    Measured by a lane whose wait hung on a round already dead, and by a round whose
    four panelists were SIGTERMed inside one second — the shape of an external stop,
    not four independent failures.

    Asserted on the source: stubbing `docker` here means stubbing `command -v` too,
    and a test that fakes the shell builtins it depends on proves less than reading
    the filter it is meant to check.
    """
    body = _extract(LAUNCHER.read_text(), "alive_count")
    assert 'name=^medulla-$(basename "$run")' in body, body
    assert 'local run="${1:-}"' in body, "the run must be an argument, not a global"
    assert "docker ps -q --filter 'name=^medulla-'" in body, \
        "the machine-wide form stays as the fallback when no run is named"


def test_a_container_is_named_after_its_run():
    """A random uuid made every panel indistinguishable: a kill by name or image hit
    whoever else was running, and I did exactly that to another team's panel."""
    src = (LAUNCHER.resolve().parent.parent.parent.parent
           / "scripts" / "dockerlib" / "process.py").read_text()
    assert 'container_name = f"medulla-{safe[:100]}"' in src
    assert "if run_dir_name:" in src
    assert 'f"medulla-{uuid.uuid4().hex[:8]}"' in src, "fallback stays for callers with no run dir"


def test_a_round_that_died_at_startup_shows_the_reason(tmp_path):
    """`start` is fire-and-forget: it prints the run directory and returns 0 the
    moment medulla names it — before the container has done anything. When the engine
    then dies (a workflow file that was not there, an image that will not build) there
    is no run directory, no journal and no outcome, and the caller was told the panel
    started. Reported live: medulla exited on FileNotFoundError for its own
    workflow.yaml, and the lane had nothing but silence to go on. The launcher's own
    stderr had the answer the whole time.
    """
    box = tmp_path / "box"
    box.mkdir()
    run = box / "2026-09-10_12-00-00-lane"          # named, never created
    (box / "run.120000-4242.log").write_text(f"{run}\n")
    (box / "err.120000-4242.log").write_text(
        "Traceback (most recent call last):\n"
        "FileNotFoundError: /mnt/medulla-workflows/spar/workflow.yaml\n")
    res = subprocess.run(
        ["/bin/bash", str(LAUNCHER), "wait", str(run), "--timeout", "120"],
        capture_output=True, text=True, timeout=180, check=False,
        env={**os.environ, "SPAR_STARTUP_GRACE_S": "0",
             "PATH": f"/bin:/usr/bin:{os.environ.get('PATH', '')}"})
    assert "died at startup" in res.stderr, res.stderr
    assert "FileNotFoundError" in res.stderr, "the reason was one file away and unread"


# ── a round that finished but was never marked finished ─────────────────────────

def _finished_run(tmp_path, next_state="__exit_ok__", with_outcome=False):
    run = tmp_path / "run"
    (run / "artifacts").mkdir(parents=True)
    (run / "journal.jsonl").write_text(
        json.dumps({"node": "prepare", "signal": "ready", "next": "panel"}) + "\n"
        + json.dumps({"node": "synthesize", "signal": "ready", "next": next_state}) + "\n")
    (run / "verdict.md").write_text("# Panel verdict\n")
    (run / "artifacts" / "sonnet.md").write_text("## VERDICT\nGO — 1\n")
    if with_outcome:
        (run / "outcome.json").write_text('{"outcome": "succeeded"}')
    return run


def test_a_completed_round_is_not_reported_as_dead(tmp_path):
    """outcome.json is written LAST, so an engine killed after the terminal transition
    leaves a COMPLETE review with no marker — journal terminal, verdict.json and
    verdict.md on disk. `wait` keyed off the marker alone and announced "no container
    is running and no outcome was written" about a review that had finished. Seen
    twice on live rounds; a lane read the verdict anyway and the tool contradicted it.
    """
    run = _finished_run(tmp_path)
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "wait", str(run), "--timeout", "30"],
                         capture_output=True, text=True, timeout=120, check=False,
                         env={**os.environ, "SPAR_STARTUP_GRACE_S": "0"})
    assert "FINISHED but was never marked" in res.stderr, res.stderr
    assert "sonnet.md" in res.stdout, "the artifacts of a finished round must still print"


def test_an_unfinished_round_is_still_reported_as_dead(tmp_path):
    """Preserve failure detection: a journal that never reached a terminal state is
    NOT a finished round, however many artifacts are lying about. Keying off the
    presence of a verdict file instead of the journal would erase real failures."""
    run = _finished_run(tmp_path, next_state="panel")
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "wait", str(run), "--timeout", "30"],
                         capture_output=True, text=True, timeout=120, check=False,
                         env={**os.environ, "SPAR_STARTUP_GRACE_S": "0"})
    assert "FINISHED but was never marked" not in res.stderr
    assert res.returncode == 3, res.stderr


def test_a_panel_survives_the_session_that_started_it(tmp_path):
    """`cmd &` leaves the child in the caller's process group, so when that session
    ends the kernel SIGHUPs the group and the panel dies mid-round — after twenty
    minutes of paid models. That is what produced the finished-but-unmarked rounds:
    the engine writes outcome.json last and never got there.
    """
    project = tmp_path / "project"
    (project / ".medulla/workflows/spar").mkdir(parents=True)
    question = tmp_path / "q.md"
    question.write_text("Review this fixture.\n")
    marker = tmp_path / "survived"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "docker").write_text("#!/bin/sh\nexit 0\n")
    # prints the run dir at once, then outlives its parent by a second and records it
    # The launcher only accepts a run path INSIDE the box it named, so echo the box
    # it was given rather than the cwd.
    (binaries / "medulla").write_text(
        '#!/bin/sh\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  [ "$1" = --runs-folder ] && { box=$2; shift 2; continue; }\n'
        '  shift\n'
        'done\n'
        'printf "%s/fixture-run\\n" "$(cd "$box" && pwd -P)"\n'
        'sleep 3\nprintf done > "$SPAR_TEST_MARKER"\n')
    for name in ("docker", "medulla"):
        (binaries / name).chmod(0o755)

    env = {**os.environ, "PATH": f"{binaries}:/bin:/usr/bin",
           "MEDULLA_PANEL_RUNS": str(tmp_path / "panel-runs" / "box"),
           "SPAR_TEST_MARKER": str(marker)}
    # Run the launcher in its OWN session, then hang up that session — exactly what
    # happens when the caller's shell goes away.
    proc = subprocess.Popen(["/bin/bash", "-c",
                             f"exec {LAUNCHER} start {question}"],
                            cwd=project, env=env, start_new_session=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pgid = os.getpgid(proc.pid)      # BEFORE waiting: once reaped, the pid is gone
    proc.wait(timeout=60)
    out, err = proc.communicate()
    assert proc.returncode == 0, f"the launcher itself failed: {err.decode()[:400]}"
    os.killpg(pgid, signal.SIGHUP)
    time.sleep(5)
    assert marker.exists(), "the panel died with the session that started it"


def test_a_finished_FAILURE_keeps_its_failure_status(tmp_path):
    """The rescue must not rescue too much. A round whose journal reached
    __exit_fail__ finished — but it finished FAILING, and reporting it as a completed
    review would be worse than the bug being fixed. With no outcome.json there is
    nothing to grep either, and grepping a missing file called every unmarked round a
    failure, including the completed ones this branch exists to save.
    """
    run = _finished_run(tmp_path, next_state="__exit_fail__")
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "wait", str(run), "--timeout", "30"],
                         capture_output=True, text=True, timeout=120, check=False,
                         env={**os.environ, "SPAR_STARTUP_GRACE_S": "0"})
    assert res.returncode == 2, res.stderr
    assert "FAILED" in res.stderr and "__exit_fail__" in res.stderr
    assert "sonnet.md" in res.stdout, "the evidence of a failed round is still evidence"


def test_a_finished_SUCCESS_without_a_marker_is_not_called_a_failure(tmp_path):
    """The other half: no outcome.json used to mean `grep` on a missing file, which
    fails, which printed "the run did NOT succeed" about a completed review."""
    run = _finished_run(tmp_path, next_state="__exit_ok__")
    res = subprocess.run(["/bin/bash", str(LAUNCHER), "wait", str(run), "--timeout", "30"],
                         capture_output=True, text=True, timeout=120, check=False,
                         env={**os.environ, "SPAR_STARTUP_GRACE_S": "0"})
    assert res.returncode == 0, res.stderr
    assert "did NOT succeed" not in res.stderr
